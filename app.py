from flask import Flask, request, jsonify, send_from_directory, Response
from anthropic import Anthropic
import os
import base64
import json
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 MB upload cap
client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

SAMPLE_BILL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sample_bill.txt')
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo-white.png')

# EPA emission factors (eGRID 2023, CAMX subregion - California)
CAMX_FACTORS = {
    "co2_lb_per_mwh": 436.655,
    "ch4_lb_per_mwh": 0.025,
    "n2o_lb_per_mwh": 0.003,
}

# Natural gas combustion factors (EPA GHG Emission Factors Hub), kg per MMBtu
GAS_FACTORS = {
    "co2_kg_per_mmbtu": 53.06,
    "ch4_kg_per_mmbtu": 0.001,
    "n2o_kg_per_mmbtu": 0.0001,
}

# Global Warming Potentials, IPCC AR5 100-year
GWP = {"co2": 1.0, "ch4": 28.0, "n2o": 265.0}

LB_TO_KG = 0.45359237
KG_TO_TONNES = 0.001

# Single source of truth for the methodology, surfaced both in the API response
# (for the on-screen Methodology panel) and in the PDF. ASCII gas labels keep
# the PDF's standard fonts happy; the browser is free to prettify them.
METHODOLOGY = {
    "electricity": {
        "scope": "Scope 2",
        "source": "EPA eGRID 2023, CAMX subregion (California)",
        "method": "Location-based",
        "factors": [
            {"gas": "CO2", "value": 436.655, "unit": "lb/MWh"},
            {"gas": "CH4", "value": 0.025, "unit": "lb/MWh"},
            {"gas": "N2O", "value": 0.003, "unit": "lb/MWh"},
        ],
    },
    "natural_gas": {
        "scope": "Scope 1",
        "source": "EPA GHG Emission Factors Hub",
        "method": "Stationary combustion",
        "factors": [
            {"gas": "CO2", "value": 53.06, "unit": "kg/MMBtu"},
            {"gas": "CH4", "value": 0.001, "unit": "kg/MMBtu"},
            {"gas": "N2O", "value": 0.0001, "unit": "kg/MMBtu"},
        ],
    },
    "gwp": {"CO2": 1, "CH4": 28, "N2O": 265, "basis": "IPCC AR5, 100-year"},
}


def _nullable(*types):
    return {"anyOf": [{"type": t} for t in types] + [{"type": "null"}]}


BILL_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": _nullable("string"),
        "account_number": _nullable("string"),
        "service_address": _nullable("string"),
        "statement_date": _nullable("string"),
        "period_start": _nullable("string"),
        "period_end": _nullable("string"),
        "electricity_kwh": _nullable("number"),
        "natural_gas_therms": _nullable("number"),
        "utility": _nullable("string"),
    },
    "required": [
        "company_name",
        "account_number",
        "service_address",
        "statement_date",
        "period_start",
        "period_end",
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
- Billing/service period start date (as YYYY-MM-DD)
- Billing/service period end date (as YYYY-MM-DD)
- Electricity consumption in kWh for this billing period
- Natural gas consumption in therms for this billing period
- Utility company name (PG&E, SCE, etc.)

Report consumption as plain numbers with no units, commas, or currency symbols.
If the bill states usage in a different unit, convert it (1 CCF of natural gas
is approximately 1.037 therms; 1 MWh is 1000 kWh). Use null for any value that
is not present on the bill - do not guess."""


def _to_number(value):
    """Coerce a consumption value to float, tolerating strings like '1,234 kWh'."""
    if value is None or isinstance(value, (int, float)):
        return value
    cleaned = "".join(c for c in str(value) if c.isdigit() or c in ".-")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_bill_data(file_base64, media_type):
    """Use Claude to extract bill data from a PDF, image, or text utility bill."""
    # Plain text needs no vision - pass the bill through as text.
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
        raise ValueError("Extraction was truncated before completing - raise max_tokens.")

    # Find the text block explicitly; content[0] is not guaranteed to be one.
    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise ValueError(f"No text content returned (stop_reason={message.stop_reason})")

    data = json.loads(text)
    data["electricity_kwh"] = _to_number(data.get("electricity_kwh"))
    data["natural_gas_therms"] = _to_number(data.get("natural_gas_therms"))
    return data


def calculate_scope2(kwh):
    """Scope 2 emissions from purchased electricity (location-based)."""
    if kwh is None:  # None means "not on the bill"; 0 is a real zero-usage month
        return None

    mwh = kwh / 1000
    co2_kg = mwh * CAMX_FACTORS["co2_lb_per_mwh"] * LB_TO_KG * GWP["co2"]
    ch4_kg = mwh * CAMX_FACTORS["ch4_lb_per_mwh"] * LB_TO_KG * GWP["ch4"]
    n2o_kg = mwh * CAMX_FACTORS["n2o_lb_per_mwh"] * LB_TO_KG * GWP["n2o"]

    total_co2e_kg = co2_kg + ch4_kg + n2o_kg
    return {
        "co2_kg": round(co2_kg, 2),
        "ch4_kg": round(ch4_kg, 4),
        "n2o_kg": round(n2o_kg, 4),
        "total_co2e_kg": round(total_co2e_kg, 2),
        "total_co2e_tonnes": round(total_co2e_kg * KG_TO_TONNES, 4),
    }


def calculate_scope1(therms):
    """Scope 1 emissions from on-site natural gas combustion."""
    if therms is None:
        return None

    mmbtu = therms * 0.1
    co2_kg = mmbtu * GAS_FACTORS["co2_kg_per_mmbtu"] * GWP["co2"]
    ch4_kg = mmbtu * GAS_FACTORS["ch4_kg_per_mmbtu"] * GWP["ch4"]
    n2o_kg = mmbtu * GAS_FACTORS["n2o_kg_per_mmbtu"] * GWP["n2o"]

    total_co2e_kg = co2_kg + ch4_kg + n2o_kg
    return {
        "co2_kg": round(co2_kg, 2),
        "ch4_kg": round(ch4_kg, 4),
        "n2o_kg": round(n2o_kg, 4),
        "total_co2e_kg": round(total_co2e_kg, 2),
        "total_co2e_tonnes": round(total_co2e_kg * KG_TO_TONNES, 4),
    }


def _clean_utility(value):
    """Strip trailing punctuation/whitespace off the utility name (e.g. 'PG&E;').
    Applied server-side so the results page and the PDF stay consistent."""
    if not isinstance(value, str):
        return value
    cleaned = value.strip().rstrip(";,. ").strip()
    return cleaned or None


def build_result(bill_data):
    """Assemble the full API payload from extracted bill data. Shared by the
    upload, sample, and report endpoints so the numbers can never diverge."""
    bill_data["utility"] = _clean_utility(bill_data.get("utility"))
    kwh = bill_data.get("electricity_kwh")
    therms = bill_data.get("natural_gas_therms")

    scope2 = calculate_scope2(kwh)
    scope1 = calculate_scope1(therms)

    total_tonnes = 0.0
    if scope2:
        total_tonnes += scope2["total_co2e_tonnes"]
    if scope1:
        total_tonnes += scope1["total_co2e_tonnes"]

    return {
        "bill_data": bill_data,
        "scope_1": scope1,
        "scope_2": scope2,
        "total_co2e_tonnes": round(total_tonnes, 4),
        "has_data": scope1 is not None or scope2 is not None,
        "derived": {
            "electricity_mwh": round(kwh / 1000, 2) if kwh is not None else None,
            "natural_gas_mmbtu": round(therms * 0.1, 1) if therms is not None else None,
        },
        "methodology": METHODOLOGY,
    }


def _fmt(n, dp=2):
    """Format a number with thousands separators, or an em dash for None."""
    if n is None:
        return "—"
    return f"{n:,.{dp}f}"


def _parse_date(s):
    """Best-effort parse of a bill date string into a datetime, else None."""
    if not s or not isinstance(s, str):
        return None
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _fmt_date(s):
    """ISO or messy date string -> 'June 12, 2026'; unparseable input passes through."""
    d = _parse_date(s)
    if not d:
        return s
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _fmt_period(start, end):
    """Format a date range as 'June 12 - July 11, 2026' (en dash, year stated once
    when both dates share a year). Returns None if the range can't be parsed."""
    ds, de = _parse_date(start), _parse_date(end)
    if not (ds and de):
        return None
    if ds.year == de.year:
        return f"{ds.strftime('%B')} {ds.day} – {de.strftime('%B')} {de.day}, {de.year}"
    return f"{_fmt_date(start)} – {_fmt_date(end)}"


def generate_report_pdf(result):
    """Render a light-themed, audit-ready PDF from a build_result() payload.

    reportlab is imported lazily so the rest of the app still runs locally if
    it isn't installed yet. All PDF text is ASCII: the standard PDF fonts don't
    carry glyphs like subscript-2 or en dash, so we spell those out here even
    though the on-screen UI uses the prettier Unicode forms.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
        )
    except ImportError as e:
        raise RuntimeError("reportlab is not installed. Run: pip install reportlab") from e

    import io

    GREEN = colors.HexColor('#192D28')
    GREEN_ACCENT = colors.HexColor('#2F5D4F')
    CREAM = colors.HexColor('#F5F1E8')
    TINT = colors.HexColor('#EEF3F0')
    MUTED = colors.HexColor('#5B6B64')
    DARK = colors.HexColor('#1B2620')

    FRAME_W = 7.3 * inch  # letter width (8.5in) minus 0.6in margins each side

    bill = result.get('bill_data') or {}
    scope1 = result.get('scope_1')
    scope2 = result.get('scope_2')
    derived = result.get('derived') or {}
    company = (bill.get('company_name') or 'Customer').strip()

    period = _fmt_period(bill.get('period_start'), bill.get('period_end'))
    if not period and bill.get('statement_date'):
        period = _fmt_date(bill.get('statement_date'))

    body = ParagraphStyle('body', fontName='Helvetica', fontSize=10, textColor=DARK, leading=14)
    h2 = ParagraphStyle('h2', fontName='Helvetica-Bold', fontSize=13, textColor=GREEN,
                        spaceBefore=18, spaceAfter=6)
    small = ParagraphStyle('small', fontName='Helvetica', fontSize=8.5, textColor=MUTED, leading=12)
    label = ParagraphStyle('label', fontName='Helvetica-Bold', fontSize=9, textColor=MUTED)
    big = ParagraphStyle('big', fontName='Helvetica-Bold', fontSize=26, textColor=GREEN, leading=30)

    story = []

    # --- Header band (white logo reads on the green fill) ---
    if os.path.exists(LOGO_PATH):
        left = Image(LOGO_PATH, width=0.5 * inch, height=0.5 * inch)
    else:
        left = Paragraph('Eco Bizzy', ParagraphStyle('l', fontName='Helvetica-Bold',
                         fontSize=16, textColor=CREAM))
    right = Paragraph('EMISSIONS REPORT',
                      ParagraphStyle('t', fontName='Helvetica-Bold', fontSize=15,
                                     textColor=CREAM, alignment=2))
    header = Table([[left, right]], colWidths=[1.3 * inch, 6.0 * inch])
    header.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), GREEN),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (0, 0), 14),
        ('RIGHTPADDING', (-1, -1), (-1, -1), 14),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    story.append(header)
    story.append(Spacer(1, 20))

    # "Prepared for" record: label column muted, value column dark, each on its
    # own row. A table keeps the labels and values on shared baselines with no
    # risk of the lines colliding.
    kv_label = ParagraphStyle('kvl', fontName='Helvetica', fontSize=9.5,
                              textColor=MUTED, leading=15)
    kv_value = ParagraphStyle('kvv', fontName='Helvetica-Bold', fontSize=10,
                              textColor=DARK, leading=15)

    def kv(lbl, val):
        # Escape values: reportlab parses Paragraph text as XML markup, so a bare
        # '&' (e.g. "PG&E") would be mangled into "PG&E;" without escaping.
        return [Paragraph(lbl, kv_label), Paragraph(xml_escape(str(val)), kv_value)]

    info_rows = [kv('Prepared for', company)]
    if bill.get('account_number'):
        info_rows.append(kv('Account', bill['account_number']))
    if bill.get('utility'):
        info_rows.append(kv('Utility', bill['utility']))
    if period:
        info_rows.append(kv('Reporting period', period))
    info_rows.append(kv('Prepared by', 'Eco Bizzy | ecobizzy.com'))

    info = Table(info_rows, colWidths=[1.5 * inch, 5.8 * inch])
    info.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(info)
    story.append(Spacer(1, 18))

    # --- Executive summary ---
    total_t = result.get('total_co2e_tonnes')
    summary = Table(
        [[Paragraph('TOTAL CO2e EMISSIONS (SCOPE 1 + 2)', label)],
         [Paragraph(f"{_fmt(total_t)} <font size=12>metric tonnes CO2e</font>", big)]],
        colWidths=[FRAME_W],
    )
    summary.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), TINT),
        ('LEFTPADDING', (0, 0), (-1, -1), 16),
        ('RIGHTPADDING', (0, 0), (-1, -1), 16),
        ('TOPPADDING', (0, 0), (0, 0), 12),
        ('BOTTOMPADDING', (-1, -1), (-1, -1), 14),
        ('TOPPADDING', (0, 1), (0, 1), 2),
        ('LINEBEFORE', (0, 0), (0, -1), 3, GREEN_ACCENT),
    ]))
    story.append(summary)

    # --- Scope breakdown tables ---
    def scope_block(title, scope, usage_line):
        story.append(Paragraph(title, h2))
        if usage_line:
            story.append(Paragraph(usage_line, small))
            story.append(Spacer(1, 6))
        if not scope:
            story.append(Paragraph("Not reported on this bill.", body))
            return
        rows = [
            ["Greenhouse gas", "CO2e (kg)"],
            ["Carbon dioxide (CO2)", _fmt(scope['co2_kg'])],
            ["Methane (CH4)", _fmt(scope['ch4_kg'])],
            ["Nitrous oxide (N2O)", _fmt(scope['n2o_kg'])],
            ["Total", f"{_fmt(scope['total_co2e_kg'])} kg  |  {_fmt(scope['total_co2e_tonnes'])} t"],
        ]
        t = Table(rows, colWidths=[4.3 * inch, 3.0 * inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), GREEN),
            ('TEXTCOLOR', (0, 0), (-1, 0), CREAM),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('TEXTCOLOR', (0, 1), (-1, -1), DARK),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, TINT]),
            ('LINEABOVE', (0, -1), (-1, -1), 0.75, GREEN_ACCENT),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ]))
        story.append(t)

    e_usage = None
    if bill.get('electricity_kwh') is not None:
        e_usage = (f"Electricity consumption: {_fmt(bill['electricity_kwh'], 0)} kWh "
                   f"(approx. {_fmt(derived.get('electricity_mwh'))} MWh)")
    g_usage = None
    if bill.get('natural_gas_therms') is not None:
        g_usage = (f"Natural gas consumption: {_fmt(bill['natural_gas_therms'], 0)} therms "
                   f"(approx. {_fmt(derived.get('natural_gas_mmbtu'), 1)} MMBtu)")

    scope_block("Scope 2 - Purchased Electricity (location-based)", scope2, e_usage)
    scope_block("Scope 1 - Natural Gas Combustion", scope1, g_usage)

    # --- Methodology ---
    story.append(Paragraph("Methodology", h2))
    m = result.get('methodology') or METHODOLOGY
    elec, gas, gwp = m['electricity'], m['natural_gas'], m['gwp']

    def factor_line(block):
        return ", ".join(f"{f['gas']}: {f['value']} {f['unit']}" for f in block['factors'])

    story.append(Paragraph(
        f"<b>Electricity (Scope 2):</b> {elec['source']}. {factor_line(elec)}.", body))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        f"<b>Natural gas (Scope 1):</b> {gas['source']}. {factor_line(gas)}.", body))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        f"<b>Global Warming Potentials ({gwp['basis']}):</b> "
        f"CO2 = {gwp['CO2']}, CH4 = {gwp['CH4']}, N2O = {gwp['N2O']}.", body))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        "CO2e = sum of (activity x emission factor x GWP). Electricity uses the EPA "
        "eGRID CAMX subregion (California) location-based method.", small))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "This report follows the GHG Protocol Corporate Accounting and Reporting Standard.",
        ParagraphStyle('ghg', fontName='Helvetica-Oblique', fontSize=8.5,
                       textColor=MUTED, leading=12)))

    now = datetime.now()
    generated = f"{now.strftime('%B')} {now.day}, {now.year}"

    # --- Contact / signature block, sits at the end of the content ---
    story.append(Spacer(1, 24))
    sig = ParagraphStyle('sig', fontName='Helvetica', fontSize=8.5, textColor=MUTED, leading=13)
    story.append(Paragraph(f"Report generated by Eco Bizzy on {generated}.", sig))
    story.append(Paragraph("Questions about this report? saajan@ecobizzy.com", sig))

    # --- Build with a footer on every page ---
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.7 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        title=f"Emissions Report - {company}", author="Eco Bizzy",
    )

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.6 * inch, 0.4 * inch,
                          f"Generated {generated}  |  Eco Bizzy (ecobizzy.com)")
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.4 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _safe_filename(company):
    base = "".join(c if c.isalnum() or c in " -_" else "" for c in (company or "")).strip()
    base = base.replace(" ", "-") or "Report"
    return f"Emissions-Report-{base}.pdf"


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/logo-white.png')
def logo():
    # Named explicitly rather than a catch-all root route, which would happily
    # serve .env and app.py over HTTP.
    return send_from_directory('.', 'logo-white.png')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    try:
        file = request.files['file']

        file_data = file.read()
        image_base64 = base64.b64encode(file_data).decode('utf-8')

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

        bill_data = extract_bill_data(image_base64, media_type)
        return jsonify(build_result(bill_data))

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sample', methods=['GET'])
def sample():
    """Run the full extraction pipeline on the bundled sample bill. Powers the
    'Try with sample bill' demo button - no customer bill needed."""
    try:
        with open(SAMPLE_BILL, 'rb') as f:
            sample_base64 = base64.b64encode(f.read()).decode('utf-8')
        bill_data = extract_bill_data(sample_base64, 'text/plain')
        return jsonify(build_result(bill_data))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/report', methods=['POST'])
def report():
    """Generate the downloadable PDF. Recomputes emissions from the posted
    bill_data so the report is always the single source of truth."""
    try:
        payload = request.get_json(force=True) or {}
        bill_data = payload.get('bill_data') or {}
        result = build_result(bill_data)
        pdf_bytes = generate_report_pdf(result)
        filename = _safe_filename(bill_data.get('company_name'))
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # Railway injects PORT; debug must stay off in production (the Werkzeug
    # debugger exposes an arbitrary-code-execution console).
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true')
    app.run(host='0.0.0.0', port=port, debug=debug)
