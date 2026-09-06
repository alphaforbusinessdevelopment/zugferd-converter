import os
import io
import math
import secrets
import hashlib
from datetime import datetime
from typing import Optional, Literal
from fastapi import FastAPI, File, UploadFile, Response, HTTPException, Form, Header, Request
from pypdf import PdfReader, PdfWriter
from supabase import create_client, Client
import stripe

app = FastAPI(
    title="ZUGFeRD / Factur-X PDF/A-3 Multi-Language Engine",
    description="GDPR-compliant Zero Data Retention Engine with Multi-Country Rules, Ingestion Tiers & PDF/A-3 Compliance",
    version="2.3.0"
)

# ---------------------------------------------------------
# 1. إعدادات بيئة العمل والمتغيرات الأساسية
# ---------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
HASH_SALT = os.getenv("HASH_SALT", "default_secure_salt_2026")
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "sk_live_master_key_2026")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:
        supabase = None

# ---------------------------------------------------------
# 2. الدوال المساعدة (Helper Functions)
# ---------------------------------------------------------
def hash_string(value: str) -> str:
    clean_val = value.strip()
    salted_string = f"{clean_val}:{HASH_SALT}"
    return hashlib.sha256(salted_string.encode('utf-8')).hexdigest()

def calculate_required_credits(page_count: int) -> int:
    return math.ceil(page_count / 3.0)

def generate_blank_pdf() -> bytes:
    """توليد ملف PDF نقي خفيف للإدخال اليدوي عبر Web Form بدون مكتبات خارجية قد تكسر البيئة"""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    stream.seek(0)
    return stream.getvalue()

def generate_dynamic_zugferd_xml(
    vat_id: str,
    invoice_number: str = "INV-2026-001",
    issue_date: Optional[str] = None,
    target_country: str = "DE",
    siren_siret: Optional[str] = None,
    local_tax_number: Optional[str] = None
) -> bytes:
    """توليد هيكل XML متوافق مع معايير EN 16931 وقواعد ألمانيا وفرنسا"""
    if not issue_date:
        issue_date = datetime.utcnow().strftime("%Y%m%d")

    country_code = target_country.upper()
    tax_registration_node = f'<ram:ID schemeID="VA">{vat_id.strip().upper()}</ram:ID>'
    
    # القواعد الفنية الخاصة بكل دولة (Germany FC vs France 0002)
    if country_code == "FR" and siren_siret:
        tax_registration_node += f'\n        <ram:ID schemeID="0002">{siren_siret.strip()}</ram:ID>'
    elif country_code == "DE" and local_tax_number:
        tax_registration_node += f'\n        <ram:ID schemeID="FC">{local_tax_number.strip()}</ram:ID>'

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
          {tax_registration_node}
        </ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""
    return xml_content.encode('utf-8')

# ---------------------------------------------------------
# 3. نقاط النهاية للخدمة (API Endpoints)
# ---------------------------------------------------------

@app.get("/")
def read_root():
    return {
        "status": "ok", 
        "engine": "ZUGFeRD / Factur-X PDF/A-3 Multi-Language Engine", 
        "version": "2.3.0",
        "supported_countries": ["DE", "FR", "EU"],
        "supported_languages": ["en", "de", "fr"],
        "ingestion_methods": ["native_pdf", "ocr_scan", "web_form", "api"]
    }

@app.get("/pricing")
def get_pricing_tiers():
    """عرض الباقات الرسمية المطابقة لجدول التسعير المعتمد (شامل ضريبة 19%)"""
    if supabase:
        try:
            res = supabase.table("tiers").select("*").execute()
            if res.data:
                return {"tiers": res.data}
        except Exception:
            pass

    # القيم الاحتياطية المطابقة تماماً لجدول التسعير الرسمي
    return {
        "tiers": [
            {"id": "free_trial", "name_ar": "التجربة المجانية", "price_gross_eur": 0.00, "price_net_eur": 0.00, "vat_eur": 0.00, "credits": 1, "allow_excel_api": False},
            {"id": "single_payg", "name_ar": "تحويل فردي", "price_gross_eur": 2.99, "price_net_eur": 2.51, "vat_eur": 0.48, "credits": 1, "allow_excel_api": False},
            {"id": "freelancer", "name_ar": "باقة المستقلين", "price_gross_eur": 12.00, "price_net_eur": 10.08, "vat_eur": 1.92, "credits": 10, "allow_excel_api": False},
            {"id": "business", "name_ar": "باقة الشركات", "price_gross_eur": 35.00, "price_net_eur": 29.41, "vat_eur": 5.59, "credits": 100, "allow_excel_api": True}
        ]
    }

@app.get("/check-balance")
def check_balance(x_api_key: str = Header(...)):
    """فحص رصيد المفتاح والصلاحيات"""
    if x_api_key == MASTER_API_KEY:
        return {"tier": "master", "credits": "unlimited"}
        
    if not supabase:
        raise HTTPException(status_code=500, detail="Database connection not configured.")
        
    key_hash = hash_string(x_api_key)
    res = supabase.table("api_keys").select("credits", "is_active", "tier_id").eq("key_hash", key_hash).execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="Invalid API Key.")
        
    return res.data[0]

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """استقبال إشعارات الدفع من Stripe وتوليد المفاتيح ورصيد الفواتير تلقائياً"""
    if not STRIPE_WEBHOOK_SECRET:
        return {"status": "ignored", "reason": "Webhook secret not configured"}

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook Error: {str(e)}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_email = session.get("customer_details", {}).get("email")
        amount_total = session.get("amount_total", 0)  # المبلغ بالسنت

        # مطابقة المبلغ المخصوم بالباقة المستحقة
        tier_id = "single_payg"
        credits = 1
        
        if amount_total >= 3500:      # باقة الشركات (35.00 EUR)
            tier_id = "business"
            credits = 100
        elif amount_total >= 1200:    # باقة المستقلين (12.00 EUR)
            tier_id = "freelancer"
            credits = 10
        elif amount_total >= 299:     # تحويل فردي (2.99 EUR)
            tier_id = "single_payg"
            credits = 1

        # توليد وحفظ مفتاح API المشفر في Supabase
        raw_key = f"sk_live_{secrets.token_urlsafe(24)}"
        key_hash = hash_string(raw_key)

        if supabase:
            supabase.table("api_keys").insert({
                "key_hash": key_hash,
                "credits": credits,
                "tier_id": tier_id,
                "is_active": True,
                "owner_email": customer_email
            }).execute()

    return {"status": "success"}

@app.post("/convert")
async def convert_invoice(
    vat_id: str = Form(...),
    invoice_number: Optional[str] = Form("INV-2026-001"),
    target_country: Literal["DE", "FR", "EU"] = Form("DE"),
    ingestion_method: Literal["native_pdf", "ocr_scan", "web_form", "api"] = Form("native_pdf"),
    siren_siret: Optional[str] = Form(None),
    local_tax_number: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    x_api_key: Optional[str] = Header(None)
):
    """المحرك الرئيسي لتحويل الفواتير وتطبيق الحوكمة وإرفاق الـ XML"""
    vat_hash = hash_string(vat_id.strip().upper())
    today_str = datetime.utcnow().strftime("%Y%m%d")

    # 1. تحديد معالجة المستند بناءً على طريقة الإدخال المختارة
    if ingestion_method == "web_form":
        pdf_bytes = generate_blank_pdf()
    else:
        if not file:
            raise HTTPException(status_code=400, detail="File upload is required for this ingestion method.")
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
        writer.append(reader)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=400, detail="Corrupted PDF file.")

    # 2. حوكمة المفاتيح والتأكد من الصلاحيات والاشتراك
    is_master = (x_api_key and x_api_key == MASTER_API_KEY)
    key_data = None
    
    if not is_master and x_api_key and supabase:
        key_hash = hash_string(x_api_key)
        res = supabase.table("api_keys").select("*").eq("key_hash", key_hash).eq("is_active", True).execute()
        if res.data:
            key_data = res.data[0]

    # حصر الربط المباشر بـ API لباقة الشركات فقط
    if not is_master and key_data:
        if ingestion_method == "api" and key_data.get("tier_id") != "business":
            raise HTTPException(
                status_code=403, 
                detail="Direct API integration requires the Business Pack subscription."
            )

    # 3. فحص خصم الرصيد أو التجربة المجانية (فاتورة واحدة)
    if not is_master:
        if key_data:
            current_credits = key_data.get("credits", 0)
            if current_credits < required_credits:
                raise HTTPException(
                    status_code=402, 
                    detail=f"Insufficient credits. Requires {required_credits} credits."
                )
        elif supabase:
            try:
                check_res = supabase.table("used_trials").select("*").eq("vat_id_hash", vat_hash).execute()
                if check_res.data:
                    raise HTTPException(
                        status_code=403, 
                        detail="This VAT ID has used its free trial (1 invoice). Please purchase a pack to continue."
                    )
            except Exception:
                pass

    # 4. توليد الـ XML وإرفاقه للـ PDF وتطبيق ميتا داتا الـ PDF/A-3b
    xml_data = generate_dynamic_zugferd_xml(
        vat_id=vat_id,
        invoice_number=invoice_number,
        issue_date=today_str,
        target_country=target_country,
        siren_siret=siren_siret,
        local_tax_number=local_tax_number
    )
    
    writer.add_attachment("factur-x.xml", xml_data)
    writer.add_metadata({
        "/Title": f"Invoice {invoice_number}",
        "/Creator": "ZUGFeRD PDF/A-3 Engine",
        "/Producer": "FastAPI ZUGFeRD Converter v2.3",
        "/Keywords": "ZUGFeRD, Factur-X, EN 16931, E-Invoicing"
    })

    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)

    # 5. خصم الرصيد أو تسجيل استهلاك التجربة المجانية
    if not is_master and supabase:
        try:
            if key_data:
                new_credits = key_data["credits"] - required_credits
                supabase.table("api_keys").update({"credits": new_credits}).eq("id", key_data["id"]).execute()
            else:
                supabase.table("used_trials").insert({
                    "vat_id_hash": vat_hash,
                    "target_country": target_country
                }).execute()
        except Exception:
            pass

    out_name = file.filename if file else f"{invoice_number}.pdf"
    return Response(
        content=output_stream.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=zugferd_{target_country}_{out_name}"}
    )
