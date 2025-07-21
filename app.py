from flask import Flask, request, jsonify, send_file, after_this_request, Response
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import pandas as pd
import os
from openpyxl import load_workbook, Workbook
from datetime import datetime, timezone
import threading
import logging
import io # REQUIRED for in-memory file handling
# tempfile is no longer strictly needed for download_excel, but kept if used elsewhere.
# import tempfile # REQUIRED for TemporaryDirectory

# === Logging Setup ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === App Configuration ===
app = Flask(__name__)
CORS(app)

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///inventory.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

UPLOAD_FOLDER = "uploaded_inventory"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# === Column Index Constants (KEPT EXACTLY AS PER YOUR REQUEST) ===
# Since you have no headers, these are the 0-indexed column numbers in your raw Excel data.
COL_IDX_PRODUCT_NAME = 0
COL_IDX_COST_PRICE = 2
COL_IDX_SELLING_PRICE = 4
COL_IDX_UNIT_QTY = 9 # If this column contains non-numeric text like 'وحدة كبرى', it will default to 0.
COL_IDX_ITEM_ID = 11

# === Database Model ===
class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    product_name = db.Column(db.String(255), nullable=False)
    expected_qty = db.Column(db.Integer, default=0)
    scanned_qty = db.Column(db.Integer, default=0)
    item_price = db.Column(db.Float, default=0.0)
    item_selling_price = db.Column(db.Float, default=0.0)
    last_scanned_date = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<InventoryItem {self.item_id}>"

    def to_dict(self):
        return {
            "item_id": self.item_id,
            "product_name": self.product_name,
            "expected_qty": self.expected_qty,
            "scanned_qty": self.scanned_qty,
            "variance": self.scanned_qty - self.expected_qty,
            "item_price": self.item_price,
            "selling_price": self.item_selling_price,
            "total_price": round(self.scanned_qty * self.item_price, 2),
            "expected_total_price": round(self.expected_qty * self.item_price, 2),
            "date": self.last_scanned_date.isoformat() if self.last_scanned_date else None
        }

# === Global variable for the path to the last uploaded Excel file ===
excel_file_path = None
excel_file_path_lock = threading.Lock()

# --- Database Initialization ---
@app.before_request
def create_tables():
    with app.app_context():
        db.create_all()

# === API Endpoints ===

@app.route("/", methods=["GET"])
def health_check():
    return "Flask backend is running with database integration!"

@app.route("/upload-excel", methods=["POST"])
def upload_excel():
    global excel_file_path
    
    file = request.files.get("file")
    if not file:
        logger.warning("No file uploaded for /upload-excel")
        return jsonify({"error": "No file uploaded"}), 400

    original_filename = file.filename
    new_excel_filename = "inventory_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".xlsx"
    current_excel_file_storage_path = os.path.join(UPLOAD_FOLDER, new_excel_filename)

    try:
        if original_filename.endswith(".xls"):
            temp_xls_path = os.path.join(UPLOAD_FOLDER, "temp_uploaded.xls")
            file.save(temp_xls_path)
            # Read .xls with no header, then save as .xlsx with no header
            df = pd.read_excel(temp_xls_path, header=None) 
            df.to_excel(current_excel_file_storage_path, index=False, header=False)
            os.remove(temp_xls_path)
        else:
            file.save(current_excel_file_storage_path)
        
        with excel_file_path_lock:
            excel_file_path = current_excel_file_storage_path
            
    except Exception as e:
        logger.error(f"Error saving uploaded file: {e}", exc_info=True)
        return jsonify({"error": f"Failed to save file: {str(e)}"}), 500

    items_loaded_count = 0
    try:
        # MODIFIED: Use header=None because you have no headers.
        # This means COL_IDX constants directly refer to the 0-indexed column numbers.
        df = pd.read_excel(current_excel_file_storage_path, header=None) 
        df = df.fillna('') # Fill NaN with empty string for safer processing

        # --- RE-IMPLEMENTED LOGIC TO PRESERVE SCANNED DATA ---
        # 1. Fetch all existing items from the DB into a dictionary for quick lookup
        existing_items_map = {item.item_id: item for item in db.session.query(InventoryItem).all()}
        
        item_ids_from_excel = set() # To track items present in the new Excel

        for index, row in df.iterrows():
            try:
                # Access columns by their 0-indexed positions directly
                item_id = str(row[COL_IDX_ITEM_ID]).strip().upper()
                if not item_id or item_id == '0':
                    continue

                product_name = str(row[COL_IDX_PRODUCT_NAME]).strip()
                
                # Robust conversion with try-except for expected_qty
                expected_qty = 0
                # Use .get() with default for robustness if column might be out of bounds for some rows
                expected_qty_val = row.get(COL_IDX_UNIT_QTY) 
                if pd.notna(expected_qty_val) and str(expected_qty_val).strip():
                    try:
                        expected_qty = int(float(expected_qty_val))
                    except ValueError:
                        logger.warning(f"Could not convert expected_qty '{expected_qty_val}' for item {item_id} (row {index+1}, col {COL_IDX_UNIT_QTY}). Defaulting to 0.")
                        expected_qty = 0 # Default to 0 if conversion fails

                # Robust conversion with try-except for item_price
                item_price = 0.0
                item_price_val = row.get(COL_IDX_COST_PRICE)
                if pd.notna(item_price_val) and str(item_price_val).strip():
                    try:
                        item_price = float(item_price_val)
                    except ValueError:
                        logger.warning(f"Could not convert item_price '{item_price_val}' for item {item_id} (row {index+1}, col {COL_IDX_COST_PRICE}). Defaulting to 0.0.")
                        item_price = 0.0

                # Robust conversion with try-except for item_selling_price
                item_selling_price = 0.0
                item_selling_price_val = row.get(COL_IDX_SELLING_PRICE)
                if pd.notna(item_selling_price_val) and str(item_selling_price_val).strip():
                    try:
                        item_selling_price = float(item_selling_price_val)
                    except ValueError:
                        logger.warning(f"Could not convert item_selling_price '{item_selling_price_val}' for item {item_id} (row {index+1}, col {COL_IDX_SELLING_PRICE}). Defaulting to 0.0.")
                        item_selling_price = 0.0

                item_ids_from_excel.add(item_id) # Mark this item_id as present in the new Excel

                if item_id in existing_items_map:
                    # Item exists in DB: Update its expected properties, PRESERVE scanned_qty and last_scanned_date
                    item = existing_items_map[item_id]
                    item.product_name = product_name
                    item.expected_qty = expected_qty
                    item.item_price = item_price
                    item.item_selling_price = item_selling_price
                    # item.scanned_qty and item.last_scanned_date are NOT touched here
                    db.session.add(item) # Add to session for update
                else:
                    # New item from Excel: Add to DB with scanned_qty 0
                    new_item = InventoryItem(
                        item_id=item_id,
                        product_name=product_name,
                        expected_qty=expected_qty,
                        scanned_qty=0, # New items start with 0 scanned
                        item_price=item_price, 
                        item_selling_price=item_selling_price,
                        last_scanned_date=None # New items have no last scanned date
                    )
                    db.session.add(new_item) # Add to session for insert
                items_loaded_count += 1
            except Exception as row_error:
                # Log the row data that caused the error for easier debugging
                logger.warning(f"Skipping row {index+1} due to error: {row_error} - Row data: {row.to_dict() if hasattr(row, 'to_dict') else row}", exc_info=True)
                continue

        # Handle items that were in the DB but are NOT in the new Excel file
        # Set their expected_qty to 0 to reflect they are no longer expected,
        # but preserve their scanned_qty for historical context.
        for item_id, item_obj in existing_items_map.items():
            if item_id not in item_ids_from_excel:
                item_obj.expected_qty = 0 # Mark as no longer expected
                db.session.add(item_obj) # Mark for update
                logger.info(f"Item {item_id} from previous inventory not found in new Excel. Setting expected_qty to 0.")
        
        db.session.commit() # Commit all changes (updates and inserts)

        logger.info(f"Excel file uploaded and {items_loaded_count} items processed into database (updated/inserted).")
        return jsonify({
            "message": "Excel file uploaded and processed successfully! Scanned data preserved.",
            "items_loaded": items_loaded_count
        })

    except Exception as e:
        db.session.rollback() # Rollback if any error occurs during DB population
        logger.error(f"Error processing Excel or populating database: {e}", exc_info=True)
        return jsonify({"error": f"Failed to process Excel file: {str(e)}"}), 500


@app.route("/scan-item", methods=["POST"])
def scan_item():
    data = request.get_json()
    item_id = str(data.get("item_id", "")).strip().upper()
    quantity_input = data.get("quantity")

    if not item_id:
        return jsonify({"error": "Item ID cannot be empty."}), 400

    try:
        quantity = int(quantity_input)
    except (ValueError, TypeError):
        return jsonify({"error": "Quantity must be a valid integer."}), 400

    try:
        item = db.session.query(InventoryItem).filter_by(item_id=item_id).first()

        if not item:
            logger.warning(f"Scan request for unknown Item ID: {item_id}")
            return jsonify({"error": f"Item ID '{item_id}' not found in inventory."}), 404

        item.scanned_qty += quantity
        item.last_scanned_date = datetime.now(timezone.utc)

        db.session.commit()

        logger.info(f"Scanned item: {item_id}, Quantity: {quantity}. New scanned_qty: {item.scanned_qty}")

        return jsonify({
            "message": "Item scanned successfully",
            "item_id": item.item_id,
            "expected_qty": item.expected_qty,
            "scanned_qty": item.scanned_qty,
            "variance": item.scanned_qty - item.expected_qty,
            "item_price": item.item_price,
            "selling_price": item.item_selling_price,
            "total_price": round(item.scanned_qty * item.item_price, 2),
            "expected_total_price": round(item.expected_qty * item.item_price, 2),
            "date": item.last_scanned_date.isoformat()
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error scanning item {item_id}: {e}", exc_info=True)
        return jsonify({"error": f"Failed to scan item: {str(e)}"}), 500

@app.route("/get-scanned-summary", methods=["GET"])
def get_summary():
    all_items = db.session.query(InventoryItem).all()
    
    data_list = [item.to_dict() for item in all_items]

    data_list.sort(key=lambda x: datetime.fromisoformat(x["date"]) if x["date"] else datetime.min, reverse=True)
    
    logger.info(f"Returning summary of {len(data_list)} items, sorted by last scanned date.")
    return jsonify({"all_scanned_data": data_list})

@app.route("/download-excel", methods=["GET"])
def download_excel():
    global excel_file_path

    with excel_file_path_lock:
        current_excel_path = excel_file_path

    if not current_excel_path or not os.path.exists(current_excel_path):
        logger.warning("Download request for non-existent Excel file.")
        return jsonify({"error": "No Excel file found to download. Please upload one first."}), 404

    # NEW: Use io.BytesIO for in-memory Excel generation
    excel_buffer = io.BytesIO()
    output_filename = "inventory_report_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".xlsx"
    
    logger.info(f"Attempting to generate Excel in memory: {output_filename}")

    try:
        # Query only items that have been scanned (scanned_qty > 0)
        all_inventory_items = db.session.query(InventoryItem).filter(InventoryItem.scanned_qty > 0).all()
        
        report_data = []
        for item in all_inventory_items:
            report_data.append({
                "Item ID": item.item_id,
                "Product Name": item.product_name,
                "Expected Quantity": item.expected_qty,
                "Scanned Quantity": item.scanned_qty,
                "Variance": item.scanned_qty - item.expected_qty,
                "Item Price": item.item_price,
                "Selling Price": item.item_selling_price,
                "Total Scanned Value": round(item.scanned_qty * item.item_price, 2),
                "Expected Total Value": round(item.expected_qty * item.item_price, 2),
                "Last Scanned Date": item.last_scanned_date.isoformat() if item.last_scanned_date else "N/A"
            })
        
        df_report = pd.DataFrame(report_data)
        # Write to the in-memory buffer
        df_report.to_excel(excel_buffer, index=False, engine='openpyxl') # Specify engine for openpyxl
        
        # Seek to the beginning of the buffer before sending
        excel_buffer.seek(0)
        
        logger.info(f"Excel report successfully generated in memory: {output_filename}")

        # send_file will read from the in-memory buffer
        response = send_file(
            excel_buffer,
            as_attachment=True,
            download_name=output_filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        logger.info(f"Returning file response for: {output_filename}")
        return response

    except Exception as e:
        logger.error(f"Error generating or sending Excel report: {e}", exc_info=True)
        return jsonify({"error": f"Failed to generate or download Excel report: {str(e)}"}), 500


@app.route("/delete-uploaded", methods=["DELETE"])
def delete_uploaded():
    try:
        db.session.query(InventoryItem).update({
            InventoryItem.scanned_qty: 0,
            InventoryItem.last_scanned_date: None
        })
        db.session.commit()
        logger.info("All scanned quantities reset to 0. Master inventory preserved.")
        return jsonify({"message": "Scan session reset successfully. Master inventory preserved."})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error resetting scanned quantities: {e}", exc_info=True)
        return jsonify({"error": f"Failed to reset scanned quantities: {str(e)}"}), 500


# if __name__ == "__main__":
#     app.run(debug=True)
