import json

# CAMX emission factors (EPA eGRID 2023 data)
# Source: https://www.epa.gov/egrid/summary-data
CAMX_FACTORS = {
    "co2_lb_per_mwh": 436.655,
    "ch4_lb_per_mwh": 0.025,
    "n2o_lb_per_mwh": 0.003,
}

# Conversion factors
LB_TO_KG = 0.45359237
KG_TO_TONNES = 0.001

def calculate_scope2_electricity(kwh, region="CAMX"):
    """
    Calculate Scope 2 emissions from electricity consumption.
    
    kwh: kilowatt-hours consumed
    region: eGRID subregion (for now, only CAMX)
    
    Returns: CO2e in metric tonnes
    """
    if region != "CAMX":
        raise ValueError(f"Region {region} not supported yet")
    
    if kwh is None or kwh < 0:
        return None
    
    # Convert kWh to MWh
    mwh = kwh / 1000
    
    # Get factors
    factors = CAMX_FACTORS
    
    # Calculate each gas in kg CO2e
    co2_kg = mwh * factors["co2_lb_per_mwh"] * LB_TO_KG * 1.0  # GWP for CO2
    ch4_kg = mwh * factors["ch4_lb_per_mwh"] * LB_TO_KG * 28.0  # GWP for CH4 (AR5)
    n2o_kg = mwh * factors["n2o_lb_per_mwh"] * LB_TO_KG * 265.0  # GWP for N2O (AR5)
    
    total_co2e_kg = co2_kg + ch4_kg + n2o_kg
    total_co2e_tonnes = total_co2e_kg * KG_TO_TONNES
    
    return {
        "co2_kg": round(co2_kg, 2),
        "ch4_kg": round(ch4_kg, 6),
        "n2o_kg": round(n2o_kg, 6),
        "total_co2e_kg": round(total_co2e_kg, 2),
        "total_co2e_tonnes": round(total_co2e_tonnes, 4),
    }


def calculate_scope1_natural_gas(therms):
    """
    Calculate Scope 1 emissions from natural gas combustion.
    
    therms: therms of natural gas consumed
    
    Returns: CO2e in metric tonnes
    """
    if therms is None or therms < 0:
        return None
    
    # Natural gas emission factors (EPA - needs verification)
    # kg per mmBtu
    co2_kg_per_mmbtu = 53.06
    ch4_kg_per_mmbtu = 0.001
    n2o_kg_per_mmbtu = 0.0001
    
    # Convert therms to mmBtu (1 therm = 0.1 mmBtu)
    mmbtu = therms * 0.1
    
    # Calculate each gas
    co2_kg = mmbtu * co2_kg_per_mmbtu * 1.0
    ch4_kg = mmbtu * ch4_kg_per_mmbtu * 28.0  # GWP for CH4
    n2o_kg = mmbtu * n2o_kg_per_mmbtu * 265.0  # GWP for N2O
    
    total_co2e_kg = co2_kg + ch4_kg + n2o_kg
    total_co2e_tonnes = total_co2e_kg * KG_TO_TONNES
    
    return {
        "co2_kg": round(co2_kg, 2),
        "ch4_kg": round(ch4_kg, 6),
        "n2o_kg": round(n2o_kg, 6),
        "total_co2e_kg": round(total_co2e_kg, 2),
        "total_co2e_tonnes": round(total_co2e_tonnes, 4),
    }


if __name__ == "__main__":
    # Test Scope 2
    scope2 = calculate_scope2_electricity(142880)
    print("Scope 2 (Electricity):")
    print(json.dumps(scope2, indent=2))
    
    # Test Scope 1
    scope1 = calculate_scope1_natural_gas(9640)
    print("\nScope 1 (Natural Gas):")
    print(json.dumps(scope1, indent=2))
    
    # Total
    total = scope2["total_co2e_tonnes"] + scope1["total_co2e_tonnes"]
    print(f"\nTotal CO2e: {total:.4f} metric tonnes")