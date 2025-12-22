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

# Returns URL filters for Spire's API given the key and value
def format_json(key, value):
    url_filter = {key:value}
    filter_json = json.dumps(url_filter)
    url_safe_json = urllib.parse.quote_plus(filter_json)
    return f"filter={url_safe_json}"

# Interprets the input as a Spire PO number and creates the request url
def process_po_number(no):
    # PO numbers are always 10 digits, so pad the input with 0's
    po_number = no.zfill(10)
    po_filter = format_json("number", po_number)
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

def item_exists(part_no):
    part_no_filter = format_json("partNo", part_no.upper())
    url = f"{root_url}/inventory/items/?{part_no_filter}"
    response = requests.get(url, headers=headers, auth=auth)
    if response.status_code == 200 and response.json()["records"] != []:
        return True
    else:
        return False

# Function to create the payload from the csv file
def create_payload(csv_file: UploadFile, required_headers, create_inventory: bool):
    base_payload = {
        "items": []
    }
    with io.TextIOWrapper(csv_file.file, encoding="utf-8", newline="") as file:
        csv_file = csv.reader(file)

        headers = next(csv_file)
        uppercase_headers = {header.upper(): i for i, header in enumerate(headers)}

        for header in required_headers:
            if header not in uppercase_headers:
                raise HTTPException(status_code=422, detail=f"Missing {header} column") 
 
        for line_no, lines in enumerate(csv_file):
            # UOM autopopulates with stock UOM
            try:
                part_no = lines[uppercase_headers["PART NO"]].strip()
                order_qty = lines[uppercase_headers["ORDER QTY"]].strip()
                unit_price = float(lines[uppercase_headers.get("UNIT PRICE")].strip()) if "UNIT PRICE" in uppercase_headers else None
                description = lines[uppercase_headers.get("DESCRIPTION")].strip() if "DESCRIPTION" in uppercase_headers else ""
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Error geting values for {part_no}: {e}") 
            
            if create_inventory and not item_exists(part_no):
                if description: 
                    print(f"Creating inventory item {part_no}")
                    try: 
                        create_inventory_item(part_no, description, unit_price)
                    except Exception as e:
                        raise HTTPException(status_code=400, detail=f"Error creating {part_no}: {e}") 
                else:
                    print(f"{part_no} needs a description to be created!")
            
            item = {
                "inventory": {
                    "whse": "00", # Default warehouse is 00
                    "partNo": part_no
                },
                "orderQty": order_qty
            }
            if unit_price:
                item["unitPrice"] = unit_price
            if description:
                item["description"] = description

            base_payload["items"].append(item)
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
