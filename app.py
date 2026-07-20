from flask import Flask, request, jsonify, send_from_directory
from anthropic import Anthropic
import os
import base64
import json
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# Your EPA emission factors (CAMX - California)
CAMX_FACTORS = {
    "co2_lb_per_mwh": 436.655,
    "ch4_lb_per_mwh": 0.025,
    "n2o_lb_per_mwh": 0.003,
}

LB_TO_KG = 0.45359237
KG_TO_TONNES = 0.001

def _nullable(*types):
    return {"anyOf": [{"type": t} for t in types] + [{"type": "null"}]}

BILL_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": _nullable("string"),
        "account_number": _nullable("string"),
        "service_address": _nullable("string"),
        "statement_date": _nullable("string"),
        "electricity_kwh": _nullable("number"),
        "natural_gas_therms": _nullable("number"),
        "utility": _nullable("string"),
    },
    "required": [
        "company_name",
        "account_number",
        "service_address",
        "statement_date",
        "electricity_kwh",
        "natural_gas_therms",
        "utility",
    ],
    "additionalProperties": False,
}

EXTRACTION_PROMPT = """Extract the following from this utility bill:
- Company/Customer name
- Account number
- Service address
- Statement date (as YYYY-MM-DD)
- Electricity consumption in kWh for this billing period
- Natural gas consumption in therms for this billing period
- Utility company name (PG&E, SCE, etc.)

Report consumption as plain numbers with no units, commas, or currency symbols.
If the bill states usage in a different unit, convert it (1 CCF of natural gas
is approximately 1.037 therms; 1 MWh is 1000 kWh). Use null for any value that
is not present on the bill — do not guess."""


def extract_bill_data(file_base64, media_type):
    """Use Claude to extract bill data from a PDF or image utility bill."""
    # Plain text needs no vision — pass the bill through as text.
    if media_type == "text/plain":
        source_block = {
            "type": "text",
            "text": "Utility bill contents:\n\n"
            + base64.b64decode(file_base64).decode("utf-8", errors="replace"),
        }
    # PDFs must be sent as a `document` block; images as an `image` block.
    elif media_type == "application/pdf":
        source_block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": file_base64,
            },
        }
    else:
        source_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": file_base64,
            },
        }

    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        output_config={"format": {"type": "json_schema", "schema": BILL_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": [source_block, {"type": "text", "text": EXTRACTION_PROMPT}],
            }
        ],
    )

    if message.stop_reason == "max_tokens":
        raise ValueError("Extraction was truncated before completing — raise max_tokens.")

    # Find the text block explicitly; content[0] is not guaranteed to be one.
    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise ValueError(f"No text content returned (stop_reason={message.stop_reason})")

    data = json.loads(text)
    data["electricity_kwh"] = _to_number(data.get("electricity_kwh"))
    data["natural_gas_therms"] = _to_number(data.get("natural_gas_therms"))
    return data


def _to_number(value):
    """Coerce a consumption value to float, tolerating strings like '1,234 kWh'."""
    if value is None or isinstance(value, (int, float)):
        return value
    cleaned = "".join(c for c in str(value) if c.isdigit() or c in ".-")
    try:
        return float(cleaned)
    except ValueError:
        return None

def calculate_scope2(kwh):
    """Calculate Scope 2 emissions from electricity"""
    if not kwh:
        return None
    
    mwh = kwh / 1000
    co2_kg = mwh * CAMX_FACTORS["co2_lb_per_mwh"] * LB_TO_KG * 1.0
    ch4_kg = mwh * CAMX_FACTORS["ch4_lb_per_mwh"] * LB_TO_KG * 28.0
    n2o_kg = mwh * CAMX_FACTORS["n2o_lb_per_mwh"] * LB_TO_KG * 265.0
    
    total_co2e_kg = co2_kg + ch4_kg + n2o_kg
    total_co2e_tonnes = total_co2e_kg * KG_TO_TONNES
    
    return {
        "co2_kg": round(co2_kg, 2),
        "ch4_kg": round(ch4_kg, 6),
        "n2o_kg": round(n2o_kg, 6),
        "total_co2e_kg": round(total_co2e_kg, 2),
        "total_co2e_tonnes": round(total_co2e_tonnes, 4),
    }

def calculate_scope1(therms):
    """Calculate Scope 1 emissions from natural gas"""
    if not therms:
        return None
    
    mmbtu = therms * 0.1
    co2_kg = mmbtu * 53.06 * 1.0
    ch4_kg = mmbtu * 0.001 * 28.0
    n2o_kg = mmbtu * 0.0001 * 265.0
    
    total_co2e_kg = co2_kg + ch4_kg + n2o_kg
    total_co2e_tonnes = total_co2e_kg * KG_TO_TONNES
    
    return {
        "co2_kg": round(co2_kg, 2),
        "ch4_kg": round(ch4_kg, 6),
        "n2o_kg": round(n2o_kg, 6),
        "total_co2e_kg": round(total_co2e_kg, 2),
        "total_co2e_tonnes": round(total_co2e_tonnes, 4),
    }

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/analyze', methods=['POST'])
def analyze():
    try:
        file = request.files['file']
        
        # Read file
        file_data = file.read()
        image_base64 = base64.b64encode(file_data).decode('utf-8')
        
        # Determine media type
        filename = file.filename.lower()
        if filename.endswith('.pdf'):
            media_type = 'application/pdf'
        elif filename.endswith(('.jpg', '.jpeg')):
            media_type = 'image/jpeg'
        elif filename.endswith('.png'):
            media_type = 'image/png'
        elif filename.endswith('.txt'):
            media_type = 'text/plain'
        else:
            return jsonify({"error": "Unsupported file type"}), 400
        
        # Extract data using Claude
        bill_data = extract_bill_data(image_base64, media_type)
        
        # Calculate emissions
        scope2 = calculate_scope2(bill_data.get("electricity_kwh"))
        scope1 = calculate_scope1(bill_data.get("natural_gas_therms"))
        
        total_tonnes = 0
        if scope2:
            total_tonnes += scope2["total_co2e_tonnes"]
        if scope1:
            total_tonnes += scope1["total_co2e_tonnes"]
        
        result = {
            "bill_data": bill_data,
            "scope_1": scope1,
            "scope_2": scope2,
            "total_co2e_tonnes": round(total_tonnes, 4),
        }
        
        return jsonify(result)
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)