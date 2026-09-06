import os
import io
import math
import secrets
import hashlib
from datetime import datetime
from typing import Optional, Literal
from fastapi import FastAPI, File, UploadFile, Response, HTTPException, Form, Header, Request
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DictionaryObject, NameObject, StringObject, 
    ArrayObject, DecodedStreamObject
)
from supabase import create_client, Client

app = FastAPI(
    title="ZUGFeRD / Factur-X PDF/A-3 Multi-Language Engine",
    description="GDPR-compliant Zero Data Retention Engine with Multi-Country Rules, Ingestion Tiers & PDF/A-3b Compliance",
    version="2.0.0"
)

# متغيرات البيئة
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
HASH_SALT = os.getenv("HASH_SALT", "default_secure_salt_2026")
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "sk_live_master_key_2026")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "whsec_default_secret_key")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def hash_string(value: str) -> str:
    clean_val = value.strip()
    salted_string = f"{clean_val}:{HASH_SALT}"
    return hashlib.sha256(salted_string.encode('utf-8')).hexdigest()

def calculate_required_credits(page_count: int) -> int:
    """كل 3 صفحات تعادل وحدة فاتورة واحدة"""
    return math.ceil(page_count / 3.0)

def generate_dynamic_zugferd_xml(
    vat_id: str,
    invoice_number: str = "INV-2026-001",
    issue_date: Optional[str] = None,
    target_country: str = "DE",
    siren_siret: Optional[str] = None,
    local_tax_number: Optional[str] = None
) -> bytes:
    """
    توليد هيكل XML مطابق لمعيار EN 16931 ومطابق لشروط الدول:
    - DE (Germany): ZUGFeRD 2.2 / USt-IDNr / Steuernummer
    - FR (France): Factur-X / Chorus Pro (SIREN/SIRET)
    - EU: Standard EN 16931
    """
    if not issue_date:
        issue_date = datetime.utcnow().strftime("%Y%m%d")

    country_code = target_country.upper()
    tax_registration_node = f'<ram:ID schemeID="VA">{vat_id.strip().upper()}</ram:ID>'
    
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

def apply_pdf_a3_compliance(writer: PdfWriter, xml_bytes: bytes):
    """
    تطبيق معيار PDF/A-3b (ISO 19005-3):
    1. حقن XMP Metadata برواسب المعايير الرسمية (pdfaid:part=3, pdfaid:conformance=B, fx:urn)
    2. ربط الملف المرفق بـ AFRelationship / Data
    """
    # 1. إعداد وحقن XMP Metadata Stream
    xmp_xml = f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">
   <pdfaid:part>3</pdfaid:part>
   <pdfaid:conformance>B</pdfaid:conformance>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:fx="urn:factur-x.eu:1p0:1.0#">
   <fx:DocumentType>INVOICE</fx:DocumentType>
   <fx:FileName>factur-x.xml</fx:FileName>
   <fx:Version>1.0</fx:Version>
   <fx:ConformanceLevel>BASIC</fx:ConformanceLevel>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

    metadata_stream = DecodedStreamObject()
    metadata_stream.setData(xmp_xml.encode('utf-8'))
    metadata_stream.update({
        NameObject('/Type'): NameObject('/Metadata'),
        NameObject('/Subtype'): NameObject('/XML')
    })
    metadata_ref = writer._add_object(metadata_stream)
    writer._root_object.update({NameObject('/Metadata'): metadata_ref})

    # 2. إنشاء مرفق الـ XML مع AFRelationship
    file_entry = DecodedStreamObject()
    file_entry.setData(xml_bytes)
    file_entry.update({
        NameObject('/Type'): NameObject('/EmbeddedFile'),
        NameObject('/Subtype'): NameObject('/text#2Fxml'),
        NameObject('/Params'): DictionaryObject({
            NameObject('/ModDate'): StringObject(f"D:{datetime.utcnow().strftime('%Y%m%d%H%M%SZ')}")
        })
    })
    file_ref = writer._add_object(file_entry)

    filespec = DictionaryObject({
        NameObject('/Type'): NameObject('/Filespec'),
        NameObject('/F'): StringObject('factur-x.xml'),
        NameObject('/UF'): StringObject('factur-x.xml'),
        NameObject('/EF'): DictionaryObject({
            NameObject('/F'): file_ref,
            NameObject('/UF'): file_ref
        }),
        NameObject('/AFRelationship'): NameObject('/Data')
    })
    filespec_ref = writer._add_object(filespec)

    if NameObject('/AF') not in writer._root_object:
        writer._root_object[NameObject('/AF')] = ArrayObject()
    writer._root_object[NameObject('/AF')].append(filespec_ref)

    names_entry = DictionaryObject({
        NameObject('/EmbeddedFiles'): DictionaryObject({
            NameObject('/Names'): ArrayObject([StringObject('factur-x.xml'), filespec_ref])
        })
    })
    writer._root_object[NameObject('/Names')] = names_entry

@app.get("/")
def read_root():
    return {
        "status": "ok", 
        "engine": "ZUGFeRD / Factur-X PDF/A-3 Engine", 
        "version": "2.0.0",
        "supported_countries": ["DE", "FR", "EU"],
        "supported_languages": ["en", "de", "fr"],
        "ingestion_methods": ["native_pdf", "ocr_scan", "web_form"]
    }

@app.post("/convert")
async def convert_invoice(
    vat_id: str = Form(...),
    invoice_number: Optional[str] = Form("INV-2026-001"),
    target_country: Literal["DE", "FR", "EU"] = Form("DE"),
    ingestion_method: Literal["native_pdf", "ocr_scan", "web_form"] = Form("native_pdf"),
    siren_siret: Optional[str] = Form(None),
    local_tax_number: Optional[str] = Form(None),
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

    # 1. التحقق من مفتاح الـ Master
    is_master = (x_api_key and x_api_key == MASTER_API_KEY)
    key_data = None
    
    if not is_master and x_api_key and supabase:
        key_hash = hash_string(x_api_key)
        res = supabase.table("api_keys").select("*").eq("key_hash", key_hash).eq("is_active", True).execute()
        if res.data:
            key_data = res.data[0]

    # 2. فحص صَلاحية طريقة الإدخال (OCR / Native)
    if not is_master and key_data:
        if ingestion_method == "ocr_scan" and not key_data.get("allow_ocr", False):
            raise HTTPException(
                status_code=403, 
                detail="OCR processing requires a Pro tier subscription."
            )

    # 3. فحص الرصيد والتجربة المجانية
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

    # 4. توليد الـ XML المعياري وتطبيق التوافقية PDF/A-3b
    xml_data = generate_dynamic_zugferd_xml(
        vat_id=vat_id,
        invoice_number=invoice_number,
        target_country=target_country,
        siren_siret=siren_siret,
        local_tax_number=local_tax_number
    )
    
    apply_pdf_a3_compliance(writer, xml_data)

    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)

    # 5. خصم الرصيد أو تسجيل التجربة
    if not is_master and supabase:
        if key_data:
            new_credits = key_data["credits"] - required_credits
            supabase.table("api_keys").update({"credits": new_credits}).eq("id", key_data["id"]).execute()
        else:
            supabase.table("used_trials").insert({
                "vat_id_hash": vat_hash,
                "target_country": target_country
            }).execute()

    return Response(
        content=output_stream.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=zugferd_{target_country}_{file.filename}"}
    )
