from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
import pdfplumber
import io
import os
import re
from datetime import datetime
import tempfile
from collections import defaultdict
import traceback

app = Flask(__name__, static_folder='.')
CORS(app)

def extract_skus_from_text(text):
    """Extract SKUs from text using multiple patterns"""
    skus = []
    
    # Pattern 1: N5-96TU-TT9Z style
    pattern1 = re.findall(r'\b[A-Z]\d-[A-Z0-9]{4}-[A-Z0-9]{4}\b', text)
    skus.extend(pattern1)
    
    # Pattern 2: B0090IFLG6 style (Amazon ASIN)
    pattern2 = re.findall(r'\bB[A-Z0-9]{9,10}\b', text)
    skus.extend(pattern2)
    
    # Remove duplicates
    return list(set(skus))

def create_status_overlay(expected_qty, actual_qty, width, height):
    """Create a status banner overlay for checklist"""
    try:
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=(width, height))
        
        # Calculate difference
        difference = actual_qty - expected_qty
        
        # Determine status and color
        if difference == 0:
            status_text = f"✓ {actual_qty}/{expected_qty} Labels - Perfect Match"
            status_color = colors.green
            note = ""
        elif difference > 0:
            status_text = f"⚠ {actual_qty}/{expected_qty} Labels - {difference} EXTRA"
            status_color = colors.orange
            note = f"Note: {difference} extra label(s) included - verify before shipping"
        else:
            status_text = f"✗ {actual_qty}/{expected_qty} Labels - {abs(difference)} MISSING"
            status_color = colors.red
            note = f"WARNING: Missing {abs(difference)} label(s) - DO NOT SHIP until printed"
        
        # Draw status box below header/barcode area (about 150px from top)
        note_height = 60
        box_y = height - 150 - note_height
        
        c.setFillColor(status_color)
        c.setStrokeColor(status_color)
        c.rect(0, box_y, width, note_height, fill=True, stroke=True)
        
        # Add text
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, box_y + 35, "SHIPPING LABELS STATUS:")
        
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, box_y + 15, status_text)
        
        if note:
            c.setFont("Helvetica", 10)
            c.drawString(40, box_y + 3, note)
        
        c.save()
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"❌ Error creating status overlay: {str(e)}")
        raise

@app.route('/')
def home():
    """Serve the main HTML page"""
    try:
        # Get the directory where app.py is located
        base_dir = os.path.dirname(os.path.abspath(__file__))
        index_path = os.path.join(base_dir, 'index.html')
        
        print(f"Looking for index.html in: {base_dir}")
        
        if not os.path.exists(index_path):
            files = os.listdir(base_dir)
            print(f"Files in directory: {files}")
            return jsonify({
                "error": "index.html not found", 
                "looking_in": base_dir,
                "files": files
            }), 404
        
        print(f"✓ Found index.html at: {index_path}")
        return send_file(index_path)
    except Exception as e:
        print(f"❌ Error serving homepage: {str(e)}")
        return jsonify({"error": f"Could not load homepage: {str(e)}"}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy", "message": "PDF Organizer is running"}), 200

@app.route('/api/organize-pdfs', methods=['POST'])
def organize_pdfs():
    try:
        print("\n" + "="*50)
        print("📋 NEW REQUEST RECEIVED")
        print("="*50)
        
        # Get uploaded files
        checklist_file = request.files.get('checklist')
        labels_file = request.files.get('labels')
        csv_data = request.form.get('csv_data', '')
        
        if not checklist_file:
            error_msg = "No checklist file uploaded"
            print(f"❌ ERROR: {error_msg}")
            return jsonify({"error": error_msg}), 400
            
        if not labels_file:
            error_msg = "No labels file uploaded"
            print(f"❌ ERROR: {error_msg}")
            return jsonify({"error": error_msg}), 400
        
        print(f"📋 Checklist: {checklist_file.filename}")
        print(f"🏷️  Labels: {labels_file.filename}")
        
        # Parse CSV mapping if provided
        sku_mapping = {}
        if csv_data:
            print(f"📊 CSV mapping provided")
            for line in csv_data.strip().split('\n'):
                if ',' in line:
                    parts = line.split(',')
                    if len(parts) >= 2:
                        sku_mapping[parts[0].strip()] = parts[1].strip()
            print(f"   Mapped {len(sku_mapping)} SKUs")
        
        # Process checklist PDF
        print("\n🔍 STEP 1: Processing checklist...")
        try:
            checklist_reader = PdfReader(checklist_file)
            print(f"   ✓ Opened checklist PDF ({len(checklist_reader.pages)} pages)")
        except Exception as e:
            error_msg = f"Could not read checklist PDF: {str(e)}"
            print(f"❌ ERROR: {error_msg}")
            return jsonify({"error": error_msg}), 400
        
        checklists = []
        
        try:
            with pdfplumber.open(checklist_file) as pdf:
                for i, (pypdf_page, plumber_page) in enumerate(zip(checklist_reader.pages, pdf.pages)):
                    text = plumber_page.extract_text()
                    print(f"\n   Page {i+1}:")
                    print(f"   - Extracted {len(text)} characters")
                    
                    # Extract SKUs
                    skus = extract_skus_from_text(text)
                    print(f"   - Found SKUs: {skus}")
                    
                    if not skus:
                        print(f"   ⚠️  No SKUs found on page {i+1}")
                        continue
                    
                    # Extract expected quantities
                    sku_quantities = {}
                    for sku in skus:
                        qty_pattern = rf'{re.escape(sku)}\s+(\d+)'
                        qty_match = re.search(qty_pattern, text)
                        if qty_match:
                            qty = int(qty_match.group(1))
                            sku_quantities[sku] = qty
                            print(f"   - {sku}: Expected qty = {qty}")
                        else:
                            sku_quantities[sku] = 0
                            print(f"   - {sku}: No quantity found, assuming 0")
                    
                    checklists.append({
                        'page': pypdf_page,
                        'page_num': i + 1,
                        'skus': skus,
                        'sku_quantities': sku_quantities,
                        'text': text
                    })
        except Exception as e:
            error_msg = f"Error extracting text from checklist: {str(e)}"
            print(f"❌ ERROR: {error_msg}")
            traceback.print_exc()
            return jsonify({"error": error_msg}), 500
        
        if not checklists:
            error_msg = "No SKUs found in checklist PDF. Make sure your checklist contains SKUs like 'N5-96TU-TT9Z' or 'B0090IFLG6'"
            print(f"❌ ERROR: {error_msg}")
            return jsonify({"error": error_msg}), 400
        
        print(f"\n✓ Found {len(checklists)} checklist page(s) with SKUs")
        
        # Process labels PDF
        print("\n🔍 STEP 2: Processing labels...")
        try:
            labels_reader = PdfReader(labels_file)
            print(f"   ✓ Opened labels PDF ({len(labels_reader.pages)} pages)")
        except Exception as e:
            error_msg = f"Could not read labels PDF: {str(e)}"
            print(f"❌ ERROR: {error_msg}")
            return jsonify({"error": error_msg}), 400
        
        labels = []
        
        try:
            with pdfplumber.open(labels_file) as pdf:
                for i, (pypdf_page, plumber_page) in enumerate(zip(labels_reader.pages, pdf.pages)):
                    text = plumber_page.extract_text()
                    
                    # Extract SKU
                    skus = extract_skus_from_text(text)
                    sku = skus[0] if skus else None
                    
                    if sku:
                        print(f"   Label {i+1}: SKU = {sku}")
                    else:
                        print(f"   Label {i+1}: No SKU found")
                    
                    labels.append({
                        'page': pypdf_page,
                        'page_num': i + 1,
                        'sku': sku
                    })
        except Exception as e:
            error_msg = f"Error extracting text from labels: {str(e)}"
            print(f"❌ ERROR: {error_msg}")
            traceback.print_exc()
            return jsonify({"error": error_msg}), 500
        
        print(f"\n✓ Found {len(labels)} label(s)")
        
        # Match labels to checklists
        print("\n🔍 STEP 3: Matching labels to checklists...")
        labels_by_sku = defaultdict(list)
        for label in labels:
            if label['sku']:
                labels_by_sku[label['sku']].append(label)
        
        print(f"   Labels grouped by {len(labels_by_sku)} unique SKUs")
        
        matched_groups = []
        for checklist in checklists:
            matching_labels = []
            
            for sku in checklist['skus']:
                # Direct match
                if sku in labels_by_sku:
                    count = len(labels_by_sku[sku])
                    print(f"   ✓ {sku}: Found {count} matching label(s)")
                    matching_labels.extend(labels_by_sku[sku])
                # Check CSV mapping
                elif sku_mapping and sku in sku_mapping:
                    mapped_sku = sku_mapping[sku]
                    if mapped_sku in labels_by_sku:
                        count = len(labels_by_sku[mapped_sku])
                        print(f"   ✓ {sku} → {mapped_sku}: Found {count} matching label(s) via mapping")
                        matching_labels.extend(labels_by_sku[mapped_sku])
                    else:
                        print(f"   ✗ {sku} → {mapped_sku}: No matching labels found")
                else:
                    print(f"   ✗ {sku}: No matching labels found")
            
            matched_groups.append((checklist, matching_labels))
        
        print(f"\n✓ Matched {len(matched_groups)} order(s)")
        
        # Create organized PDF with status banners
        print("\n🔍 STEP 4: Creating organized PDF...")
        try:
            output = io.BytesIO()
            writer = PdfWriter()
            
            for checklist, matching_labels in matched_groups:
                # Calculate total expected and actual
                total_expected = sum(checklist['sku_quantities'].values())
                total_actual = len(matching_labels)
                
                print(f"   Checklist page {checklist['page_num']}: Expected {total_expected}, Found {total_actual}")
                
                # Get page dimensions
                checklist_page = checklist['page']
                mediabox = checklist_page.mediabox
                width = float(mediabox.width)
                height = float(mediabox.height)
                
                # Create status overlay
                overlay_buffer = create_status_overlay(total_expected, total_actual, width, height)
                overlay_reader = PdfReader(overlay_buffer)
                
                # Merge overlay with checklist
                checklist_page.merge_page(overlay_reader.pages[0])
                
                # Add checklist with overlay
                writer.add_page(checklist_page)
                
                # Add all matching labels
                for label in matching_labels:
                    writer.add_page(label['page'])
            
            # Write to output
            writer.write(output)
            output.seek(0)
            
            total_pages = len(writer.pages)
            print(f"\n✓ Created organized PDF with {total_pages} pages")
            
            # Save to temp file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
            temp_file.write(output.read())
            temp_file.close()
            
            print(f"✓ Saved to: {temp_file.name}")
            print("="*50)
            print("🎉 SUCCESS!")
            print("="*50 + "\n")
            
            # Send file
            return send_file(
                temp_file.name,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=f'organized_shipping_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
            )
            
        except Exception as e:
            error_msg = f"Error creating final PDF: {str(e)}"
            print(f"❌ ERROR: {error_msg}")
            traceback.print_exc()
            return jsonify({"error": error_msg}), 500
        
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        print(f"❌ FATAL ERROR: {error_msg}")
        traceback.print_exc()
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("\n" + "="*50)
    print("🚀 PDF ORGANIZER STARTING")
    print("="*50)
    print(f"Port: {port}")
    print(f"Debug: False")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=port, debug=False)
