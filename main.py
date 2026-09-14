import os
import io
import json
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Security, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import pypdf
import facturx

app = FastAPI(
    title="Zugify Core Enterprise API",
    description="Full EN 16931 compliant engine converting PDFs to PDF/A-3 ZUGFeRD / Factur-X",
    version="1.1.0"
)

# 1. إعدادات الأمان والـ CORS
ALLOWED_ORIGINS = [
    "https://zugify.com",
    "https://www.zugify.com",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    expected_key = os.getenv("ZUGIFY_API_KEY")
    if expected_key and api_key != expected_key:
        raise HTTPException(status_code=403, detail="مفتاح الـ API غير صحيح أو غير مصرح به.")
    return api_key

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# 2. استخراج النص من PDF
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        extracted_text = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                extracted_text += text + "\n"
        return extracted_text.strip()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"فشل في قراءة محتوى ملف الـ PDF: {str(e)}"
        )


# 3. استخراج البيانات التفصيلية بالذكاء الاصطناعي (ممتثل لـ EN 16931)
async def parse_invoice_with_ai(pdf_text: str) -> Dict[str, Any]:
    prompt = f"""
    You are an expert financial data extractor adhering strictly to European Standard EN 16931 (ZUGFeRD/Factur-X).
    Extract key invoice data from the text below and return ONLY a JSON object with this exact structure:

    {{
        "invoice_id": "string",
        "issue_date": "YYYY-MM-DD",
        "due_date": "YYYY-MM-DD or null",
        "currency": "EUR",
        "seller": {{
            "name": "string",
            "vat_id": "string",
            "country_code": "2-letter ISO code e.g. DE",
            "city": "string or null",
            "postcode": "string or null",
            "street": "string or null"
        }},
        "buyer": {{
            "name": "string",
            "vat_id": "string or null",
            "country_code": "2-letter ISO code e.g. DE",
            "city": "string or null",
            "postcode": "string or null",
            "street": "string or null"
        }},
        "payment_means": {{
            "iban": "string or null",
            "bic": "string or null"
        }},
        "line_items": [
            {{
                "line_id": "1",
                "name": "Item description",
                "quantity": 1.0,
                "unit_code": "C62",
                "unit_price": 0.00,
                "net_amount": 0.00,
                "vat_rate": 19.0,
                "vat_category": "S"
            }}
        ],
        "totals": {{
            "tax_basis_total": 0.00,
            "tax_total": 0.00,
            "grand_total": 0.00,
            "vat_rate": 19.0,
            "vat_category_code": "S"
        }}
    }}

    Invoice Text:
    {pdf_text}
    """

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a precise EN 16931 financial parser."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"فشلت عملية تحليل بيانات الفاتورة بواسطة الذكاء الاصطناعي: {str(e)}"
        )


# 4. بناء ملف XML الكامل والمطابق لـ EN 16931
def build_zugferd_xml_full(data: Dict[str, Any]) -> bytes:
    ns = {
        "xmlns:rsm": "urn:untdid:20137:payeq",
        "xmlns:ram": "urn:untdid:20137:payeq:ram",
        "xmlns:udt": "urn:untdid:20137:payeq:udt"
    }
    rsm = ET.Element("rsm:CrossIndustryInvoice", ns)
    
    # Header Context
    header = ET.SubElement(rsm, "rsm:ExchangedDocumentContext")
    guideline = ET.SubElement(header, "ram:GuidelineSpecifiedDocumentContextParameter")
    ET.SubElement(guideline, "ram:ID").text = "urn:cen.eu:en16931:2017"
    
    # Exchanged Document
    doc = ET.SubElement(rsm, "rsm:ExchangedDocument")
    ET.SubElement(doc, "ram:ID").text = str(data.get("invoice_id", "INV-001"))
    ET.SubElement(doc, "ram:TypeCode").text = "380"
    
    issue_date_node = ET.SubElement(doc, "ram:IssueDateTime")
    formatted_date = str(data.get("issue_date", "")).replace("-", "")
    ET.SubElement(issue_date_node, "udt:DateTimeString", format="102").text = formatted_date or "20260101"

    trade = ET.SubElement(rsm, "rsm:SupplyChainTradeTransaction")

    # Line Items (البنود التفصيلية)
    line_items = data.get("line_items", [])
    for idx, item in enumerate(line_items, 1):
        line_node = ET.SubElement(trade, "ram:IncludedSupplyChainTradeLineItem")
        
        doc_line = ET.SubElement(line_node, "ram:AssociatedDocumentLineDocument")
        ET.SubElement(doc_line, "ram:LineID").text = str(item.get("line_id", idx))

        product = ET.SubElement(line_node, "ram:SpecifiedTradeProduct")
        ET.SubElement(product, "ram:Name").text = str(item.get("name", "Item"))

        agreement = ET.SubElement(line_node, "ram:SpecifiedLineTradeAgreement")
        gross_price = ET.SubElement(agreement, "ram:NetPriceProductTradePrice")
        ET.SubElement(gross_price, "ram:ChargeAmount").text = f"{float(item.get('unit_price', 0)):.2f}"

        delivery = ET.SubElement(line_node, "ram:SpecifiedLineTradeDelivery")
        ET.SubElement(delivery, "ram:BilledQuantity", unitCode=item.get("unit_code", "C62")).text = f"{float(item.get('quantity', 1)):.2f}"

        settlement = ET.SubElement(line_node, "ram:SpecifiedLineTradeSettlement")
        trade_tax = ET.SubElement(settlement, "ram:ApplicableTradeTax")
        ET.SubElement(trade_tax, "ram:TypeCode").text = "VAT"
        ET.SubElement(trade_tax, "ram:CategoryCode").text = item.get("vat_category", "S")
        ET.SubElement(trade_tax, "ram:RateApplicablePercent").text = f"{float(item.get('vat_rate', 19)):.2f}"

        monetary = ET.SubElement(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation")
        ET.SubElement(monetary, "ram:LineTotalAmount").text = f"{float(item.get('net_amount', 0)):.2f}"

    # Header Agreement (Seller & Buyer)
    header_agreement = ET.SubElement(trade, "ram:ApplicableHeaderTradeAgreement")
    
    # Seller
    seller_data = data.get("seller", {})
    seller = ET.SubElement(header_agreement, "ram:SellerTradeParty")
    ET.SubElement(seller, "ram:Name").text = seller_data.get("name", "Seller Name")
    
    seller_addr = ET.SubElement(seller, "ram:PostalTradeAddress")
    ET.SubElement(seller_addr, "ram:PostcodeCode").text = seller_data.get("postcode", "")
    ET.SubElement(seller_addr, "ram:LineOne").text = seller_data.get("street", "")
    ET.SubElement(seller_addr, "ram:CityName").text = seller_data.get("city", "")
    ET.SubElement(seller_addr, "ram:CountryID").text = seller_data.get("country_code", "DE")

    if seller_data.get("vat_id"):
        seller_tax = ET.SubElement(seller, "ram:SpecifiedTaxRegistration")
        ET.SubElement(seller_tax, "ram:ID", schemeID="VA").text = str(seller_data["vat_id"])

    # Buyer
    buyer_data = data.get("buyer", {})
    buyer = ET.SubElement(header_agreement, "ram:BuyerTradeParty")
    ET.SubElement(buyer, "ram:Name").text = buyer_data.get("name", "Buyer Name")
    
    buyer_addr = ET.SubElement(buyer, "ram:PostalTradeAddress")
    ET.SubElement(buyer_addr, "ram:PostcodeCode").text = buyer_data.get("postcode", "")
    ET.SubElement(buyer_addr, "ram:LineOne").text = buyer_data.get("street", "")
    ET.SubElement(buyer_addr, "ram:CityName").text = buyer_data.get("city", "")
    ET.SubElement(buyer_addr, "ram:CountryID").text = buyer_data.get("country_code", "DE")

    if buyer_data.get("vat_id"):
        buyer_tax = ET.SubElement(buyer, "ram:SpecifiedTaxRegistration")
        ET.SubElement(buyer_tax, "ram:ID", schemeID="VA").text = str(buyer_data["vat_id"])

    # Header Delivery
    ET.SubElement(trade, "ram:ApplicableHeaderTradeDelivery")

    # Header Settlement
    header_settlement = ET.SubElement(trade, "ram:ApplicableHeaderTradeSettlement")
    currency = data.get("currency", "EUR")
    ET.SubElement(header_settlement, "ram:InvoiceCurrencyCode").text = currency

    # Payment Means
    payment_data = data.get("payment_means", {})
    if payment_data.get("iban"):
        pay_means = ET.SubElement(header_settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        ET.SubElement(pay_means, "ram:TypeCode").text = "42"  # Payment to bank account
        pay_account = ET.SubElement(pay_means, "ram:PayeePartyCreditorFinancialAccount")
        ET.SubElement(pay_account, "ram:IBANID").text = payment_data["iban"]

    # Category Tax
    totals = data.get("totals", {})
    header_tax = ET.SubElement(header_settlement, "ram:ApplicableTradeTax")
    ET.SubElement(header_tax, "ram:CalculatedAmount", currencyID=currency).text = f"{float(totals.get('tax_total', 0)):.2f}"
    ET.SubElement(header_tax, "ram:TypeCode").text = "VAT"
    ET.SubElement(header_tax, "ram:BasisAmount", currencyID=currency).text = f"{float(totals.get('tax_basis_total', 0)):.2f}"
    ET.SubElement(header_tax, "ram:CategoryCode").text = totals.get("vat_category_code", "S")
    ET.SubElement(header_tax, "ram:RateApplicablePercent").text = f"{float(totals.get('vat_rate', 19)):.2f}"

    # Monetary Summation
    summation = ET.SubElement(header_settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    ET.SubElement(summation, "ram:LineTotalAmount", currencyID=currency).text = f"{float(totals.get('tax_basis_total', 0)):.2f}"
    ET.SubElement(summation, "ram:TaxBasisTotalAmount", currencyID=currency).text = f"{float(totals.get('tax_basis_total', 0)):.2f}"
    ET.SubElement(summation, "ram:TaxTotalAmount", currencyID=currency).text = f"{float(totals.get('tax_total', 0)):.2f}"
    ET.SubElement(summation, "ram:GrandTotalAmount", currencyID=currency).text = f"{float(totals.get('grand_total', 0)):.2f}"
    ET.SubElement(summation, "ram:DuePayableAmount", currencyID=currency).text = f"{float(totals.get('grand_total', 0)):.2f}"

    tree = ET.ElementTree(rsm)
    output = io.BytesIO()
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return output.getvalue()


# 5. Root Route
@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "engine": "Zugify Core Enterprise API",
        "version": "1.1.0",
        "standard": "EN 16931 / ZUGFeRD / Factur-X"
    }


# 6. Conversion Endpoint
@app.post("/api/v1/convert")
async def convert_pdf_to_zugferd(
    file: UploadFile = File(...),
    api_key: str = Depends(verify_api_key)
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="صيغة غير مدعومة. يرجى رفع ملف PDF فقط.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="الملف المرفوع فارغ.")

    # 1. استخراج النص
    extracted_text = extract_text_from_pdf(pdf_bytes)
    if not extracted_text:
        raise HTTPException(
            status_code=422,
            detail="تعذر استخراج النص من ملف PDF. يرجى التأكد من أن الملف ليس عبارة عن صورة (Scanned PDF)."
        )

    # 2. التحليل التفصيلي عبر الذكاء الاصطناعي
    invoice_data = await parse_invoice_with_ai(extracted_text)

    # 3. الفحص الصارم لمتطلبات الامتثال
    seller_vat = invoice_data.get("seller", {}).get("vat_id")
    if not seller_vat:
        raise HTTPException(
            status_code=422,
            detail="فشل الامتثال: الفاتورة تفتقر إلى الرقم الضريبي للبائع (Seller VAT ID)، وهو متطلب إجباري في معيار EN 16931."
        )

    # 4. بناء الـ XML والتحويل إلى PDF/A-3
    try:
        xml_bytes = build_zugferd_xml_full(invoice_data)
        facturx_pdf_bytes = facturx.generate_facturx(pdf_bytes, xml_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"خطأ في معيار PDF/A-3: تعذر دمج الـ XML المولد داخل الفاتورة. التفاصيل: {str(exc)}"
        )

    output_filename = f"zugferd_{file.filename}"
    return Response(
        content=facturx_pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={output_filename}"}
    )
