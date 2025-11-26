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
    
    # Pattern 1: N5-96TU-TT9Z style (Letter+Digit or 2 Letters/Digits - 4 chars - 4 chars)
    # Matches: N5-96TU-TT9Z, U2-5YVZ-Q8TC, S4-6D0J-DNSB, L4-KTDG-ZIY6, etc.
    pattern1 = re.findall(r'\b[A-Z0-9]{2}-[A-Z0-9]{4}-[A-Z0-9]{4}\b', text)
    skus.extend(pattern1)
    
    # Pattern 2: B0090IFLG6 style (Amazon ASIN)
    pattern2 = re.findall(r'\bB[A-Z0-9]{9,10}\b', text)
    skus.extend(pattern2)
    
    # Pattern 3: Longer format like N7-KJ7T-EVIN (catches variations)
    pattern3 = re.findall(r'\b[A-Z][0-9]-[A-Z0-9]{4}-[A-Z0-9]{4}\b', text)
    skus.extend(pattern3)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_skus = []
    for sku in skus:
        if sku not in seen:
            seen.add(sku)
            unique_skus.append(sku)
    
    return unique_skus

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
        
        # Draw status box below header/barcode area
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

def create_summary_page(matched_groups, unmatched_labels, total_labels, start_time, width=612, height=792):
    """Create a comprehensive summary page"""
    import time
    processing_time = time.time() - start_time
    
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height))
    
    # Background
    c.setFillColor(colors.white)
    c.rect(0, 0, width, height, fill=True, stroke=False)
    
    # Header section
    c.setFillColor(colors.HexColor('#667eea'))
    c.rect(0, height - 120, width, 120, fill=True, stroke=False)
    
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(width / 2, height - 50, "📦 USMANI WHOLESALE")
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(width / 2, height - 75, "DAILY SHIPPING SUMMARY REPORT")
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - 95, datetime.now().strftime("%B %d, %Y - %I:%M %p"))
    
    y = height - 150
    
    # Processing Summary Box
    c.setFillColor(colors.HexColor('#F0F4FF'))
    c.setStrokeColor(colors.HexColor('#667eea'))
    c.setLineWidth(2)
    c.rect(40, y - 80, width - 80, 75, fill=True, stroke=True)
    
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, y - 25, "📊 PROCESSING SUMMARY")
    c.setFont("Helvetica", 11)
    c.drawString(80, y - 45, f"• Total Checklists: {len(matched_groups)}")
    c.drawString(80, y - 60, f"• Total Labels: {total_labels}")
    c.drawString(80, y - 75, f"• Processing Time: {processing_time:.1f} seconds")
    
    y -= 110
    
    # Matched Orders Section
    c.setFillColor(colors.HexColor('#28a745'))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"✅ MATCHED ORDERS: {len(matched_groups)}")
    
    y -= 25
    c.setFont("Helvetica", 10)
    
    page_num = 2  # Summary is page 1, content starts at page 2
    
    for i, (checklist, labels) in enumerate(matched_groups, 1):
        if y < 200:  # If running out of space
            break
            
        expected = sum(checklist['sku_quantities'].values())
        # Sum quantities instead of counting pages
        actual = sum(label.get('qty', 1) for label in labels)
        diff = actual - expected
        
        # Extract PO number from checklist text
        po_match = re.search(r'PO.*?(\d{5,})', checklist['text'])
        po_num = po_match.group(1) if po_match else "Unknown"
        
        # Extract supplier
        supplier_match = re.search(r'Supplier:\s*([^\n]+)', checklist['text'])
        supplier = supplier_match.group(1).strip() if supplier_match else "Unknown"
        
        # Status color and text
        if diff == 0:
            status_color = colors.HexColor('#28a745')
            status_text = f"✓ {actual}/{expected} Labels - Perfect Match"
        elif diff > 0:
            status_color = colors.HexColor('#FF9800')
            status_text = f"⚠ {actual}/{expected} Labels - {diff} EXTRA"
        else:
            status_color = colors.HexColor('#dc3545')
            status_text = f"✗ {actual}/{expected} Labels - {abs(diff)} MISSING"
        
        # Draw order info
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(60, y, f"{i}. PO-{po_num} ({supplier})")
        
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor('#666666'))
        skus = ", ".join(checklist['skus'][:2])  # Show first 2 SKUs
        if len(checklist['skus']) > 2:
            skus += f" +{len(checklist['skus']) - 2} more"
        c.drawString(75, y - 12, f"SKU: {skus}")
        
        c.setFillColor(status_color)
        c.drawString(75, y - 24, f"Status: {status_text}")
        
        c.setFillColor(colors.HexColor('#666666'))
        end_page = page_num + len(labels)
        c.drawString(75, y - 36, f"Pages: {page_num}-{end_page}")
        
        page_num = end_page + 1
        y -= 50
    
    if len(matched_groups) > 4:
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor('#666666'))
        c.drawString(60, y, f"... and {len(matched_groups) - 4} more orders (see full PDF)")
        y -= 20
    
    y -= 20
    
    # Unmatched Labels Section
    if unmatched_labels:
        c.setFillColor(colors.HexColor('#FF9800'))
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, y, f"⚠️ UNMATCHED LABELS: {len(unmatched_labels)}")
        y -= 20
        
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.black)
        for label in unmatched_labels[:3]:
            order_match = re.search(r'Order.*?(\d{3}-\d{7}-\d{7})', label.get('text', ''))
            order_num = order_match.group(1) if order_match else "Unknown"
            c.drawString(60, y, f"• SKU: {label['sku'] or 'None'} (Order #{order_num})")
            y -= 15
        
        if len(unmatched_labels) > 3:
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.HexColor('#666666'))
            c.drawString(60, y, f"... and {len(unmatched_labels) - 3} more (see end of PDF)")
            y -= 15
        
        y -= 10
    
    # Action Items Section
    y -= 20
    c.setFillColor(colors.HexColor('#667eea'))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "🎯 ACTION ITEMS")
    y -= 25
    
    # High priority items (missing labels)
    missing_orders = [(checklist, labels) for checklist, labels in matched_groups 
                      if sum(label.get('qty', 1) for label in labels) < sum(checklist['sku_quantities'].values())]
    
    if missing_orders:
        c.setFillColor(colors.HexColor('#dc3545'))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(60, y, "⚠️ HIGH PRIORITY:")
        y -= 15
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.black)
        for checklist, labels in missing_orders[:2]:
            po_match = re.search(r'PO.*?(\d{5,})', checklist['text'])
            po_num = po_match.group(1) if po_match else "Unknown"
            actual_qty = sum(label.get('qty', 1) for label in labels)
            expected_qty = sum(checklist['sku_quantities'].values())
            missing = expected_qty - actual_qty
            c.drawString(75, y, f"• PO-{po_num}: MISSING {missing} label(s) - DO NOT SHIP")
            y -= 15
        y -= 10
    
    # Review needed items (extras or unmatched)
    extra_orders = [(checklist, labels) for checklist, labels in matched_groups 
                    if sum(label.get('qty', 1) for label in labels) > sum(checklist['sku_quantities'].values())]
    
    if extra_orders or unmatched_labels:
        c.setFillColor(colors.HexColor('#FF9800'))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(60, y, "📋 REVIEW NEEDED:")
        y -= 15
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.black)
        
        for checklist, labels in extra_orders[:2]:
            po_match = re.search(r'PO.*?(\d{5,})', checklist['text'])
            po_num = po_match.group(1) if po_match else "Unknown"
            c.drawString(75, y, f"• PO-{po_num}: Verify extra labels before shipping")
            y -= 15
        
        if unmatched_labels:
            c.drawString(75, y, f"• {len(unmatched_labels)} unmatched labels need checklist assignment")
            y -= 15
        y -= 10
    
    # Ready to ship
    perfect_orders = [(checklist, labels) for checklist, labels in matched_groups 
                      if sum(label.get('qty', 1) for label in labels) == sum(checklist['sku_quantities'].values())]
    
    if perfect_orders:
        c.setFillColor(colors.HexColor('#28a745'))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(60, y, f"✅ READY TO SHIP: {len(perfect_orders)} order(s)")
        y -= 15
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.black)
        for checklist, labels in perfect_orders[:3]:
            po_match = re.search(r'PO.*?(\d{5,})', checklist['text'])
            po_num = po_match.group(1) if po_match else "Unknown"
            c.drawString(75, y, f"• PO-{po_num} (Perfect match)")
            y -= 15
    
    # Footer
    c.setFillColor(colors.HexColor('#999999'))
    c.setFont("Helvetica", 9)
    c.drawCentredString(width / 2, 30, "Generated by PDF Organizer v2.0")
    c.drawCentredString(width / 2, 18, "https://pdf-organizer-evoe.onrender.com")
    
    c.save()
    buffer.seek(0)
    return buffer

def create_unmatched_separator_page(unmatched_count, width=612, height=792):
    """Create a warning page for labels without checklists"""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height))
    
    # Background
    c.setFillColor(colors.HexColor('#FFF3CD'))
    c.rect(0, 0, width, height, fill=True, stroke=False)
    
    # Warning banner
    banner_height = 120
    banner_y = (height - banner_height) / 2
    
    c.setFillColor(colors.HexColor('#FF9800'))
    c.setStrokeColor(colors.HexColor('#F57C00'))
    c.setLineWidth(3)
    c.rect(50, banner_y, width - 100, banner_height, fill=True, stroke=True)
    
    # Warning icon and text
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 48)
    c.drawCentredString(width / 2, banner_y + 70, "⚠️")
    
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width / 2, banner_y + 45, "LABELS WITHOUT CHECKLIST")
    
    c.setFont("Helvetica", 16)
    c.drawCentredString(width / 2, banner_y + 20, f"{unmatched_count} label(s) found with no matching checklist")
    
    # Instructions
    c.setFillColor(colors.HexColor('#856404'))
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width / 2, banner_y - 40, "ACTION REQUIRED:")
    
    c.setFont("Helvetica", 12)
    instructions = [
        "• Check if checklists are missing or delayed",
        "• Verify SKUs match between checklist and labels",
        "• Hold these shipments until checklists arrive",
        "• Update daily order report if needed"
    ]
    
    y_pos = banner_y - 70
    for instruction in instructions:
        c.drawString(100, y_pos, instruction)
        y_pos -= 25
    
    c.save()
    buffer.seek(0)
    return buffer

@app.route('/')
def home():
    """Serve the main HTML page"""
    try:
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
        
        # Parse CSV mapping
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
                    
                    skus = extract_skus_from_text(text)
                    print(f"   - Found SKUs: {skus}")
                    
                    if not skus:
                        print(f"   ⚠️  No SKUs found on page {i+1}")
                        continue
                    
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
            error_msg = "No SKUs found in checklist PDF"
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
                    
                    skus = extract_skus_from_text(text)
                    sku = skus[0] if skus else None
                    
                    # Extract quantity from label
                    qty_pattern = r'Qty[:\s]+(\d+)'
                    qty_match = re.search(qty_pattern, text, re.IGNORECASE)
                    qty = int(qty_match.group(1)) if qty_match else 1
                    
                    if sku:
                        print(f"   Label {i+1}: SKU = {sku}, Qty = {qty}")
                    else:
                        print(f"   Label {i+1}: No SKU found, Qty = {qty}")
                    
                    labels.append({
                        'page': pypdf_page,
                        'page_num': i + 1,
                        'sku': sku,
                        'qty': qty,
                        'text': text  # Store text here for summary page
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
        
        # Track which labels were matched GLOBALLY (for unmatched detection)
        matched_label_pages = set()
        matched_groups = []
        
        for checklist in checklists:
            matching_labels = []
            # Track which labels we've already added to THIS checklist
            added_to_this_checklist = set()
            
            for sku in checklist['skus']:
                # Direct match
                if sku in labels_by_sku:
                    count = len(labels_by_sku[sku])
                    print(f"   ✓ {sku}: Found {count} matching label(s)")
                    for label in labels_by_sku[sku]:
                        # Only add if not already added to this checklist
                        if label['page_num'] not in added_to_this_checklist:
                            matching_labels.append(label)
                            matched_label_pages.add(label['page_num'])
                            added_to_this_checklist.add(label['page_num'])
                # Check CSV mapping
                elif sku_mapping and sku in sku_mapping:
                    mapped_sku = sku_mapping[sku]
                    if mapped_sku in labels_by_sku:
                        count = len(labels_by_sku[mapped_sku])
                        print(f"   ✓ {sku} → {mapped_sku}: Found {count} matching label(s) via mapping")
                        for label in labels_by_sku[mapped_sku]:
                            # Only add if not already added to this checklist
                            if label['page_num'] not in added_to_this_checklist:
                                matching_labels.append(label)
                                matched_label_pages.add(label['page_num'])
                                added_to_this_checklist.add(label['page_num'])
                    else:
                        print(f"   ✗ {sku} → {mapped_sku}: No matching labels found")
                else:
                    print(f"   ✗ {sku}: No matching labels found")
            
            matched_groups.append((checklist, matching_labels))
        
        # Find unmatched labels
        unmatched_labels = [label for label in labels if label['page_num'] not in matched_label_pages]
        
        print(f"\n✓ Matched {len(matched_groups)} order(s)")
        print(f"⚠️  Found {len(unmatched_labels)} unmatched label(s)")
        
        # Create organized PDF
        print("\n🔍 STEP 4: Creating organized PDF...")
        try:
            import time
            start_time = time.time()
            
            output = io.BytesIO()
            writer = PdfWriter()
            
            # Add summary page FIRST
            print("   Creating summary page...")
            summary_buffer = create_summary_page(matched_groups, unmatched_labels, len(labels), start_time)
            summary_reader = PdfReader(summary_buffer)
            writer.add_page(summary_reader.pages[0])
            print("   ✓ Summary page added")
            
            # Add matched groups first
            for checklist, matching_labels in matched_groups:
                # Calculate total expected from checklist
                total_expected = sum(checklist['sku_quantities'].values())
                
                # Calculate total actual by SUMMING label quantities (not counting pages!)
                total_actual = sum(label.get('qty', 1) for label in matching_labels)
                
                print(f"   Checklist page {checklist['page_num']}: Expected {total_expected}, Found {total_actual} (from {len(matching_labels)} label pages)")
                
                checklist_page = checklist['page']
                mediabox = checklist_page.mediabox
                width = float(mediabox.width)
                height = float(mediabox.height)
                
                overlay_buffer = create_status_overlay(total_expected, total_actual, width, height)
                overlay_reader = PdfReader(overlay_buffer)
                
                checklist_page.merge_page(overlay_reader.pages[0])
                writer.add_page(checklist_page)
                
                for label in matching_labels:
                    writer.add_page(label['page'])
            
            # Add unmatched labels section if any exist
            if unmatched_labels:
                print(f"\n   Adding separator page for {len(unmatched_labels)} unmatched labels...")
                separator_buffer = create_unmatched_separator_page(len(unmatched_labels))
                separator_reader = PdfReader(separator_buffer)
                writer.add_page(separator_reader.pages[0])
                
                print(f"   Adding {len(unmatched_labels)} unmatched labels...")
                for label in unmatched_labels:
                    writer.add_page(label['page'])
                    print(f"   - Added label page {label['page_num']} (SKU: {label['sku'] or 'None'})")
            
            writer.write(output)
            output.seek(0)
            
            total_pages = len(writer.pages)
            print(f"\n✓ Created organized PDF with {total_pages} pages")
            print(f"  - Matched sections: {len(matched_groups)}")
            print(f"  - Unmatched labels: {len(unmatched_labels)}")
            
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
            temp_file.write(output.read())
            temp_file.close()
            
            print(f"✓ Saved to: {temp_file.name}")
            print("="*50)
            print("🎉 SUCCESS!")
            print("="*50 + "\n")
            
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
