import os
import io
import math
import secrets
import hashlib
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Response, HTTPException, Form, Header, Request
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
from supabase import create_client, Client

app = FastAPI(
    title="ZUGFeRD / Factur-X Converter API",
    description="GDPR-compliant Zero Data Retention Engine with Smart Metering, Subscriptions & Referrals",
    version="1.2.0"
)

# البيئة والمتغيرات
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
HASH_SALT = os.getenv("HASH_SALT", "default_secure_salt_2026")
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "sk_live_master_key_2026")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "whsec_default_secret_key")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class TrialCheckRequest(BaseModel):
    vat_id: str

def hash_string(value: str) -> str:
    clean_val = value.strip()
    salted_string = f"{clean_val}:{HASH_SALT}"
    return hashlib.sha256(salted_string.encode('utf-8')).hexdigest()

def calculate_required_credits(page_count: int) -> int:
    return math.ceil(page_count / 3.0)

def generate_api_key() -> str:
    return f"sk_live_{secrets.token_urlsafe(24)}"

def generate_dynamic_zugferd_xml(
    vat_id: str,
    invoice_number: str = "INV-2026-001",
    issue_date: Optional[str] = None
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
        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">{vat_id.strip().upper()}</ram:ID>
        </ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""
    return xml_content.encode('utf-8')

@app.get("/")
def read_root():
    return {"status": "ok", "service": "ZUGFeRD Engine", "version": "1.2.0"}

@app.post("/convert")
async def convert_invoice(
    vat_id: str = Form(...),
    invoice_number: Optional[str] = Form("INV-2026-001"),
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None)
):
    vat_hash = hash_string(vat_id.strip().upper())
    
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
    
    pdf_bytes = await file.read()
    
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            raise HTTPException(status_code=400, detail="Encrypted PDFs are not supported.")
        
        page_count = len(reader.pages)
        required_credits = calculate_required_credits(page_count)
        
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=400, detail="Corrupted PDF file.")

    # 1. فحص Master Key
    is_master = (x_api_key and x_api_key == MASTER_API_KEY)
    
    key_data = None
    if not is_master and x_api_key and supabase:
        key_hash = hash_string(x_api_key)
        res = supabase.table("api_keys").select("*").eq("key_hash", key_hash).eq("is_active", True).execute()
        if res.data:
            key_data = res.data[0]

    # 2. فحص الرصيد والحظر
    if not is_master:
        if key_data:
            current_credits = key_data.get("credits", 0)
            if current_credits < required_credits:
                raise HTTPException(
                    status_code=402, 
                    detail=f"Insufficient credits. This {page_count}-page document requires {required_credits} credits."
                )
        elif supabase:
            check_res = supabase.table("used_trials").select("*").eq("vat_id_hash", vat_hash).execute()
            if check_res.data:
                raise HTTPException(
                    status_code=403, 
                    detail="This VAT ID has used its free trial. Please upgrade with an API Key."
                )

    # 3. معالجة الحقن
    xml_data = generate_dynamic_zugferd_xml(vat_id=vat_id, invoice_number=invoice_number)
    writer.add_attachment("factur-x.xml", xml_data)

    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)

    # 4. خصم الرصيد
    if not is_master and supabase:
        if key_data:
            new_credits = key_data["credits"] - required_credits
            supabase.table("api_keys").update({"credits": new_credits}).eq("id", key_data["id"]).execute()
        else:
            supabase.table("used_trials").insert({"vat_id_hash": vat_hash}).execute()

    return Response(
        content=output_stream.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=zugferd_{file.filename}"}
    )

@app.post("/webhook/payment")
async def payment_webhook(request: Request):
    """مستقبل عمليات الشراء الآلية لإصدار المفاتيح ومعالجة الإحالات"""
    payload = await request.json()
    
    # فحص الأمان للـ Webhook
    secret_header = request.headers.get("x-webhook-secret", "")
    if secret_header != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    
    customer_email = payload.get("email")
    purchased_credits = int(payload.get("credits", 0))
    referred_by_code = payload.get("referred_by")
    
    if not customer_email or purchased_credits <= 0:
        raise HTTPException(status_code=400, detail="Invalid payload data")

    raw_api_key = generate_api_key()
    key_hash = hash_string(raw_api_key)
    my_referral_code = secrets.token_hex(4).upper()

    if supabase:
        # 1. إدراج مفتاح API جديد للمشتري
        supabase.table("api_keys").insert({
            "key_hash": key_hash,
            "user_email": customer_email,
            "credits": purchased_credits,
            "referral_code": my_referral_code,
            "referred_by": referred_by_code
        }).execute()

        # 2. معالجة مكافأة الإحالة إذا وُجد كود إحالة وتم تنفيذ أول عملية شراء
        if referred_by_code:
            ref_check = supabase.table("referrals").select("*").eq("referred_user_email", customer_email).execute()
            if not ref_check.data:
                # تسريع إضافة 1 رصيد مجاني لصاحب الإحالة
                referrer_res = supabase.table("api_keys").select("*").eq("referral_code", referred_by_code).execute()
                if referrer_res.data:
                    referrer_data = referrer_res.data[0]
                    supabase.table("api_keys").update({
                        "credits": referrer_data["credits"] + 1
                    }).eq("id", referrer_data["id"]).execute()
                    
                    # تسجيل منح المكافأة
                    supabase.table("referrals").insert({
                        "referrer_code": referred_by_code,
                        "referred_user_email": customer_email,
                        "reward_granted": True
                    }).execute()

    return {"status": "success", "api_key": raw_api_key, "credits": purchased_credits}
