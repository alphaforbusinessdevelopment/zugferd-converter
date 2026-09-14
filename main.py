import os
import io
import json
import re
from datetime import datetime
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Depends, Form
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import pypdf
import facturx

app = FastAPI(
    title="Zugify Enterprise Core Engine",
    description="Production-grade engine for EN 16931 & ZUGFeRD / Factur-X PDF/A-3 compliance",
    version="1.2.0"
)

# ---------------------------------------------------------
# 1. إعدادات الأمان ونطاقات الاتصال (CORS & API Security)
# ---------------------------------------------------------
ALLOWED_ORIGINS = [
    "https://zugify.com",
    "https://www.zugify.com",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app",  # لدعم نطاقات المعاينة الخاصة بـ Vercel
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
        raise HTTPException(status_code=403, detail="مفتاح الـ API غير صالح أو غير مصرح له.")
    return api_key

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------------------------
# 2. دوال المعالجة المساعدة وتنظيف البيانات (Data Sanitization)
# ---------------------------------------------------------
def safe_float(val: Any, default: float = 0.0) -> float:
    """تنظيف النصوص وتحويلها إلى أرقام عشرية آمنة دون التسبب في أخطاء تشغيلية"""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    try:
        # إزالة العملات والرموز والمسافات
        cleaned = re.sub(r"[^\d.,-]", "", str(val)).strip()
        if not cleaned:
            return default
        # معالجة التنسيق الأوربي (استبدال الفاصلة بالنقطة)
        if "," in cleaned and "." in cleaned:
            if cleaned.find(",") > cleaned.find("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        return float(cleaned)
    except Exception:
        return default

def format_en16931_date(date_str: Optional[str]) -> str:
    """تحويل التواريخ النصية إلى صيغة YYYYMMDD المطابقة لمعيار 102"""
    if not date_str:
        return datetime.today().strftime("%Y%m%d")
    clean_digits = re.sub(r"\D", "", str(date_str))
    if len(clean_digits) == 8:
        return clean_digits
    try:
        parsed_dt = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d")
        return parsed_dt.strftime("%Y%m%d")
    except Exception:
        return datetime.today().strftime("%Y%m%d")

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """استخراج النصوص المباشرة من ملف الـ PDF"""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        extracted_text = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                extracted_text += text + "\n"
        return extracted_text.strip()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"تعذر قراءة ملف PDF: {str(e)}")

# ---------------------------------------------------------
# 3. محرك تحليل البيانات المالي بواسطة الذكاء الاصطناعي
# ---------------------------------------------------------
async def parse_invoice_with_ai(pdf_text: str) -> Dict[str, Any]:
    prompt = f"""
    You are an expert financial data extractor adhering strictly to European Standard EN 16931 (ZUGFeRD / Factur-X).
    Extract invoice data from the provided text and return ONLY a valid JSON object matching this exact schema:

    {{
        "invoice_id": "string",
        "issue_date": "YYYY-MM-DD",
        "due_date": "YYYY-MM-DD or null",
        "buyer_reference": "string or null",
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
                {"role": "system", "content": "You are a precise EN 16931 financial parser. Output pure JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"خطأ أثناء تحليل بيانات الفاتورة بالذكاء الاصطناعي: {str(e)}"
        )

# ---------------------------------------------------------
# 4. محرك بناء ملف XML الدقيق المطابق لمعيار UN/CEFACT CII
# ---------------------------------------------------------
def build_zugferd_xml_full(data: Dict[str, Any]) -> bytes:
    ns = {
        "xmlns:rsm": "urn:untdid:20137:payeq",
        "xmlns:ram": "urn:untdid:20137:payeq:ram",
        "xmlns:udt": "urn:untdid:20137:payeq:udt"
    }
    rsm = ET.Element("rsm:CrossIndustryInvoice", ns)
    
    # 1. Header Context
    header = ET.SubElement(rsm, "rsm:ExchangedDocumentContext")
    guideline = ET.SubElement(header, "ram:GuidelineSpecifiedDocumentContextParameter")
    ET.SubElement(guideline, "ram:ID").text = "urn:cen.eu:en16931:2017"
    
    # 2. Exchanged Document
    doc = ET.SubElement(rsm, "rsm:ExchangedDocument")
    ET.SubElement(doc, "ram:ID").text = str(data.get("invoice_id", "INV-001"))
    ET.SubElement(doc, "ram:TypeCode").text = "380"
    
    issue_date_node = ET.SubElement(doc, "ram:IssueDateTime")
    ET.SubElement(issue_date_node, "udt:DateTimeString", format="102").text = format_en16931_date(data.get("issue_date"))

    # 3. Trade Transaction
    trade = ET.SubElement(rsm, "rsm:SupplyChainTradeTransaction")

    # Line Items
    line_items = data.get("line_items", [])
    for idx, item in enumerate(line_items, 1):
        line_node = ET.SubElement(trade, "ram:IncludedSupplyChainTradeLineItem")
        
        doc_line = ET.SubElement(line_node, "ram:AssociatedDocumentLineDocument")
        ET.SubElement(doc_line, "ram:LineID").text = str(item.get("line_id", idx))

        product = ET.SubElement(line_node, "ram:SpecifiedTradeProduct")
        ET.SubElement(product, "ram:Name").text = str(item.get("name", "Product/Service"))

        agreement = ET.SubElement(line_node, "ram:SpecifiedLineTradeAgreement")
        gross_price = ET.SubElement(agreement, "ram:NetPriceProductTradePrice")
        ET.SubElement(gross_price, "ram:ChargeAmount").text = f"{safe_float(item.get('unit_price')): .2f}".strip()

        delivery = ET.SubElement(line_node, "ram:SpecifiedLineTradeDelivery")
        ET.SubElement(delivery, "ram:BilledQuantity", unitCode=str(item.get("unit_code", "C62"))).text = f"{safe_float(item.get('quantity', 1)):.2f}".strip()

        settlement = ET.SubElement(line_node, "ram:SpecifiedLineTradeSettlement")
        trade_tax = ET.SubElement(settlement, "ram:ApplicableTradeTax")
        ET.SubElement(trade_tax, "ram:TypeCode").text = "VAT"
        ET.SubElement(trade_tax, "ram:CategoryCode").text = str(item.get("vat_category", "S"))
        ET.SubElement(trade_tax, "ram:RateApplicablePercent").text = f"{safe_float(item.get('vat_rate', 19)):.2f}".strip()

        monetary = ET.SubElement(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation")
        ET.SubElement(monetary, "ram:LineTotalAmount").text = f"{safe_float(item.get('net_amount')): .2f}".strip()

    # Header Agreement
    header_agreement = ET.SubElement(trade, "ram:ApplicableHeaderTradeAgreement")
    
    if data.get("buyer_reference"):
        ET.SubElement(header_agreement, "ram:BuyerReference").text = str(data["buyer_reference"])

    # Seller Party
    seller_data = data.get("seller", {})
    seller = ET.SubElement(header_agreement, "ram:SellerTradeParty")
    ET.SubElement(seller, "ram:Name").text = str(seller_data.get("name", "Seller Name"))
    
    seller_addr = ET.SubElement(seller, "ram:PostalTradeAddress")
    if seller_data.get("postcode"):
        ET.SubElement(seller_addr, "ram:PostcodeCode").text = str(seller_data["postcode"])
    if seller_data.get("street"):
        ET.SubElement(seller_addr, "ram:LineOne").text = str(seller_data["street"])
    if seller_data.get("city"):
        ET.SubElement(seller_addr, "ram:CityName").text = str(seller_data["city"])
    ET.SubElement(seller_addr, "ram:CountryID").text = str(seller_data.get("country_code", "DE"))

    if seller_data.get("vat_id"):
        seller_tax = ET.SubElement(seller, "ram:SpecifiedTaxRegistration")
        ET.SubElement(seller_tax, "ram:ID", schemeID="VA").text = str(seller_data["vat_id"])

    # Buyer Party
    buyer_data = data.get("buyer", {})
    buyer = ET.SubElement(header_agreement, "ram:BuyerTradeParty")
    ET.SubElement(buyer, "ram:Name").text = str(buyer_data.get("name", "Buyer Name"))
    
    buyer_addr = ET.SubElement(buyer, "ram:PostalTradeAddress")
    if buyer_data.get("postcode"):
        ET.SubElement(buyer_addr, "ram:PostcodeCode").text = str(buyer_data["postcode"])
    if buyer_data.get("street"):
        ET.SubElement(buyer_addr, "ram:LineOne").text = str(buyer_data["street"])
    if buyer_data.get("city"):
        ET.SubElement(buyer_addr, "ram:CityName").text = str(buyer_data["city"])
    ET.SubElement(buyer_addr, "ram:CountryID").text = str(buyer_data.get("country_code", "DE"))

    if buyer_data.get("vat_id"):
        buyer_tax = ET.SubElement(buyer, "ram:SpecifiedTaxRegistration")
        ET.SubElement(buyer_tax, "ram:ID", schemeID="VA").text = str(buyer_data["vat_id"])

    # Delivery & Settlement
    ET.SubElement(trade, "ram:ApplicableHeaderTradeDelivery")

    header_settlement = ET.SubElement(trade, "ram:ApplicableHeaderTradeSettlement")
    currency = str(data.get("currency", "EUR"))
    ET.SubElement(header_settlement, "ram:InvoiceCurrencyCode").text = currency

    # Payment Means
    payment_data = data.get("payment_means", {})
    if payment_data.get("iban"):
        pay_means = ET.SubElement(header_settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        ET.SubElement(pay_means, "ram:TypeCode").text = "42"
        pay_account = ET.SubElement(pay_means, "ram:PayeePartyCreditorFinancialAccount")
        ET.SubElement(pay_account, "ram:IBANID").text = str(payment_data["iban"])

    # Applicable Tax
    totals = data.get("totals", {})
    header_tax = ET.SubElement(header_settlement, "ram:ApplicableTradeTax")
    ET.SubElement(header_tax, "ram:CalculatedAmount", currencyID=currency).text = f"{safe_float(totals.get('tax_total')):.2f}".strip()
    ET.SubElement(header_tax, "ram:TypeCode").text = "VAT"
    ET.SubElement(header_tax, "ram:BasisAmount", currencyID=currency).text = f"{safe_float(totals.get('tax_basis_total')):.2f}".strip()
    ET.SubElement(header_tax, "ram:CategoryCode").text = str(totals.get("vat_category_code", "S"))
    ET.SubElement(header_tax, "ram:RateApplicablePercent").text = f"{safe_float(totals.get('vat_rate', 19)):.2f}".strip()

    # Monetary Summation
    summation = ET.SubElement(header_settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    ET.SubElement(summation, "ram:LineTotalAmount", currencyID=currency).text = f"{safe_float(totals.get('tax_basis_total')):.2f}".strip()
    ET.SubElement(summation, "ram:TaxBasisTotalAmount", currencyID=currency).text = f"{safe_float(totals.get('tax_basis_total')):.2f}".strip()
    ET.SubElement(summation, "ram:TaxTotalAmount", currencyID=currency).text = f"{safe_float(totals.get('tax_total')):.2f}".strip()
    ET.SubElement(summation, "ram:GrandTotalAmount", currencyID=currency).text = f"{safe_float(totals.get('grand_total')):.2f}".strip()
    ET.SubElement(summation, "ram:DuePayableAmount", currencyID=currency).text = f"{safe_float(totals.get('grand_total')):.2f}".strip()

    tree = ET.ElementTree(rsm)
    output = io.BytesIO()
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return output.getvalue()

# ---------------------------------------------------------
# 5. نقاط الاتصال العامة والخدمية (API Endpoints)
# ---------------------------------------------------------
@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "engine": "Zugify Core Enterprise API",
        "version": "1.2.0",
        "compliance": "EN 16931 / ZUGFeRD / Factur-X PDF/A-3"
    }

@app.post("/api/v1/parse")
async def parse_invoice_only(
    file: UploadFile = File(...),
    api_key: str = Depends(verify_api_key)
):
    """مسار معاينة واستخراج البيانات فقط بصيغة JSON بدون تحويل الملف"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف بصيغة PDF فقط.")

    pdf_bytes = await file.read()
    extracted_text = extract_text_from_pdf(pdf_bytes)
    
    if not extracted_text:
        raise HTTPException(
            status_code=422,
            detail="تعذر استخراج النص من الفاتورة. يبدو أن الملف عبارة عن صورة ممسوحة ضوئياً (Scanned PDF)."
        )

    parsed_data = await parse_invoice_with_ai(extracted_text)
    return {"success": True, "data": parsed_data}

@app.post("/api/v1/convert")
async def convert_pdf_to_zugferd(
    file: UploadFile = File(...),
    api_key: str = Depends(verify_api_key)
):
    """المسار الرئيسي لتحويل الفاتورة مباشرة إلى PDF/A-3 ZUGFeRD"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف بصيغة PDF فقط.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="الملف المرفوع فارغ.")

    # 1. استخراج النصوص
    extracted_text = extract_text_from_pdf(pdf_bytes)
    if not extracted_text:
        raise HTTPException(
            status_code=422,
            detail="تعذر استخراج النص من الفاتورة. يرجى إرفاق ملف PDF نصي وليس صورة."
        )

    # 2. تحليل البيانات
    invoice_data = await parse_invoice_with_ai(extracted_text)

    # 3. فحص الجودة والمطابقة
    seller_vat = invoice_data.get("seller", {}).get("vat_id")
    if not seller_vat:
        raise HTTPException(
            status_code=422,
            detail="فشل الامتثال: لم يتم العثور على الرقم الضريبي للبائع (Seller VAT ID)، وهو حقل إجباري لمعيار EN 16931."
        )

    # 4. بناء الـ XML ودمجه داخل الـ PDF/A-3
    try:
        xml_bytes = build_zugferd_xml_full(invoice_data)
        facturx_pdf_bytes = facturx.generate_facturx(pdf_bytes, xml_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"خطأ أثناء توليد ملف PDF/A-3 المطابق: {str(exc)}"
        )

    output_filename = f"zugferd_{file.filename}"
    return Response(
        content=facturx_pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={output_filename}"}
    )
