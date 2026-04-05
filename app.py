#!/usr/bin/env python3
import base64
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PDF_ORGANIZER_BUCKET = os.environ.get("PDF_ORGANIZER_BUCKET", "supplier-return-labels")
PROCESSOR_URL = os.environ.get("PDF_ORGANIZER_PROCESSOR_URL", "").strip()
PROCESSOR_SECRET = os.environ.get("PDF_ORGANIZER_PROCESSOR_SECRET", "").strip()
WORKER_SECRET = os.environ.get("PDF_ORGANIZER_WORKER_SECRET", "").strip()
HEARTBEAT_SECONDS = int(os.environ.get("PDF_ORGANIZER_WORKER_HEARTBEAT_SECONDS", "20"))
MAX_ATTEMPTS = int(os.environ.get("PDF_ORGANIZER_MAX_ATTEMPTS", "3"))
BASE_BACKOFF_SECONDS = int(os.environ.get("PDF_ORGANIZER_BASE_BACKOFF_SECONDS", "30"))
PROCESSOR_TIMEOUT_SECONDS = int(os.environ.get("PDF_ORGANIZER_PROCESSOR_TIMEOUT_SECONDS", "900"))
PROCESSOR_ATTEMPTS = max(1, int(os.environ.get("PDF_ORGANIZER_PROCESSOR_ATTEMPTS", "3")))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def supabase_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


def ensure_env() -> Optional[str]:
    if not SUPABASE_URL:
        return "SUPABASE_URL is missing"
    if not SUPABASE_SERVICE_ROLE_KEY:
        return "SUPABASE_SERVICE_ROLE_KEY is missing"
    if not PROCESSOR_URL:
        return "PDF_ORGANIZER_PROCESSOR_URL is missing"
    return None


def update_job(job_id: str, payload: Dict[str, Any]) -> None:
    url = f"{SUPABASE_URL}/rest/v1/pdf_organizer_jobs?id=eq.{quote(job_id, safe='')}"
    headers = supabase_headers()
    headers["Content-Type"] = "application/json"
    headers["Prefer"] = "return=minimal"
    r = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"update_job failed {r.status_code}: {r.text[:500]}")


def mark_retry(job_id: str, msg: str, code: str) -> None:
    url = f"{SUPABASE_URL}/rest/v1/rpc/mark_pdf_organizer_job_retry"
    headers = supabase_headers()
    headers["Content-Type"] = "application/json"
    payload = {
        "p_job_id": job_id,
        "p_error_message": msg,
        "p_error_code": code,
        "p_max_attempts": MAX_ATTEMPTS,
        "p_base_backoff_seconds": BASE_BACKOFF_SECONDS,
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"mark_retry failed {r.status_code}: {r.text[:500]}")


def upload_pdf(path: str, content: bytes) -> None:
    safe_path = quote(path, safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{PDF_ORGANIZER_BUCKET}/{safe_path}"
    headers = supabase_headers()
    headers["Content-Type"] = "application/pdf"
    headers["x-upsert"] = "true"
    r = requests.post(url, headers=headers, data=content, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"upload_pdf failed {r.status_code}: {r.text[:500]}")


def heartbeat_loop(job_id: str, worker_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(HEARTBEAT_SECONDS):
        try:
            update_job(job_id, {
                "status": "processing",
                "worker_id": worker_id,
                "last_heartbeat_at": utc_now_iso(),
            })
        except Exception as e:
            print(f"[heartbeat] {job_id} {e}")


def call_processor(checklist_paths: list[str], labels_paths: list[str], csv_data: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"checklistPaths": checklist_paths, "labelsPaths": labels_paths}
    if csv_data and csv_data.strip():
        payload["csvData"] = csv_data

    headers = {"Content-Type": "application/json"}
    if PROCESSOR_SECRET:
        headers["Authorization"] = f"Bearer {PROCESSOR_SECRET}"

    last_err: Optional[Exception] = None
    for attempt in range(1, PROCESSOR_ATTEMPTS + 1):
        try:
            r = requests.post(PROCESSOR_URL, headers=headers, data=json.dumps(payload), timeout=PROCESSOR_TIMEOUT_SECONDS)
            if r.status_code >= 500 and attempt < PROCESSOR_ATTEMPTS:
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code >= 300:
                raise RuntimeError(f"processor HTTP {r.status_code}: {r.text[:800]}")
            data = r.json()
            if not isinstance(data.get("labelsPdf"), str) or not isinstance(data.get("checklistPdf"), str):
                raise RuntimeError("processor missing labelsPdf/checklistPdf")
            return data
        except Exception as e:
            last_err = e
            if attempt < PROCESSOR_ATTEMPTS:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"processor failed after {PROCESSOR_ATTEMPTS} attempts: {last_err}")


def process_job(body: Dict[str, Any]) -> None:
    job_id = str(body["jobId"])
    checklist_paths = [x for x in body.get("checklistPaths", []) if isinstance(x, str) and x]
    labels_paths = [x for x in body.get("labelsPaths", []) if isinstance(x, str) and x]
    csv_data = body.get("csvData") if isinstance(body.get("csvData"), str) else None
    worker_id = str(body.get("workerId") or f"render-worker-{os.getpid()}")

    stop_event = threading.Event()
    t = threading.Thread(target=heartbeat_loop, args=(job_id, worker_id, stop_event), daemon=True)

    try:
        update_job(job_id, {
            "status": "processing",
            "started_at": utc_now_iso(),
            "completed_at": None,
            "error_message": None,
            "last_error_code": None,
            "worker_id": worker_id,
            "last_heartbeat_at": utc_now_iso(),
            "retry_after": None,
        })
        t.start()

        if not checklist_paths or not labels_paths:
            raise RuntimeError("missing checklistPaths or labelsPaths")

        result = call_processor(checklist_paths, labels_paths, csv_data)
        labels_pdf = base64.b64decode(result["labelsPdf"])
        checklist_pdf = base64.b64decode(result["checklistPdf"])

        labels_path = f"pdf-organizer-output/{job_id}/labels.pdf"
        checklist_path = f"pdf-organizer-output/{job_id}/checklists.pdf"

        upload_pdf(labels_path, labels_pdf)
        upload_pdf(checklist_path, checklist_pdf)

        update_job(job_id, {
            "status": "done",
            "completed_at": utc_now_iso(),
            "worker_id": None,
            "last_heartbeat_at": utc_now_iso(),
            "error_message": None,
            "last_error_code": None,
            "retry_after": None,
            "output_paths": {"labelsPath": labels_path, "checklistPath": checklist_path},
            "stats": result.get("stats") if isinstance(result.get("stats"), dict) else None,
        })
        print(f"[worker] done {job_id}")

    except Exception as e:
        msg = str(e)
        print(f"[worker] failed {job_id}: {msg}")
        try:
            mark_retry(job_id, msg, "worker_processing_error")
        except Exception as e2:
            update_job(job_id, {
                "status": "failed",
                "completed_at": utc_now_iso(),
                "error_message": f"{msg} | mark_retry_failed: {e2}",
                "last_error_code": "worker_retry_mark_failed",
                "worker_id": None,
            })
    finally:
        stop_event.set()


def auth_ok(req) -> bool:
    if not WORKER_SECRET:
        return True
    return (req.headers.get("Authorization") or "").strip() == f"Bearer {WORKER_SECRET}"


@app.get("/worker/health")
def health():
    err = ensure_env()
    if err:
        return jsonify({"ok": False, "error": err}), 500
    return jsonify({"ok": True, "status": "healthy"}), 200


@app.post("/worker/process-pdf-organizer-job")
def process_pdf_organizer_job():
    if not auth_ok(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    err = ensure_env()
    if err:
        return jsonify({"ok": False, "error": err}), 500

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

    if not isinstance(body.get("jobId"), str) or not body["jobId"].strip():
        return jsonify({"ok": False, "error": "jobId is required"}), 400
    if not isinstance(body.get("checklistPaths"), list) or not isinstance(body.get("labelsPaths"), list):
        return jsonify({"ok": False, "error": "checklistPaths and labelsPaths must be arrays"}), 400

    threading.Thread(target=process_job, args=(body,), daemon=True).start()
    return jsonify({"ok": True, "accepted": True, "jobId": body["jobId"]}), 202


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
