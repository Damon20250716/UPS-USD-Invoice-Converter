import streamlit as st
import pymupdf as fitz
import pandas as pd
import re
from io import BytesIO
from openpyxl import Workbook

# =====================================
# PAGE CONFIG
# =====================================

st.set_page_config(
    page_title="UPS Invoice USD Converter",
    layout="wide"
)

st.title("UPS Invoice USD Converter")

st.markdown("""
Upload a UPS Invoice PDF, enter an exchange rate,
and convert invoice values into USD.
""")

# =====================================
# INPUTS
# =====================================

uploaded_file = st.file_uploader(
    "Upload UPS Invoice PDF",
    type=["pdf"]
)

exchange_rate = st.number_input(
    "Exchange Rate (Local Currency per 1 USD)",
    min_value=0.0001,
    value=150.0000,
    format="%.4f"
)

# =====================================
# FUNCTIONS
# =====================================

def extract_pdf_text(pdf_file):
    pdf_bytes = pdf_file.read()

    doc = fitz.open(
        stream=pdf_bytes,
        filetype="pdf"
    )

    text = ""

    for page in doc:
        text += page.get_text()

    return text


def detect_currency(text):

    if "JPY" in text:
        return "JPY"

    if "KRW" in text:
        return "KRW"

    if "AUD" in text:
        return "AUD"

    if "$" in text:
        return "AUD"

    return "UNKNOWN"


def extract_amount(text, field_name):

    pattern = (
        re.escape(field_name)
        + r".{0,100}?([0-9,]+(?:\.[0-9]+)?)"
    )

    match = re.search(
        pattern,
        text,
        re.IGNORECASE | re.DOTALL
    )

    if match:
        try:
            return float(
                match.group(1).replace(",", "")
            )
        except Exception:
            return None

    return None


def convert_to_usd(amount, exchange_rate):

    if amount is None:
        return None

    return round(
        amount / exchange_rate,
        2
    )


def build_audit_excel(df):

    wb = Workbook()

    ws = wb.active
    ws.title = "Audit"

    ws.append(
        [
            "Field",
            "Original Amount",
            "USD Amount"
        ]
    )

    for _, row in df.iterrows():

        ws.append(
            [
                row["Field"],
                row["Original Amount"],
                row["USD Amount"]
            ]
        )

    excel_buffer = BytesIO()

    wb.save(excel_buffer)

    excel_buffer.seek(0)

    return excel_buffer


# =====================================
# CONVERT BUTTON
# =====================================

if st.button("Convert Invoice"):

    if uploaded_file is None:

        st.error(
            "Please upload a PDF invoice."
        )

    else:

        try:

            text = extract_pdf_text(
                uploaded_file
            )

            currency = detect_currency(text)

            st.success(
                f"Detected Currency: {currency}"
            )

            fields = [
                "Total Amount Due",
                "Total Charges",
                "Charges",
                "GST",
                "VAT",
                "Non-Taxable Charges",
                "Taxable Charges",
                "Disbursement Fee",
                "Security Fee",
                "Customs Entry Fee"
            ]

            results = []

            for field in fields:

                amount = extract_amount(
                    text,
                    field
                )

                if amount is not None:

                    usd = convert_to_usd(
                        amount,
                        exchange_rate
                    )

                    results.append(
                        [
                            field,
                            amount,
                            usd
                        ]
                    )

            if len(results) == 0:

                st.warning(
                    "No invoice amounts were detected."
                )

            else:

                df = pd.DataFrame(
                    results,
                    columns=[
                        "Field",
                        "Original Amount",
                        "USD Amount"
                    ]
                )

                st.subheader(
                    "Conversion Results"
                )

                st.dataframe(
                    df,
                    use_container_width=True
                )

                total_usd = (
                    df["USD Amount"]
                    .fillna(0)
                    .sum()
                )

                st.metric(
                    "Total USD Value",
                    f"${total_usd:,.2f}"
                )

                audit_excel = build_audit_excel(df)

                st.download_button(
                    label="Download Audit Excel",
                    data=audit_excel,
                    file_name="Invoice_Audit.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        except Exception as e:

            st.error(
                f"Error processing PDF: {str(e)}"
            )
