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
    
    # Draw status box at top
    note_height = 60
    box_y = height - note_height
    
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

@app.route('/')
def home():
    """Serve the main HTML page"""
    return send_file('index.html')

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"}), 200

@app.route('/api/organize-pdfs', methods=['POST'])
def organize_pdfs():
    try:
        # Get uploaded files
        checklist_file = request.files.get('checklist')
        labels_file = request.files.get('labels')
        csv_data = request.form.get('csv_data', '')
        
        if not checklist_file or not labels_file:
            return jsonify({"error": "Missing required files"}), 400
        
        print(f"📋 Processing checklist: {checklist_file.filename}")
        print(f"🏷️  Processing labels: {labels_file.filename}")
        
        # Parse CSV mapping if provided
        sku_mapping = {}
        if csv_data:
            for line in csv_data.strip().split('\n'):
                if ',' in line:
                    parts = line.split(',')
                    if len(parts) >= 2:
                        sku_mapping[parts[0].strip()] = parts[1].strip()
        
        # Process checklist PDF
        checklist_reader = PdfReader(checklist_file)
        checklists = []
        
        with pdfplumber.open(checklist_file) as pdf:
            for i, (pypdf_page, plumber_page) in enumerate(zip(checklist_reader.pages, pdf.pages)):
                text = plumber_page.extract_text()
                
                # Extract SKUs
                skus = extract_skus_from_text(text)
                
                # Extract expected quantities
                sku_quantities = {}
                for sku in skus:
                    qty_pattern = rf'{re.escape(sku)}\s+(\d+)'
                    qty_match = re.search(qty_pattern, text)
                    if qty_match:
                        sku_quantities[sku] = int(qty_match.group(1))
                    else:
                        sku_quantities[sku] = 0
                
                if skus:
                    checklists.append({
                        'page': pypdf_page,
                        'page_num': i + 1,
                        'skus': skus,
                        'sku_quantities': sku_quantities,
                        'text': text
                    })
        
        print(f"✓ Found {len(checklists)} checklist(s)")
        
        # Process labels PDF
        labels_reader = PdfReader(labels_file)
        labels = []
        
        with pdfplumber.open(labels_file) as pdf:
            for i, (pypdf_page, plumber_page) in enumerate(zip(labels_reader.pages, pdf.pages)):
                text = plumber_page.extract_text()
                
                # Extract SKU
                skus = extract_skus_from_text(text)
                sku = skus[0] if skus else None
                
                labels.append({
                    'page': pypdf_page,
                    'page_num': i + 1,
                    'sku': sku
                })
        
        print(f"✓ Found {len(labels)} label(s)")
        
        # Match labels to checklists
        labels_by_sku = defaultdict(list)
        for label in labels:
            if label['sku']:
                labels_by_sku[label['sku']].append(label)
        
        matched_groups = []
        for checklist in checklists:
            matching_labels = []
            
            for sku in checklist['skus']:
                # Direct match
                if sku in labels_by_sku:
                    matching_labels.extend(labels_by_sku[sku])
                # Check CSV mapping
                elif sku_mapping and sku in sku_mapping:
                    mapped_sku = sku_mapping[sku]
                    if mapped_sku in labels_by_sku:
                        matching_labels.extend(labels_by_sku[mapped_sku])
            
            matched_groups.append((checklist, matching_labels))
        
        print(f"✓ Matched {len(matched_groups)} order(s)")
        
        # Create organized PDF with status banners
        output = io.BytesIO()
        writer = PdfWriter()
        
        for checklist, matching_labels in matched_groups:
            # Calculate total expected and actual
            total_expected = sum(checklist['sku_quantities'].values())
            total_actual = len(matching_labels)
            
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
        
        print(f"✓ Created organized PDF with {len(writer.pages)} pages")
        
        # Save to temp file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        temp_file.write(output.read())
        temp_file.close()
        
        # Send file
        return send_file(
            temp_file.name,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'organized_shipping_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
        )
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
