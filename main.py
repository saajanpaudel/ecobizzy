import sys
import json
from bill_parser import extract_bill_data
from calculator import calculate_scope2_electricity, calculate_scope1_natural_gas


def run(bill_text, region="CAMX"):
    """
    Full pipeline: parse bill → extract consumption → calculate emissions
    """
    # Extract
    consumption = extract_bill_data(bill_text)
    
    # Calculate
    scope2 = None
    scope1 = None
    
    if consumption["electricity_kwh"]:
        scope2 = calculate_scope2_electricity(consumption["electricity_kwh"], region)
    
    if consumption["natural_gas_therms"]:
        scope1 = calculate_scope1_natural_gas(consumption["natural_gas_therms"])
    
    # Total
    total_tonnes = 0
    if scope2:
        total_tonnes += scope2["total_co2e_tonnes"]
    if scope1:
        total_tonnes += scope1["total_co2e_tonnes"]
    
    # Output
    result = {
        "consumption": consumption,
        "scope_1": scope1,
        "scope_2": scope2,
        "total_co2e_tonnes": round(total_tonnes, 4),
    }
    
    return result


if __name__ == "__main__":
    # Read bill from file or stdin
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'r') as f:
            bill_text = f.read()
    else:
        bill_text = sys.stdin.read()
    
    # Get region if provided
    region = sys.argv[2] if len(sys.argv) > 2 else "CAMX"
    
    # Run
    result = run(bill_text, region)
    
    # Print JSON
    print(json.dumps(result, indent=2))