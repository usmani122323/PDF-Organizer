"""
Usmani Wholesale - PDF Organizer API
Flask backend for processing shipping labels and checklists
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import re
import io
from datetime import datetime
from pypdf import PdfReader, PdfWriter
import pdfplumber
import pandas as pd
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend access

# Configuration
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
ALLOWED_EXTENSIONS = {'pdf', 'csv'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_po_number(text):
    """Extract PO Number from checklist"""
    patterns = [
        r'PO\s*Number[:\s]*(\d+)',
        r'PO[:\s]*(\d{15})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_order_number(text):
    """Extract Order # from shipping label"""
    patterns = [
        r'Order\s*#[:\s]*(\d{3}-\d{7}-\d{7})',
        r'(\d{3}-\d{7}-\d{7})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def extract_sku_from_checklist(text):
    """Extract SKUs from checklist"""
    patterns = [
        r'\b(B[A-Z0-9]{9,10})\b',
    ]
    skus = []
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        skus.extend(matches)
    return list(set(skus))


def extract_sku_from_label(text):
    """Extract SKU from shipping label"""
    patterns = [
        r'SKU[:\s]*([A-Z0-9-]{10,15})',
        r'\b([A-Z]\d-[A-Z0-9]{4}-[A-Z0-9]{4})\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def process_pdf(pdf_path, is_checklist=True):
    """Process PDF and extract information"""
    pages_info = []
    reader = PdfReader(pdf_path)
    
    with pdfplumber.open(pdf_path) as pdf:
        for i, (pypdf_page, plumber_page) in enumerate(zip(reader.pages, pdf.pages)):
            text = plumber_page.extract_text()
            
            if is_checklist:
                po_num = extract_po_number(text)
                skus = extract_sku_from_checklist(text)
                pages_info.append({
                    'page': pypdf_page,
                    'po_number': po_num,
                    'skus': skus,
                    'index': i,
                    'type': 'checklist'
                })
            else:
                order_num = extract_order_number(text)
                sku = extract_sku_from_label(text)
                pages_info.append({
                    'page': pypdf_page,
                    'order_number': order_num,
                    'sku': sku,
                    'index': i,
                    'type': 'label'
                })
    
    return pages_info


def match_by_sku(checklists, labels, csv_data=None):
    """Match labels to checklists using SKU only"""
    matched_groups = []
    
    # Parse CSV data if provided - now just for SKU info
    sku_mapping = {}
    if csv_data:
        try:
            df = pd.read_csv(io.StringIO(csv_data))
            df.columns = df.columns.str.lower().str.strip()
            
            # Build mapping of checklist SKU to label SKU
            for _, row in df.iterrows():
                checklist_sku = str(row.get('checklist_sku', '') or row.get('sku', '')).strip()
                label_sku = str(row.get('label_sku', '') or row.get('sku', '')).strip()
                if checklist_sku and label_sku:
                    sku_mapping[checklist_sku] = label_sku
        except Exception as e:
            print(f"Error parsing CSV: {e}")
    
    for checklist in checklists:
        checklist_skus = checklist.get('skus', [])
        
        # Find matching labels by SKU
        matching_labels = []
        
        for label in labels:
            label_sku = label.get('sku', '')
            
            # Try direct matching first
            if any(label_sku in sku or sku in label_sku for sku in checklist_skus):
                matching_labels.append(label)
            # If CSV mapping exists, try that too
            elif sku_mapping:
                for checklist_sku in checklist_skus:
                    if checklist_sku in sku_mapping:
                        mapped_sku = sku_mapping[checklist_sku]
                        if label_sku in mapped_sku or mapped_sku in label_sku:
                            matching_labels.append(label)
                            break
        
        matched_groups.append((checklist, matching_labels))
    
    return matched_groups


def create_organized_pdf(matched_groups, output_path):
    """Create organized PDF"""
    writer = PdfWriter()
    
    for checklist, labels in matched_groups:
        writer.add_page(checklist['page'])
        for label in labels:
            writer.add_page(label['page'])
    
    with open(output_path, 'wb') as f:
        writer.write(f)
    
    return len(writer.pages)


@app.route('/')
def index():
    """Serve the main page"""
    return send_file('index.html')


@app.route('/api/organize-pdfs', methods=['POST'])
def organize_pdfs():
    """Main API endpoint to process PDFs"""
    try:
        # Check files
        if 'checklist' not in request.files or 'labels' not in request.files:
            return jsonify({'error': 'Missing required files'}), 400
        
        checklist_file = request.files['checklist']
        labels_file = request.files['labels']
        
        if checklist_file.filename == '' or labels_file.filename == '':
            return jsonify({'error': 'Empty filename'}), 400
        
        # Save uploaded files
        checklist_path = os.path.join(
            app.config['UPLOAD_FOLDER'],
            secure_filename(f"checklist_{datetime.now().timestamp()}.pdf")
        )
        labels_path = os.path.join(
            app.config['UPLOAD_FOLDER'],
            secure_filename(f"labels_{datetime.now().timestamp()}.pdf")
        )
        
        checklist_file.save(checklist_path)
        labels_file.save(labels_path)
        
        # Get CSV data if provided
        csv_data = request.form.get('csv_data', '')
        
        # Process PDFs
        checklists = process_pdf(checklist_path, is_checklist=True)
        labels = process_pdf(labels_path, is_checklist=False)
        
        # Match
        matched_groups = match_by_sku(checklists, labels, csv_data if csv_data else None)
        
        # Create output
        output_filename = f"organized_{datetime.now().timestamp()}.pdf"
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
        total_pages = create_organized_pdf(matched_groups, output_path)
        
        # Clean up uploaded files
        os.remove(checklist_path)
        os.remove(labels_path)
        
        return jsonify({
            'success': True,
            'checklists': len(checklists),
            'labels': len(labels),
            'matched': len(matched_groups),
            'total_pages': total_pages,
            'download_url': f'/api/download/{output_filename}'
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/download/<filename>')
def download_file(filename):
    """Download processed PDF"""
    try:
        file_path = os.path.join(app.config['OUTPUT_FOLDER'], secure_filename(filename))
        return send_file(
            file_path,
            as_attachment=True,
            download_name=f'organized_shipping_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 404


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy'})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
