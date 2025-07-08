from flask import Flask, request, jsonify
from flask_cors import CORS
import pandas as pd
import os
from openpyxl import load_workbook
from openpyxl import Workbook
from datetime import datetime

# === Column Index Constants ===
COL_IDX_ITEM_ID = 11
COL_IDX_PRODUCT_NAME = 0
COL_IDX_UNIT_QTY = 9
COL_IDX_COST_PRICE = 2

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "uploaded_inventory"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# In-memory scanned data
temp_scanned_data = {}
excel_file_path = None  # Path to last uploaded file


@app.route("/", methods=["GET"])
def health_check():
    return "Flask backend is running!"


@app.route("/upload-excel", methods=["POST"])
def upload_excel():
    global temp_scanned_data, excel_file_path
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    original_filename = file.filename
    filename = "inventory" + datetime.now().strftime("_%Y-%m-%d_%H-%M-%S") + ".xlsx"
    excel_file_path = os.path.join(UPLOAD_FOLDER, filename)

    if original_filename.endswith(".xls"):
        temp_xls_path = os.path.join(UPLOAD_FOLDER, "temp_uploaded.xls")
        file.save(temp_xls_path)
        df = pd.read_excel(temp_xls_path, header=None)
        df.to_excel(excel_file_path, index=False, header=False)
        os.remove(temp_xls_path)
    else:
        file.save(excel_file_path)

    try:
        df = pd.read_excel(excel_file_path, header=None)  # Read with no header
        df = df.fillna(0)

        temp_scanned_data = {}
        for _, row in df.iterrows():
            try:
                item_id = str(row[COL_IDX_ITEM_ID]).strip().upper()
                if not item_id or item_id == '0':
                    continue  # Skip empty or invalid IDs

                product_name = row[COL_IDX_PRODUCT_NAME]
                expected_qty = int(float(row[COL_IDX_UNIT_QTY]))
                item_price = float(row[COL_IDX_COST_PRICE])

                temp_scanned_data[item_id] = {
                    "product_name": product_name,
                    "expected_qty": expected_qty,
                    "scanned_qty": 0,
                    "item_price": item_price,
                    "date": datetime.now()
                }
            except:
                continue  # Skip rows with conversion errors

        return jsonify({
            "message": "Excel file uploaded and processed successfully!",
            "items_loaded": len(temp_scanned_data)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/scan-item", methods=["POST"])
def scan_item():
    global temp_scanned_data
    data = request.get_json()
    item_id = str(data.get("item_id", "")).strip().upper()
    quantity = data.get("quantity")

    if item_id not in temp_scanned_data:
        return jsonify({"error": f"Item ID '{item_id}' not found in inventory."}), 404

    try:
        quantity = int(quantity)
    except:
        return jsonify({"error": "Quantity must be an integer."}), 400

    temp_scanned_data[item_id]["scanned_qty"] += quantity

    scanned = temp_scanned_data[item_id]
    scanned["variance"] = scanned["scanned_qty"] - scanned["expected_qty"]
    scanned["total_price"] = scanned["scanned_qty"] * scanned["item_price"]
    scanned["expected_total_price"] = scanned["expected_qty"] * scanned["item_price"]
    scanned["date"] = datetime.now()

    try:
        if not excel_file_path.endswith(".xlsx"):
            return jsonify({"error": "Only .xlsx files can be updated with scan results."}), 400

        wb = load_workbook(excel_file_path)
        if "Scan Results" not in wb.sheetnames:
            ws = wb.create_sheet("Scan Results")
            ws.append([
                "Scanned Product ID",
                "Product Name",
                "Expected Quantity",
                "Scanned Quantity",
                "Variance",
                "Total Price",
                "Expected Total Price"
            ])
        else:
            ws = wb["Scan Results"]

        found = False
        for row in ws.iter_rows(min_row=2):  # Skip header
            cell_value = str(row[0].value).strip().upper()
            if cell_value == item_id:
                row[2].value = scanned["expected_qty"]
                row[3].value = scanned["scanned_qty"]
                row[4].value = scanned["variance"]
                row[5].value = scanned["total_price"]
                row[6].value = scanned["expected_total_price"]
                found = True
                break

        if not found:
            ws.append([
                item_id,
                scanned["product_name"],
                scanned["expected_qty"],
                scanned["scanned_qty"],
                scanned["variance"],
                scanned["total_price"],
                scanned["expected_total_price"]
            ])

        wb.save(excel_file_path)

    except Exception as e:
        return jsonify({"error": f"Excel update failed: {str(e)}"}), 500

    return jsonify({
        "message": "Item scanned successfully",
        "item_id": item_id,
        "expected_qty": scanned["expected_qty"],
        "scanned_qty": scanned["scanned_qty"],
        "variance": scanned["variance"],
        "all_scanned_data": scanned_data_list()
    })


@app.route("/search-items", methods=["GET"])
def search_items():
    query = request.args.get("q", "").strip().lower()
    if not query:
        return jsonify([])

    results = []
    for item_id, data in temp_scanned_data.items():
        if query in str(data["product_name"]).lower():
            results.append({
                "item_id": item_id,
                "product_name": data["product_name"]
            })

    return jsonify(results)


@app.route("/scan-item-by-name", methods=["POST"])
def scan_item_by_name():
    global temp_scanned_data
    data = request.get_json()
    product_name = str(data.get("product_name", "")).strip()
    quantity = data.get("quantity")

    item_id = next((id for id, v in temp_scanned_data.items() if v["product_name"] == product_name), None)
    if not item_id:
        return jsonify({"error": f"Product name '{product_name}' not found in inventory."}), 404

    return scan_item()


@app.route("/get-scanned-summary", methods=["GET"])
def get_summary():
    return jsonify({"all_scanned_data": scanned_data_list()})


def scanned_data_list():
    data = [
        {
            "item_id": pid,
            "product_name": entry["product_name"],
            "expected_qty": entry["expected_qty"],
            "scanned_qty": entry["scanned_qty"],
            "variance": entry["scanned_qty"] - entry["expected_qty"],
            "item_price": entry["item_price"],
            "total_price": entry.get("total_price", 0),
            "expected_total_price": entry.get("expected_total_price", 0),
            "date": entry["date"].isoformat()
        }
        for pid, entry in temp_scanned_data.items()
    ]

    data.sort(key=lambda x: datetime.fromisoformat(x["date"]), reverse=True)

    return data


@app.route("/delete-uploaded", methods=["DELETE"])
def delete_uploaded():
    global temp_scanned_data, excel_file_path
    temp_scanned_data = {}
    return jsonify({"message": "Uploaded inventory data deleted successfully."})


if __name__ == "__main__":
    app.run(debug=True)