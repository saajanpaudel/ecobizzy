import re
import json

def extract_bill_data(text):
    """
    Read a utility bill (text) and extract kWh and therms.
    Returns a dict with the consumption data.
    """
    
    # Look for electricity consumption (kWh)
    kwh_match = re.search(r'(\d+[\d,]*)\s*kwh', text, re.IGNORECASE)
    kwh = None
    if kwh_match:
        kwh = float(kwh_match.group(1).replace(',', ''))
    
    # Look for natural gas consumption (therms)
    therm_match = re.search(r'(\d+[\d,]*)\s*therm', text, re.IGNORECASE)
    therms = None
    if therm_match:
        therms = float(therm_match.group(1).replace(',', ''))
    
    return {
        "electricity_kwh": kwh,
        "natural_gas_therms": therms
    }


if __name__ == "__main__":
    # Test it with sample text
    sample_bill = """
    PACIFIC GAS AND ELECTRIC COMPANY
    Total Electricity Usage: 142,880 kWh
    Total Gas Usage: 9,640 therms
    """
    
    result = extract_bill_data(sample_bill)
    print(json.dumps(result, indent=2))