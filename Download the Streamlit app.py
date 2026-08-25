import io
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

import streamlit as st
import pandas as pd
import pdfplumber

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

# ---------- Helpers ----------
def parse_pdf_table_first(file_bytes):
    """Try to extract the first useful table from the PDF using pdfplumber.
    Returns a DataFrame or None if no table found."""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            candidate_tables = []
            for page in pdf.pages:
                table = page.extract_table()
                if table and len(table) > 1:
                    # keep tables with at least a header + 1 row
                    candidate_tables.append(table)
            if not candidate_tables:
                return None
            # choose the largest table (by rows)
            table = max(candidate_tables, key=lambda t: len(t))
            header = table[0]
            rows = table[1:]
            # normalize header strings
            header = [h.strip() if h else f"col{i}" for i, h in enumerate(header)]
            df = pd.DataFrame(rows, columns=header)
            return df
    except Exception as e:
        st.warning(f"pdf parsing error: {e}")
        return None

def coerce_table_to_lineitems(df):
    """Attempt to map a generic table to columns: description, qty, unit_price, total_local.
    This is heuristic and will be editable by the user later."""
    df_cols = {c.lower(): c for c in df.columns}
    def find_col(possible):
        for p in possible:
            for k in df_cols:
                if p in k:
                    return df_cols[k]
        return None

    desc_col = find_col(["description", "service", "item", "detail"])
    qty_col = find_col(["qty", "quantity"])
    unit_col = find_col(["unit", "unit price", "rate", "price"])
    total_col = find_col(["amount", "total", "line total", "charge", "net"])

    # fallback: if only 2-3 columns, try to map last column as total
    if total_col is None and df.shape[1] >= 1:
        total_col = df.columns[-1]
    result = pd.DataFrame()
    result["description"] = df[desc_col] if desc_col else df.iloc[:, 0].astype(str)
    result["qty"] = df[qty_col] if qty_col else ""
    if unit_col:
        result["unit_price_local"] = df[unit_col]
    else:
        # if there's a numeric column that isn't description or total, pick first numeric
        numeric_candidate = None
        for c in df.columns:
            if c not in (desc_col, total_col):
                sample = df[c].dropna().astype(str).head(5).tolist()
                if any(re.search(r"\d", s) for s in sample):
                    numeric_candidate = c
                    break
        result["unit_price_local"] = df[numeric_candidate] if numeric_candidate else ""
    result["total_local"] = df[total_col] if total_col else ""
    # clean whitespace
    result = result.fillna("")
    return result

def extract_any_amounts_from_text(file_bytes):
    """Fallback: extract amounts by regex from raw text, return a minimal DataFrame."""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = "\n".join([p.extract_text() or "" for p in pdf.pages])
        # Heuristic: find lines that look like "description ...  123.45"
        lines = text.splitlines()
        rows = []
        for ln in lines:
            # find numbers with 2 decimals
            m = re.search(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2}))", ln)
            if m:
                amount = m.group(1)
                desc = ln[:m.start()].strip()[:120]
                rows.append({"description": desc or "item", "qty": "", "unit_price_local": "", "total_local": amount})
        if rows:
            return pd.DataFrame(rows)
    except Exception as e:
        st.warning(f"text extraction fallback failed: {e}")
    # if nothing found, return empty dataframe with columns for manual entry
    return pd.DataFrame(columns=["description", "qty", "unit_price_local", "total_local"])

def parse_rate_csv(file_bytes):
    """Expect CSV with columns: currency, date (optional), rate_to_usd"""
    try:
        df = pd.read_csv(io.BytesIO(file_bytes))
        cols = [c.lower() for c in df.columns]
        if "currency" not in cols or ("rate" not in cols and "rate_to_usd" not in cols):
            st.error("Rate CSV must contain columns 'currency' and 'rate' or 'rate_to_usd'.")
            return None
        # normalize column names
        rename = {}
        for c in df.columns:
            lc = c.lower()
            if lc == "currency":
                rename[c] = "currency"
            if lc in ("rate", "rate_to_usd"):
                rename[c] = "rate"
            if lc == "date":
                rename[c] = "date"
        df = df.rename(columns=rename)
        return df
    except Exception as e:
        st.error(f"Failed to read rate CSV: {e}")
        return None

def decimal_from_str(s):
    if s is None:
        return Decimal("0")
    s = str(s).strip().replace(",", "")
    s = re.sub(r"[^\d\.\-]", "", s)
    if s == "":
        return Decimal("0")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")

def convert_df_amounts(df, rate, rounding="0.01"):
    """Add 'total_usd' and 'unit_price_usd' columns to df. rate is Decimal (USD per unit of local)."""
    TWO = Decimal(rounding)
    df2 = df.copy()
    for col in ["unit_price_local", "total_local"]:
        if col not in df2.columns:
            df2[col] = ""
    df2["unit_price_local"] = df2["unit_price_local"].astype(str)
    df2["total_local"] = df2["total_local"].astype(str)

    unit_usd = []
    total_usd = []
    for _, row in df2.iterrows():
        u = decimal_from_str(row["unit_price_local"])
        t = decimal_from_str(row["total_local"])
        # if unit empty but total present and qty numeric, compute unit
        qty = Decimal(row["qty"]) if (str(row["qty"]).strip() and re.match(r"^\d+(\.\d+)?$", str(row["qty"]).strip())) else None
        if (u == 0 or str(row["unit_price_local"]).strip() == "") and t != 0 and qty:
            u = (t / qty).quantize(TWO, rounding=ROUND_HALF_UP)
        usd_u = (u * rate).quantize(TWO, rounding=ROUND_HALF_UP)
        usd_t = (t * rate).quantize(TWO, rounding=ROUND_HALF_UP)
        unit_usd.append(str(usd_u))
        total_usd.append(str(usd_t))
    df2["unit_price_usd"] = unit_usd
    df2["total_usd"] = total_usd
    return df2

def make_pdf_from_invoice(invoice_meta, df_lines, rate_info):
    """Generate a simple PDF (regenerated template) with original and USD amounts. Returns bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=36,leftMargin=36, topMargin=36,bottomMargin=36)
    styles = getSampleStyleSheet()
    elems = []
    elems.append(Paragraph("Converted Invoice (USD)", styles["Title"]))
    elems.append(Spacer(1, 6))
    meta_lines = [
        f"Source file: {invoice_meta.get('filename', '')}",
        f"Original currency: {invoice_meta.get('currency', '')}",
        f"Invoice date: {invoice_meta.get('date', '')}",
        f"Conversion rate: {rate_info.get('rate')} (USD per 1 {rate_info.get('currency')})",
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ')}",
    ]
    for m in meta_lines:
        elems.append(Paragraph(m, styles["Normal"]))
    elems.append(Spacer(1, 12))

    # Build table data
    table_data = [["Description", "Qty", f"Unit ({rate_info.get('currency')})", f"Total ({rate_info.get('currency')})", "Unit (USD)", "Total (USD)"]]
    for _, r in df_lines.iterrows():
        table_data.append([
            str(r.get("description", ""))[:80],
            str(r.get("qty", "")),
            str(r.get("unit_price_local", "")),
            str(r.get("total_local", "")),
            str(r.get("unit_price_usd", "")),
            str(r.get("total_usd", "")),
        ])
    t = Table(table_data, repeatRows=1, hAlign='LEFT', colWidths=[180,40,70,80,70,80])
    t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor("#d9d9d9")),
        ('GRID',(0,0),(-1,-1),0.5, colors.grey),
        ('VALIGN',(0,0),(-1,-1),'TOP'),
    ]))
    elems.append(t)
    elems.append(Spacer(1, 12))

    # totals
    try:
        total_local = sum([decimal_from_str(x) for x in df_lines["total_local"]])
        total_usd = sum([decimal_from_str(x) for x in df_lines["total_usd"]])
    except Exception:
        total_local = Decimal("0")
        total_usd = Decimal("0")
    elems.append(Paragraph(f"Total ({rate_info.get('currency')}): {total_local}", styles["Normal"]))
    elems.append(Paragraph(f"Total (USD): {total_usd}", styles["Normal"]))
    doc.build(elems)
    buf.seek(0)
    return buf.read()

# ---------- Streamlit UI ----------
st.title("UPS Invoice Local → USD Converter (Streamlit prototype)")

st.markdown("Upload a UPS invoice PDF (text PDF preferred). The app will try to extract line items; you can edit them before converting.")

uploaded_pdf = st.file_uploader("Upload invoice PDF", type=["pdf"])
col1, col2 = st.columns(2)
with col1:
    rate_csv = st.file_uploader("Upload rates CSV (optional)", type=["csv"])
with col2:
    manual_rate = st.text_input("Or enter manual rate (USD per 1 local)", help="Example: for 1 CAD = 0.75 USD, enter 0.75")
    currency_code = st.text_input("Local currency code (e.g., CAD, EUR)", value="CAD")

use_ocr = st.checkbox("Enable OCR fallback for scanned invoices (requires pytesseract & pdf2image)", value=False)

if uploaded_pdf is not None:
    file_bytes = uploaded_pdf.read()
    st.success(f"Loaded {uploaded_pdf.name} ({len(file_bytes)} bytes)")

    # try table-first parse
    parsed_table = parse_pdf_table_first(file_bytes)
    if parsed_table is not None:
        st.info("Found a table in the PDF. Preview below (heuristic column mapping will be applied).")
        df_lines = coerce_table_to_lineitems(parsed_table)
    else:
        st.info("No table detected automatically. Trying text-extraction heuristic.")
        df_lines = extract_any_amounts_from_text(file_bytes)
        if df_lines.empty:
            st.info("No amounts found automatically. A blank editable table is provided for manual entry.")
            df_lines = pd.DataFrame([{"description":"", "qty":"", "unit_price_local":"", "total_local":""}])

    st.write("Editable line-items (correct or enter data as needed):")
    # Use experimental data editor if available (newer Streamlit versions), else fall back to normal editable form
    try:
        edited = st.experimental_data_editor(df_lines, num_rows="dynamic")
        df_lines = edited.copy()
    except Exception:
        st.write("Your Streamlit version does not have experimental_data_editor; using a simple table input fallback.")
        st.write(df_lines)
        # no editing possible; user must re-upload corrected CSV — keep as-is

    # Process rates
    selected_rate = None
    selected_currency = currency_code.upper() if currency_code else ""
    rate_df = None
    if rate_csv is not None:
        rate_df = parse_rate_csv(rate_csv.read())
        if rate_df is not None:
            st.write("Rates loaded from CSV:")
            st.dataframe(rate_df.head())
            # try to select row matching currency
            matches = rate_df[rate_df['currency'].astype(str).str.upper() == selected_currency.upper()]
            if not matches.empty:
                # pick first match, optionally prefer exact date matching
                r = Decimal(str(matches.iloc[0]["rate"]))
                selected_rate = r
                st.success(f"Selected rate for {selected_currency}: {r}")
            else:
                st.warning(f"No matching currency {selected_currency} found in CSV. Please pick a row below or enter manual rate.")
                pick_idx = st.number_input("Pick CSV row index (0-based) to use as rate", min_value=0, max_value=len(rate_df)-1, value=0)
                if st.button("Use CSV row as rate"):
                    try:
                        r = Decimal(str(rate_df.iloc[int(pick_idx)]["rate"]))
                        selected_rate = r
                        selected_currency = str(rate_df.iloc[int(pick_idx)]["currency"]).upper()
                        st.success(f"Using {selected_currency} @ {selected_rate}")
                    except Exception as e:
                        st.error(f"Failed to pick CSV rate: {e}")
    if selected_rate is None and manual_rate:
        try:
            selected_rate = Decimal(str(manual_rate))
            st.success(f"Using manual rate: {selected_rate}")
        except Exception:
            st.error("Invalid manual rate. Enter a decimal like 0.75 or 1.2345.")

    if selected_rate is None:
        st.info("No rate chosen yet. Enter a manual rate or upload a rate CSV and select a row.")

    rounding = st.selectbox("Rounding rule (per-value)", options=["0.01 (2 decimals)", "1 (whole)"], index=0)
    rounding_val = "0.01" if rounding.startswith("0.01") else "1"

    if st.button("Convert to USD"):
        if selected_rate is None:
            st.error("Please provide an exchange rate (CSV or manual) before converting.")
        else:
            rate = Decimal(selected_rate)
            df_conv = convert_df_amounts(df_lines, rate, rounding=rounding_val)
            st.write("Converted line items (preview):")
            st.dataframe(df_conv)
            # show totals
            try:
                total_local = sum([decimal_from_str(x) for x in df_conv["total_local"]])
                total_usd = sum([decimal_from_str(x) for x in df_conv["total_usd"]])
                st.write(f"Total ({selected_currency}): {total_local} — Total (USD): {total_usd}")
            except Exception:
                pass

            # allow exporting CSV/JSON
            csv_bytes = df_conv.to_csv(index=False).encode("utf-8")
            st.download_button("Download converted data (CSV)", data=csv_bytes, file_name="converted_invoice.csv", mime="text/csv")

            json_bytes = df_conv.to_json(orient="records").encode("utf-8")
            st.download_button("Download converted data (JSON)", data=json_bytes, file_name="converted_invoice.json", mime="application/json")

            # generate PDF
            invoice_meta = {"filename": uploaded_pdf.name, "currency": selected_currency, "date": ""}
            rate_info = {"currency": selected_currency, "rate": str(rate)}
            pdf_bytes = make_pdf_from_invoice(invoice_meta, df_conv, rate_info)
            st.download_button("Download USD PDF (regenerated template)", data=pdf_bytes, file_name="invoice_usd.pdf", mime="application/pdf")
            st.success("Conversion complete. The regenerated PDF lists local + USD amounts and conversion info.")

st.markdown("---")
st.markdown("Notes & next steps: This prototype regenerates a clean PDF rather than overlaying onto the original. If you can provide a sample UPS invoice (or a few variants), I can:")
st.markdown("- Add an OCR fallback flow (pdf2image + pytesseract) for scanned PDFs.")
st.markdown("- Implement overlay mode (place USD values at the same positions on the original PDF).")
st.markdown("- Improve parsing heuristics or build per-template extractors for better accuracy.")
