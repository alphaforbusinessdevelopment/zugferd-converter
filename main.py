import os
import io
import hashlib
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Response, HTTPException, Form, Header
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
from supabase import create_client, Client

app = FastAPI(
    title="ZUGFeRD / Factur-X Converter API",
    description="GDPR-compliant Zero Data Retention E-Invoice Generation Engine",
    version="1.0.0"
)

# البيئة والمتغيرات
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
HASH_SALT = os.getenv("HASH_SALT", "default_secure_salt_2026")
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "sk_live_master_key_2026")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class TrialCheckRequest(BaseModel):
    vat_id: str

def hash_vat_id(vat_id: str) -> str:
    clean_vat = vat_id.strip().upper()
    salted_string = f"{clean_vat}:{HASH_SALT}"
    return hashlib.sha256(salted_string.encode('utf-8')).hexdigest()

def generate_dynamic_zugferd_xml(
    vat_id: str,
    invoice_number: str = "INV-2026-001",
    issue_date: Optional[str] = None,
    seller_name: str = "Supplier Company",
    buyer_name: str = "Client Company"
) -> bytes:
    if not issue_date:
        issue_date = datetime.utcnow().strftime("%Y%m%d")

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
                          xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
                          xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:factur-x.eu:1p0:basic</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
  <rsm:ExchangedDocument>
    <ram:ID>{invoice_number}</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">{issue_date}</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>{seller_name}</ram:Name>
        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">{vat_id.strip().upper()}</ram:ID>
        </ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
      <ram:BuyerTradeParty>
        <ram:Name>{buyer_name}</ram:Name>
      </ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""
    return xml_content.encode('utf-8')

@app.get("/")
def read_root():
    return {"status": "ok", "service": "ZUGFeRD Engine", "gdpr_mode": "Zero Data Retention"}

@app.post("/check-trial")
def check_trial(data: TrialCheckRequest):
    vat_hash = hash_vat_id(data.vat_id)
    if not supabase:
        return {"allowed": True, "message": "Trial mode active (Database bypass)."}
    
    response = supabase.table("used_trials").select("*").eq("vat_id_hash", vat_hash).execute()
    if response.data:
        return {"allowed": False, "message": "This VAT ID has already used its free trial."}
    
    return {"allowed": True, "message": "VAT ID eligible for free trial."}

@app.post("/convert")
async def convert_invoice(
    vat_id: str = Form(...),
    invoice_number: Optional[str] = Form("INV-2026-001"),
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None)
):
    vat_hash = hash_vat_id(vat_id)
    
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Invalid file type. Only PDF files are supported.")
    
    # التحقق من مفتاح الـ API المدفوع
    is_paid_user = (x_api_key and x_api_key == MASTER_API_KEY)
    
    # إذا لم يكن مشتركاً مدفوعاً، يتم تطبيق فحص التجربة المجانية
    if not is_paid_user and supabase:
        check_res = supabase.table("used_trials").select("*").eq("vat_id_hash", vat_hash).execute()
        if check_res.data:
            raise HTTPException(
                status_code=403, 
                detail="This VAT ID has used its free trial. Please provide a valid X-API-KEY to upgrade."
            )

    pdf_bytes = await file.read()
    
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            raise HTTPException(status_code=400, detail="Encrypted or password-protected PDFs are not supported.")
        
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=400, detail="Corrupted or invalid PDF structure.")

    xml_data = generate_dynamic_zugferd_xml(vat_id=vat_id, invoice_number=invoice_number)
    writer.add_attachment("factur-x.xml", xml_data)

    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)

    # التسجيل في قائمة المجانيين فقط إن لم يكن مشتركاً مدفوعاً
    if not is_paid_user and supabase:
        supabase.table("used_trials").insert({"vat_id_hash": vat_hash}).execute()

    return Response(
        content=output_stream.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=zugferd_{file.filename}"}
    )
