import os
import io
import json
import re
import math
from datetime import datetime
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Depends, status
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
import pypdf
import facturx

# ---------------------------------------------------------
# 1. إعدادات النظام وتحديد حدود الأمان والكريديت
# ---------------------------------------------------------
app = FastAPI(
    title="Zugify Enterprise Core Engine",
    description="Full EN 16931, ZUGFeRD & Factur-X PDF/A-3 compliant engine with credit metering",
    version="2.1.0"
)

# قيود الأمان وحساب الرصيد
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 ميجابايت كحد أقصى لحجم الملف
MAX_SYSTEM_PAGES_LIMIT = 10       # حد حماية السيرفر من هجمات DoS (أقصى عدد صفحات للمستند)
PAGES_PER_CREDIT = 3              # كل 3 صفحات = 1 كريديت

ALLOWED_ORIGINS = [
    "https://zugify.com",
    "https://www.zugify.com",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app",
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
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="مفتاح الـ API غير صالح أو غير مصرح له."
        )
    return api_key

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------------------------
# 2. نماذج البيانات الهيكلية (Pydantic Models)
# ---------------------------------------------------------
class PartyInfo(BaseModel):
    name: str = Field(default="Unknown Party")
    vat_id: Optional[str] = Field(default=None)
    country_code: str = Field(default="DE")
    city: Optional[str] = Field(default=None)
    postcode: Optional[str] = Field(default=None)
    street: Optional[str] = Field(default=None)

class LineItem(BaseModel):
    line_id: str = Field(default="1")
    name: str = Field(default="Item Description")
    quantity: float = Field(default=1.0)
    unit_code: str = Field(default="C62")
    unit_price: float = Field(default=0.0)
    net_amount: float = Field(default=0.0)
    vat_rate: float = Field(default=19.0)
    vat_category: str = Field(default="S")

class PaymentMeans(BaseModel):
    iban: Optional[str] = Field(default=None)
    bic: Optional[str] = Field(default=None)

class InvoiceTotals(BaseModel):
    tax_basis_total: float = Field(default=0.0)
    tax_total: float = Field(default=0.0)
    grand_total: float = Field(default=0.0)
    vat_rate: float = Field(default=19.0)
    vat_category_code: str = Field(default="S")

class ParsedInvoiceSchema(BaseModel):
    invoice_id: str = Field(default="INV-001")
    issue_date: str = Field(default_factory=lambda: datetime.today().strftime("%Y-%m-%d"))
    due_date: Optional[str] = None
    buyer_reference: Optional[str] = None
    currency: str = Field(default="EUR")
    seller: PartyInfo
    buyer: PartyInfo
    payment_means: PaymentMeans = Field(default_factory=PaymentMeans)
    line_items: List[LineItem] = Field(default_factory=list)
    totals: InvoiceTotals

# ---------------------------------------------------------
# 3. الدوال المساعدة وحساب الكريديت ومعالجة الملفات
# ---------------------------------------------------------
def calculate_required_credits(total_pages: int) -> int:
    """حساب عدد الكريديت المستحق: كل 1-3 صفحات تعادل 1 كريديت"""
    if total_pages <= 0:
        return 1
    return math.ceil(total_pages / PAGES_PER_CREDIT)

def safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    try:
        cleaned = re.sub(r"[^\d.,-]", "", str(val)).strip()
        if not cleaned:
            return default
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

def extract_text_and_page_count(pdf_bytes: bytes) -> Tuple[str, int]:
    """قراءة عدد الصفحات واستخراج النصوص مع فحص أمان DoS"""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        
        if total_pages > MAX_SYSTEM_PAGES_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"الملف يتجاوز الحد الأقصى المسموح به لحماية النظام ({MAX_SYSTEM_PAGES_LIMIT} صفحات)."
            )
            
        extracted_text = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                extracted_text += text + "\n"
                
        return extracted_text.strip(), total_pages
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"فشل في معالجة ملف PDF: {str(e)}"
        )

# ---------------------------------------------------------
# 4. محرك استخراج البيانات بالذكاء الاصطناعي
# ---------------------------------------------------------
async def parse_invoice_with_ai(pdf_text: str) -> ParsedInvoiceSchema:
    prompt = f"""
    You are an expert financial data extractor adhering strictly to European Standard EN 16931 (ZUGFeRD / Factur-X).
    Extract invoice data from the provided text and return ONLY a valid JSON object matching this schema:

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
        raw_json = json.loads(response.choices[0].message.content)
        return ParsedInvoiceSchema(**raw_json)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"خطأ في معالجة بيانات الفاتورة بالذكاء الاصطناعي: {str(e)}"
        )

# ---------------------------------------------------------
# 5. محرك بناء الـ XML المطابق لمعيار UN/CEFACT CII
# ---------------------------------------------------------
def build_zugferd_xml_full(invoice: ParsedInvoiceSchema) -> bytes:
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
    ET.SubElement(doc, "ram:ID").text = invoice.invoice_id
    ET.SubElement(doc, "ram:TypeCode").text = "380"
    
    issue_date_node = ET.SubElement(doc, "ram:IssueDateTime")
    ET.SubElement(issue_date_node, "udt:DateTimeString", format="102").text = format_en16931_date(invoice.issue_date)

    # 3. Trade Transaction
    trade = ET.SubElement(rsm, "rsm:SupplyChainTradeTransaction")

    # Line Items
    for idx, item in enumerate(invoice.line_items, 1):
        line_node = ET.SubElement(trade, "ram:IncludedSupplyChainTradeLineItem")
        
        doc_line = ET.SubElement(line_node, "ram:AssociatedDocumentLineDocument")
        ET.SubElement(doc_line, "ram:LineID").text = str(item.line_id or idx)

        product = ET.SubElement(line_node, "ram:SpecifiedTradeProduct")
        ET.SubElement(product, "ram:Name").text = item.name

        agreement = ET.SubElement(line_node, "ram:SpecifiedLineTradeAgreement")
        gross_price = ET.SubElement(agreement, "ram:NetPriceProductTradePrice")
        ET.SubElement(gross_price, "ram:ChargeAmount").text = f"{safe_float(item.unit_price):.2f}"

        delivery = ET.SubElement(line_node, "ram:SpecifiedLineTradeDelivery")
        ET.SubElement(delivery, "ram:BilledQuantity", unitCode=item.unit_code).text = f"{safe_float(item.quantity):.2f}"

        settlement = ET.SubElement(line_node, "ram:SpecifiedLineTradeSettlement")
        trade_tax = ET.SubElement(settlement, "ram:ApplicableTradeTax")
        ET.SubElement(trade_tax, "ram:TypeCode").text = "VAT"
        ET.SubElement(trade_tax, "ram:CategoryCode").text = item.vat_category
        ET.SubElement(trade_tax, "ram:RateApplicablePercent").text = f"{safe_float(item.vat_rate):.2f}"

        monetary = ET.SubElement(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation")
        ET.SubElement(monetary, "ram:LineTotalAmount").text = f"{safe_float(item.net_amount):.2f}"

    # Header Agreement
    header_agreement = ET.SubElement(trade, "ram:ApplicableHeaderTradeAgreement")
    
    if invoice.buyer_reference:
        ET.SubElement(header_agreement, "ram:BuyerReference").text = invoice.buyer_reference

    # Seller Party
    seller = ET.SubElement(header_agreement, "ram:SellerTradeParty")
    ET.SubElement(seller, "ram:Name").text = invoice.seller.name
    
    seller_addr = ET.SubElement(seller, "ram:PostalTradeAddress")
    if invoice.seller.postcode:
        ET.SubElement(seller_addr, "ram:PostcodeCode").text = invoice.seller.postcode
    if invoice.seller.street:
        ET.SubElement(seller_addr, "ram:LineOne").text = invoice.seller.street
    if invoice.seller.city:
        ET.SubElement(seller_addr, "ram:CityName").text = invoice.seller.city
    ET.SubElement(seller_addr, "ram:CountryID").text = invoice.seller.country_code or "DE"

    if invoice.seller.vat_id:
        seller_tax = ET.SubElement(seller, "ram:SpecifiedTaxRegistration")
        ET.SubElement(seller_tax, "ram:ID", schemeID="VA").text = invoice.seller.vat_id

    # Buyer Party
    buyer = ET.SubElement(header_agreement, "ram:BuyerTradeParty")
    ET.SubElement(buyer, "ram:Name").text = invoice.buyer.name
    
    buyer_addr = ET.SubElement(buyer, "ram:PostalTradeAddress")
    if invoice.buyer.postcode:
        ET.SubElement(buyer_addr, "ram:PostcodeCode").text = invoice.buyer.postcode
    if invoice.buyer.street:
        ET.SubElement(buyer_addr, "ram:LineOne").text = invoice.buyer.street
    if invoice.buyer.city:
        ET.SubElement(buyer_addr, "ram:CityName").text = invoice.buyer.city
    ET.SubElement(buyer_addr, "ram:CountryID").text = invoice.buyer.country_code or "DE"

    if invoice.buyer.vat_id:
        buyer_tax = ET.SubElement(buyer, "ram:SpecifiedTaxRegistration")
        ET.SubElement(buyer_tax, "ram:ID", schemeID="VA").text = invoice.buyer.vat_id

    # Delivery & Settlement
    ET.SubElement(trade, "ram:ApplicableHeaderTradeDelivery")

    header_settlement = ET.SubElement(trade, "ram:ApplicableHeaderTradeSettlement")
    currency = invoice.currency or "EUR"
    ET.SubElement(header_settlement, "ram:InvoiceCurrencyCode").text = currency

    # Payment Means
    if invoice.payment_means and invoice.payment_means.iban:
        pay_means = ET.SubElement(header_settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        ET.SubElement(pay_means, "ram:TypeCode").text = "42"
        pay_account = ET.SubElement(pay_means, "ram:PayeePartyCreditorFinancialAccount")
        ET.SubElement(pay_account, "ram:IBANID").text = invoice.payment_means.iban

    # Applicable Tax
    header_tax = ET.SubElement(header_settlement, "ram:ApplicableTradeTax")
    ET.SubElement(header_tax, "ram:CalculatedAmount", currencyID=currency).text = f"{safe_float(invoice.totals.tax_total):.2f}"
    ET.SubElement(header_tax, "ram:TypeCode").text = "VAT"
    ET.SubElement(header_tax, "ram:BasisAmount", currencyID=currency).text = f"{safe_float(invoice.totals.tax_basis_total):.2f}"
    ET.SubElement(header_tax, "ram:CategoryCode").text = invoice.totals.vat_category_code
    ET.SubElement(header_tax, "ram:RateApplicablePercent").text = f"{safe_float(invoice.totals.vat_rate):.2f}"

    # Monetary Summation
    summation = ET.SubElement(header_settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    ET.SubElement(summation, "ram:LineTotalAmount", currencyID=currency).text = f"{safe_float(invoice.totals.tax_basis_total):.2f}"
    ET.SubElement(summation, "ram:TaxBasisTotalAmount", currencyID=currency).text = f"{safe_float(invoice.totals.tax_basis_total):.2f}"
    ET.SubElement(summation, "ram:TaxTotalAmount", currencyID=currency).text = f"{safe_float(invoice.totals.tax_total):.2f}"
    ET.SubElement(summation, "ram:GrandTotalAmount", currencyID=currency).text = f"{safe_float(invoice.totals.grand_total):.2f}"
    ET.SubElement(summation, "ram:DuePayableAmount", currencyID=currency).text = f"{safe_float(invoice.totals.grand_total):.2f}"

    tree = ET.ElementTree(rsm)
    output = io.BytesIO()
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return output.getvalue()

# ---------------------------------------------------------
# 6. نقاط الاتصال التشغيلية (API Endpoints)
# ---------------------------------------------------------
@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "engine": "Zugify Enterprise Core Engine",
        "version": "2.1.0",
        "compliance": "EN 16931 / ZUGFeRD / Factur-X PDF/A-3"
    }

@app.post("/api/v1/parse")
async def parse_invoice_only(
    file: UploadFile = File(...),
    api_key: str = Depends(verify_api_key)
):
    """استخراج بيانات الفاتورة بصيغة JSON مع حساب عدد الكريديت المطلوبة"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف بصيغة PDF فقط.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="حجم الملف يتجاوز الحد المسموح به (10 ميجابايت).")

    extracted_text, total_pages = extract_text_and_page_count(pdf_bytes)
    if not extracted_text:
        raise HTTPException(
            status_code=422,
            detail="تعذر استخراج النص من الفاتورة. الملف عبارة عن صورة ممسوحة ضوئياً (Scanned PDF)."
        )

    parsed_data = await parse_invoice_with_ai(extracted_text)
    required_credits = calculate_required_credits(total_pages)

    return {
        "success": True,
        "total_pages": total_pages,
        "required_credits": required_credits,
        "data": parsed_data
    }

@app.post("/api/v1/convert")
async def convert_pdf_to_zugferd(
    file: UploadFile = File(...),
    api_key: str = Depends(verify_api_key)
):
    """المسار الرئيسي لتحويل الفاتورة إلى PDF/A-3 مع إرجاع مخرجات الكريديت في الـ Headers"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف بصيغة PDF فقط.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="الملف المرفوع فارغ.")
        
    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="حجم الملف يتجاوز الحد المسموح به (10 ميجابايت).")

    # 1. استخراج النص وعدد الصفحات
    extracted_text, total_pages = extract_text_and_page_count(pdf_bytes)
    if not extracted_text:
        raise HTTPException(
            status_code=422,
            detail="تعذر استخراج النص من الفاتورة. يرجى إرفاق ملف PDF نصي وليس صورة."
        )

    required_credits = calculate_required_credits(total_pages)

    # 2. تحليل البيانات بالذكاء الاصطناعي
    invoice_schema = await parse_invoice_with_ai(extracted_text)

    # 3. التحقق من متطلبات الامتثال (الرقم الضريبي للبائع)
    if not invoice_schema.seller.vat_id:
        raise HTTPException(
            status_code=422,
            detail="فشل الامتثال القانوني: الفاتورة تفتقر إلى الرقم الضريبي للبائع (Seller VAT ID)."
        )

    # 4. دمج الـ XML وإنشاء ملف PDF/A-3
    try:
        xml_bytes = build_zugferd_xml_full(invoice_schema)
        facturx_pdf_bytes = facturx.generate_facturx(pdf_bytes, xml_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"خطأ أثناء توليد ملف PDF/A-3 Factur-X المطابق: {str(exc)}"
        )

    output_filename = f"zugferd_{file.filename}"
    
    # إرجاع الملف المدمج وإرسال بيانات الصفحات والكريديت المستهلكة في الـ Headers لتسهيل الربط مع الـ Frontend
    return Response(
        content=facturx_pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={output_filename}",
            "X-Total-Pages": str(total_pages),
            "X-Required-Credits": str(required_credits)
        }
    )
