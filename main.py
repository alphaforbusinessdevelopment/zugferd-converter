import os
import io
import json
import xml.etree.ElementTree as ET
from typing import Dict, Any

from fastapi import FastAPI, UploadFile, File, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import pypdf
import facturx

app = FastAPI(
    title="Zugify Core API",
    description="Engine for converting standard invoices to PDF/A-3 ZUGFeRD / Factur-X compliant formats",
    version="1.0.0"
)

# 1. إعدادات CORS لحماية السيرفر
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

# تهيئة عميل OpenAI غير المتزامن
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# 2. دالة استخراج النص من ملف الـ PDF
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


# 3. دالة استخراج بيانات الفاتورة عبر الذكاء الاصطناعي
async def parse_invoice_with_ai(pdf_text: str) -> Dict[str, Any]:
    prompt = f"""
    You are an expert financial data extractor adhering strictly to the European standard EN 16931.
    Extract key invoice data from the text below and return ONLY a valid JSON object matching this schema:
    {{
        "invoice_id": "string",
        "issue_date": "YYYY-MM-DD",
        "currency": "EUR/USD/etc",
        "seller_name": "string",
        "seller_vat": "string or null",
        "buyer_name": "string",
        "buyer_vat": "string or null",
        "tax_basis_total_amount": "number or string",
        "tax_total_amount": "number or string",
        "grand_total_amount": "number or string"
    }}

    Invoice Text:
    {pdf_text}
    """

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You convert raw invoice text to EN 16931 JSON objects."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"فشلت عملية تحليل بيانات الفاتورة بواسطة الذكاء الاصطناعي: {str(e)}"
        )


# 4. دالة بناء ملف XML المطابق لمعيار ZUGFeRD / Factur-X
def build_zugferd_xml_safe(data: Dict[str, Any]) -> bytes:
    rsm = ET.Element("rsm:CrossIndustryInvoice", {
        "xmlns:rsm": "urn:untdid:20137:payeq",
        "xmlns:ram": "urn:untdid:20137:payeq:ram",
        "xmlns:udt": "urn:untdid:20137:payeq:udt"
    })
    
    # ExchangedDocumentContext
    header = ET.SubElement(rsm, "rsm:ExchangedDocumentContext")
    guideline = ET.SubElement(header, "ram:GuidelineSpecifiedDocumentContextParameter")
    ET.SubElement(guideline, "ram:ID").text = "urn:cen.eu:en16931:2017"
    
    # ExchangedDocument
    doc = ET.SubElement(rsm, "rsm:ExchangedDocument")
    ET.SubElement(doc, "ram:ID").text = str(data.get("invoice_id", "INV-0001"))
    ET.SubElement(doc, "ram:TypeCode").text = "380"

    issue_date_node = ET.SubElement(doc, "ram:IssueDateTime")
    formatted_date = str(data.get("issue_date", "")).replace("-", "")
    ET.SubElement(issue_date_node, "udt:DateTimeString", format="102").text = formatted_date or "20260101"

    # SupplyChainTradeTransaction
    trade = ET.SubElement(rsm, "rsm:SupplyChainTradeTransaction")
    agreement = ET.SubElement(trade, "ram:ApplicableHeaderTradeAgreement")
    
    # Seller Party
    seller = ET.SubElement(agreement, "ram:SellerTradeParty")
    ET.SubElement(seller, "ram:Name").text = str(data.get("seller_name", "Unknown Seller"))
    if data.get("seller_vat"):
        seller_tax = ET.SubElement(seller, "ram:SpecifiedTaxRegistration")
        ET.SubElement(seller_tax, "ram:ID", schemeID="VA").text = str(data["seller_vat"])

    # Buyer Party
    buyer = ET.SubElement(agreement, "ram:BuyerTradeParty")
    ET.SubElement(buyer, "ram:Name").text = str(data.get("buyer_name", "Unknown Buyer"))
    if data.get("buyer_vat"):
        buyer_tax = ET.SubElement(buyer, "ram:SpecifiedTaxRegistration")
        ET.SubElement(buyer_tax, "ram:ID", schemeID="VA").text = str(data["buyer_vat"])

    # Trade Settlement
    settlement = ET.SubElement(trade, "ram:ApplicableHeaderTradeSettlement")
    currency = str(data.get("currency", "EUR"))
    ET.SubElement(settlement, "ram:InvoiceCurrencyCode").text = currency

    # Monetary Summation
    summation = ET.SubElement(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    ET.SubElement(summation, "ram:TaxBasisTotalAmount", currencyID=currency).text = str(data.get("tax_basis_total_amount", "0.00"))
    ET.SubElement(summation, "ram:TaxTotalAmount", currencyID=currency).text = str(data.get("tax_total_amount", "0.00"))
    ET.SubElement(summation, "ram:GrandTotalAmount", currencyID=currency).text = str(data.get("grand_total_amount", "0.00"))
    ET.SubElement(summation, "ram:DuePayableAmount", currencyID=currency).text = str(data.get("grand_total_amount", "0.00"))

    tree = ET.ElementTree(rsm)
    output = io.BytesIO()
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return output.getvalue()


# 5. Root Route (Health Check)
@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "engine": "Zugify Core API",
        "version": "1.0.0",
        "standard": "EN 16931 / ZUGFeRD / Factur-X"
    }


# 6. Conversion Endpoint
@app.post("/api/v1/convert")
async def convert_pdf_to_zugferd(file: UploadFile = File(...)):
    # 1. التحقق من صيغة الملف
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="نوع الملف غير مدعوم. يرجى رفع ملف PDF فقط.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="الملف المرفوع فارغ.")

    # 2. استخراج النصوص
    extracted_text = extract_text_from_pdf(pdf_bytes)
    if not extracted_text:
        raise HTTPException(
            status_code=422,
            detail="تعذر استخراج النص من ملف PDF. قد يكون الملف عبارة عن صورة/ممسوح ضوئياً (Scanned)."
        )

    # 3. تحليل البيانات بواسطة الذكاء الاصطناعي
    invoice_data = await parse_invoice_with_ai(extracted_text)

    # 4. ضوابط الامتثال القانوني (التحقق من وجود الرقم الضريبي للبائع)
    if not invoice_data.get("seller_vat"):
        raise HTTPException(
            status_code=422,
            detail="الملف يفتقر إلى الرقم الضريبي للبائع (Seller VAT Number)، وهو متطلب إجباري للالتزام بمعيار EN 16931."
        )

    # 5. إنشاء الـ XML وإدماجه ليصبح PDF/A-3
    try:
        xml_bytes = build_zugferd_xml_safe(invoice_data)
        facturx_pdf_bytes = facturx.generate_facturx(pdf_bytes, xml_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"خطأ في الالتزام: تعذر إنشاء ملف PDF/A-3 Factur-X مطابق. التفاصيل: {str(exc)}"
        )

    # 6. إرجاع الملف المعالج مباشرة
    output_filename = f"zugferd_{file.filename}"
    return Response(
        content=facturx_pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={output_filename}"}
    )
