from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse
import io
import requests
from requests.auth import HTTPBasicAuth
import json
import time
import urllib.parse
import os
import csv
import re

root_url = os.environ["SPIRE_ROOT_URL"]
username = os.environ["SPIRE_USERNAME"]
password = os.environ["SPIRE_PASSWORD"]

headers = {"accept": "application/json"}

auth = HTTPBasicAuth(username, password)

# Headers
required_headers = {
        "PART NO", 
        "ORDER QTY"
        }

# Important info for the user!
message_1 = f"Name your column headers " + ", ".join(f"<strong>{header}</strong>" for header in required_headers) + ", <strong>DESCRIPTION</strong> and <strong>UNIT PRICE</strong>"
message_2 = f"Note that this program OVERWRITES the existing purchase order items with the content of the csv file!\n"

# FUNCTIONS

# Encodes url-unsafe characters and returns filter to append to url
def format_json_filter(json_filter):
  url_filter = json_filter
  filter_json = json.dumps(url_filter)
  url_safe_json = urllib.parse.quote_plus(filter_json)
  return f"filter={url_safe_json}"

# Interprets the input as a Spire PO number and creates the request url
def process_po_number(no):
    # PO numbers are always 10 digits, so pad the input with 0's
    po_number = no.zfill(10)
    po_filter = format_json_filter({"number": po_number})
    url = f"{root_url}/purchasing/orders/?{po_filter}"
    return {"po_number": no, "url": url}

# Find the entered PO 
def find_po(url):
    response = requests.get(url, headers=headers, auth=auth)
    if response.status_code != 200:
        print(f"Could not get PO {po_no}, Status code: {response.status_code}")
        return []
    else: 
        response_json = response.json()
        if not response_json["records"]:
            print("No results found. Double-check the PO exists and is active")
            return []
        else:
            po = response_json["records"][0]
            return po

# Creates item in inventory
def create_inventory_item(part_no, description, cost):
    url = f"{root_url}/inventory/items/"
    payload = {
        "pricing": { "EA": { "sellPrices": [round(cost/0.55, 2)] } },
        "partNo": part_no,
        "description": description,
        "whse": "00",
        "currentCost": cost
    }
    response = requests.post(url, json=payload, headers=headers, auth=auth)
    if response.status_code != 201:
        print(f"Failed to create inventory item {part_no}, status code: {response.status_code}\n{response.text}")
        return None
    else:
        return response.text

# Checks if the given part no exists in warehouse 00
def item_exists(part_no):
    part_no_filter = format_json_filter({"whse": "00", "partNo": part_no.upper()})
    url = f"{root_url}/inventory/items/?{part_no_filter}"
    response = requests.get(url, headers=headers, auth=auth)
    if response.status_code == 200 and response.json()["records"] != []:
        return True
    else:
        return False

# Extract float values from cells with unclean data
def clean_numeric(value: str):
    if not value:
        return None
    # Remove everything except digits and decimal points
    cleaned = re.sub(r'[^\d.]', '', value)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None

# Process line item
def process_line_item(row, headers_map, create_inventory: bool, vendor_no):
    try:
        part_no = row[headers_map["PART NO"]].strip()
        raw_qty = row[headers_map["ORDER QTY"]].strip()
        
        # Safe extraction for optional fields
        raw_price = row[headers_map["UNIT PRICE"]].strip() if "UNIT PRICE" in headers_map else None
        description = row[headers_map["DESCRIPTION"]].strip() if "DESCRIPTION" in headers_map else ""

        # Clean numeric values
        order_qty = clean_numeric(raw_qty)
        unit_price = clean_numeric(raw_price) if raw_price else None

        if order_qty is None:
            raise ValueError(f"Invalid quantity: {raw_qty}")

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error geting values for {part_no}: {e}") 

    # Inventory logic
    if create_inventory and not item_exists(part_no):
        if description:
            try:
                create_inventory_item(part_no, description, unit_price, vendor_no)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Error creating {part_no}: {e}")
        else:
            print(f"Warning: {part_no} needs a description to be created!")

    # Construct item payload
    item = {
        "inventory": {
            "whse": "00",
            "partNo": part_no
        },
        "orderQty": order_qty
    }
    
    if unit_price: item["unitPrice"] = unit_price
    if description: item["description"] = description
    
    return item

# Function to create the payload from the csv file
def create_payload(csv_file: UploadFile, required_headers, create_inventory: bool, vendor_no):
    base_payload = {
        "items": []
    }
    with io.TextIOWrapper(csv_file.file, encoding="utf-8-sig", newline="") as file:
        csv_file = csv.reader(file)

        headers = next(csv_file)
        headers_map = {header.upper(): i for i, header in enumerate(headers)}

        for header in required_headers:
            if header not in headers_map:
                raise HTTPException(status_code=400, detail=f"Missing {header} column") 
 
        for line in csv_file:
            if not any(line): continue  # Skip empty lines
            processed_item = process_line_item(line, headers_map, create_inventory, vendor_no)
            base_payload["items"].append(processed_item)
    return base_payload

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def upload_form():
    return f"""
    <html>
        <body>
            <h2>PO Import</h2>
            <p>{message_1}<br>{message_2}</p>
            <form action="/upload/" method="post" enctype="multipart/form-data">
                PO Number: <input type="text" name="po_number"><br>
                Upload a file: <input type="file" name="file"><br>
                Create inventory items if not found<input type="checkbox" name="create_inventory" /><br><br>
                <input type="submit" value="Submit">
            </form>
        </body>
    </html>
    """

@app.post("/upload/")
async def upload_file(po_number: str = Form(), file: UploadFile = File(), create_inventory: bool = Form(None)):
    # Form validation
    if po_number == "":
        raise HTTPException(status_code=422, detail="PO number is required")
    
    if not file.filename:
        raise HTTPException(status_code=422, detail="File is required")

    processed_po_no = process_po_number(po_number)
    po = find_po(processed_po_no["url"])

    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    
    po_id = po["id"]
    put_url = f"{root_url}/purchasing/orders/{po_id}"

    payload = create_payload(file, required_headers, create_inventory)
    response = requests.put(put_url, json=payload, headers=headers, auth=auth)

    if response.status_code == 200:
        return response.json()
    else:
        message = f"Failed to update PO: {response.text}"
        raise HTTPException(status_code=response.status_code, detail=message)
