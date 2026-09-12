import hashlib
import io
import json
import math
import os
import secrets
from datetime import datetime
from typing import Literal, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from openai import OpenAI
from pypdf import PdfReader, PdfWriter
import resend
from supabase import Client, create_client
import facturx

app = FastAPI(
    title="ZUGFeRD / Factur-X PDF/A-3 Multi-Language Engine",
    description=(
        "GDPR-compliant Zero Data Retention Engine with Multi-Country Rules,"
        " Ingestion Tiers & PDF/A-3 Compliance"
    ),
    version="2.3.1",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------
# 1. إعدادات CORS للسماح بالاتصال من الواجهات الأمامية
# ---------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# 2. متغيرات البيئة والخدمات الخارجية
# ---------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
HASH_SALT = os.getenv("HASH_SALT", "default_secure_salt_2026")
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "sk_live_master_key_2026")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:
        supabase = None

openai_client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        openai_client = None

# ---------------------------------------------------------
# 3. الدوال المساعدة (Helper Functions)
# ---------------------------------------------------------
def hash_string(value: str) -> str:
    clean_val = value.strip()
    salted_string = f"{clean_val}:{HASH_SALT}"
    return hashlib.sha256(salted_string.encode("utf-8")).hexdigest()


def calculate_required_credits(page_count: int) -> int:
    return math.ceil(page_count / 3.0)


def generate_blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    stream.seek(0)
    return stream.getvalue()


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception:
        return ""


def extract_invoice_data_with_ai(text_content: str) -> dict:
    if not openai_client or not text_content.strip():
        return {}
    try:
        prompt = f"""
        Extract key invoice fields from the following text into strict JSON format:
        - vat_id: string or null (Supplier/Seller VAT number)
        - invoice_number: string (default "INV-2026-001" if missing)
        - target_country: "DE", "FR", or "EU"
        - siren_siret: string or null (if French company)
        - local_tax_number: string or null (if German Steuernummer)

        Invoice Text:
        {text_content[:3500]}
        """
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {}


def generate_dynamic_zugferd_xml(
    vat_id: str,
    invoice_number: str = "INV-2026-001",
    issue_date: Optional[str] = None,
    target_country: str = "DE",
    siren_siret: Optional[str] = None,
    local_tax_number: Optional[str] = None,
) -> bytes:
    if not issue_date:
        issue_date = datetime.utcnow().strftime("%Y%m%d")

    country_code = target_country.upper()
    tax_registration_node = (
        f'<ram:ID schemeID="VA">{vat_id.strip().upper()}</ram:ID>'
    )

    if country_code == "FR" and siren_siret:
        tax_registration_node += (
            f'\n        <ram:ID schemeID="0002">{siren_siret.strip()}</ram:ID>'
        )
    elif country_code == "DE" and local_tax_number:
        tax_registration_node += (
            f'\n        <ram:ID schemeID="FC">{local_tax_number.strip()}</ram:ID>'
        )

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
    return xml_content.encode("utf-8")


def verify_and_consume_credit(
    x_api_key: str = Header(..., description="API Key الخاص بك"),
):
    if x_api_key == MASTER_API_KEY:
        return {"tier_id": "master", "credits": 999999}

    if not supabase:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="اتصال قاعدة البيانات غير مفعل.",
        )

    key_hash = hash_string(x_api_key)
    res = (
        supabase.table("api_keys")
        .select("*")
        .eq("key_hash", key_hash)
        .execute()
    )

    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="مفتاح الـ API غير صحيح أو غير موجود.",
        )

    record = res.data[0]

    if not record.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="هذا المفتاح معطل."
        )

    current_credits = record.get("credits", 0)

    if current_credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="رصيدك انتهى. يرجى إعادة الشراء للحصول على نقاط جديدة.",
        )

    new_credits = current_credits - 1
    supabase.table("api_keys").update({"credits": new_credits}).eq(
        "key_hash", key_hash
    ).execute()

    record["credits"] = new_credits
    return record

# ---------------------------------------------------------
# 4. نقاط النهاية العامة (API Endpoints)
# ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def read_root():
    html_file_path = os.path.join(BASE_DIR, "index.html")
    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(
            content="<h2>خطأ: لم يتم العثور على ملف index.html في المجلد الرئيسي.</h2>",
            status_code=404
        )


@app.get("/pricing")
def get_pricing_tiers():
    return {
        "tiers": [
            {
                "id": "free_trial",
                "name_ar": "التجربة المجانية",
                "price_gross_eur": 0.00,
                "price_net_eur": 0.00,
                "vat_eur": 0.00,
                "credits": 1,
                "allow_excel_api": False,
            },
            {
                "id": "single_payg",
                "name_ar": "تحويل فردي",
                "price_gross_eur": 2.99,
                "price_net_eur": 2.51,
                "vat_eur": 0.48,
                "credits": 1,
                "allow_excel_api": False,
            },
            {
                "id": "freelancer",
                "name_ar": "باقة المستقلين",
                "price_gross_eur": 12.00,
                "price_net_eur": 10.08,
                "vat_eur": 1.92,
                "credits": 10,
                "allow_excel_api": False,
            },
            {
                "id": "business",
                "name_ar": "باقة الشركات",
                "price_gross_eur": 35.00,
                "price_net_eur": 29.41,
                "vat_eur": 5.59,
                "credits": 100,
                "allow_excel_api": True,
            },
        ]
    }


@app.get("/check-balance")
def check_balance(x_api_key: str = Header(...)):
    if x_api_key == MASTER_API_KEY:
        return {"tier": "master", "credits": "unlimited"}

    if not supabase:
        raise HTTPException(
            status_code=500, detail="Database connection not configured."
        )

    key_hash = hash_string(x_api_key)
    res = (
        supabase.table("api_keys")
        .select("credits", "is_active", "tier_id")
        .eq("key_hash", key_hash)
        .execute()
    )

    if not res.data:
        raise HTTPException(status_code=404, detail="Invalid API Key.")

    return res.data[0]


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "detail": "Invalid JSON"}

    event_type = body.get("type")
    if event_type in ["checkout.session.completed", "order_created"]:
        session = body.get("data", {}).get("object", {})
        customer_email = session.get("customer_details", {}).get("email") or body.get("data", {}).get("attributes", {}).get("user_email")
        amount_total = session.get("amount_total", 0)

        tier_id = "single_payg"
        credits = 1

        if amount_total >= 3500:
            tier_id = "business"
            credits = 100
        elif amount_total >= 1200:
            tier_id = "freelancer"
            credits = 10
        elif amount_total >= 299:
            tier_id = "single_payg"
            credits = 1

        raw_key = f"sk_live_{secrets.token_urlsafe(24)}"
        key_hash = hash_string(raw_key)

        if supabase:
            supabase.table("api_keys").insert({
                "key_hash": key_hash,
                "credits": credits,
                "tier_id": tier_id,
                "is_active": True,
                "user_email": customer_email,
            }).execute()

        if customer_email and RESEND_API_KEY:
            try:
                resend.Emails.send({
                    "from": "onboarding@resend.dev",
                    "to": customer_email,
                    "subject": "Your API Key - ZUGFeRD Converter",
                    "html": f"""
                    <h2>Thank you for your purchase!</h2>
                    <p>Here is your API Key to access the service:</p>
                    <p style="font-size: 18px; font-weight: bold; background: #f4f4f4; padding: 10px; border-radius: 5px;">
                        {raw_key}
                    </p>
                    <p>Total Credits: <strong>{credits}</strong></p>
                    """,
                })
            except Exception as e:
                print(f"Resend error: {e}")

    return {"status": "success"}


@app.post("/convert")
async def convert_invoice(
    vat_id: Optional[str] = Form(None),
    invoice_number: Optional[str] = Form("INV-2026-001"),
    target_country: Literal["DE", "FR", "EU"] = Form("DE"),
    ingestion_method: Literal[
        "native_pdf", "ocr_scan", "web_form", "api"
    ] = Form("native_pdf"),
    siren_siret: Optional[str] = Form(None),
    local_tax_number: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    x_api_key: Optional[str] = Header(None),
):
    today_str = datetime.utcnow().strftime("%Y%m%d")

    if ingestion_method == "web_form":
        pdf_bytes = generate_blank_pdf()
    else:
        if not file:
            raise HTTPException(
                status_code=400,
                detail="يرجى رفع ملف PDF للتحويل.",
            )
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400, detail="الصيغ المدعومة هي PDF فقط."
            )
        pdf_bytes = await file.read()

    if not vat_id and file:
        extracted_text = extract_text_from_pdf(pdf_bytes)
        ai_data = extract_invoice_data_with_ai(extracted_text)
        vat_id = ai_data.get("vat_id")
        invoice_number = ai_data.get("invoice_number", invoice_number)
        target_country = ai_data.get("target_country", target_country)
        siren_siret = ai_data.get("siren_siret", siren_siret)
        local_tax_number = ai_data.get("local_tax_number", local_tax_number)

    if not vat_id:
        vat_id = "DE999999999"

    vat_hash = hash_string(vat_id.strip().upper())

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            raise HTTPException(
                status_code=400, detail="الملفات المشفرة غير مدعومة."
            )

        page_count = len(reader.pages)
        required_credits = calculate_required_credits(page_count)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=400, detail="ملف PDF غير صالح أو تالف.")

    is_master = bool(x_api_key and x_api_key == MASTER_API_KEY)
    key_data = None

    if not is_master and x_api_key and supabase:
        key_hash = hash_string(x_api_key)
        res = (
            supabase.table("api_keys")
            .select("*")
            .eq("key_hash", key_hash)
            .eq("is_active", True)
            .execute()
        )
        if res.data:
            key_data = res.data[0]

    if not is_master and key_data:
        if ingestion_method == "api" and key_data.get("tier_id") != "business":
            raise HTTPException(
                status_code=403,
                detail="الربط المباشر عبر API يتطلب باقة الشركات (Business Pack).",
            )

    if not is_master:
        if key_data:
            current_credits = key_data.get("credits", 0)
            if current_credits < required_credits:
                raise HTTPException(
                    status_code=402,
                    detail=f"رصيدك غير كافٍ. العملية تتطلب {required_credits} نقاط.",
                )
        elif supabase:
            try:
                check_res = (
                    supabase.table("used_trials")
                    .select("*")
                    .eq("vat_id_hash", vat_hash)
                    .execute()
                )
                if check_res.data:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "تم استخدام التجربة المجانية لهذا الرقم الضريبي من قبل."
                            " يرجى شراء باقة للمتابعة."
                        ),
                    )
            except Exception:
                pass

    xml_data = generate_dynamic_zugferd_xml(
        vat_id=vat_id,
        invoice_number=invoice_number,
        issue_date=today_str,
        target_country=target_country,
        siren_siret=siren_siret,
        local_tax_number=local_tax_number,
    )

    try:
        final_pdf_bytes = facturx.facturx_add_xml_to_pdf_metadata(
            pdf_bytes,
            xml_data,
            facturx_level="basic"
        )
    except Exception:
        writer = PdfWriter()
        writer.append(reader)
        writer.add_attachment("factur-x.xml", xml_data)
        writer.add_metadata({
            "/Title": f"Invoice {invoice_number}",
            "/Creator": "ZUGFeRD PDF/A-3 Engine",
            "/Producer": "FastAPI ZUGFeRD Converter v2.3.1",
            "/Keywords": "ZUGFeRD, Factur-X, EN 16931, E-Invoicing",
        })
        output_stream = io.BytesIO()
        writer.write(output_stream)
        final_pdf_bytes = output_stream.getvalue()

    if not is_master and supabase:
        try:
            if key_data:
                new_credits = key_data["credits"] - required_credits
                supabase.table("api_keys").update({"credits": new_credits}).eq(
                    "id", key_data["id"]
                ).execute()
            else:
                supabase.table("used_trials").insert({
                    "vat_id_hash": vat_hash,
                    "target_country": target_country,
                }).execute()
        except Exception:
            pass

    out_name = file.filename if file else f"{invoice_number}.pdf"
    return Response(
        content=final_pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"attachment; filename=zugferd_{target_country}_{out_name}"
            )
        },
    )


@app.post("/v1/convert")
async def convert_pdf_to_zugferd(
    file: UploadFile = File(...),
    api_user: dict = Depends(verify_and_consume_credit),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="نوع الملف غير مدعوم، يرجى رفع ملف PDF فقط.",
        )

    pdf_bytes = await file.read()
    extracted_text = extract_text_from_pdf(pdf_bytes)
    ai_data = extract_invoice_data_with_ai(extracted_text)

    vat_id = ai_data.get("vat_id", "DE999999999")
    invoice_number = ai_data.get("invoice_number", "INV-2026-001")
    target_country = ai_data.get("target_country", "DE")

    xml_data = generate_dynamic_zugferd_xml(
        vat_id=vat_id,
        invoice_number=invoice_number,
        target_country=target_country,
    )

    try:
        final_pdf_bytes = facturx.facturx_add_xml_to_pdf_metadata(
            pdf_bytes,
            xml_data,
            facturx_level="basic"
        )
    except Exception:
        final_pdf_bytes = pdf_bytes

    return Response(
        content=final_pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=zugferd_{file.filename}",
            "X-Remaining-Credits": str(api_user.get("credits", 0))
        },
    )
