import os
import io
import hashlib
from fastapi import FastAPI, File, UploadFile, Response, HTTPException, Form
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
from supabase import create_client, Client

app = FastAPI(title="ZUGFeRD Converter API")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
HASH_SALT = os.getenv("HASH_SALT", "default_secure_salt_2026")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class TrialCheckRequest(BaseModel):
    vat_id: str

def hash_vat_id(vat_id: str) -> str:
    clean_vat = vat_id.strip().upper()
    salted_string = f"{clean_vat}:{HASH_SALT}"
    return hashlib.sha256(salted_string.encode('utf-8')).hexdigest()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "ZUGFeRD Converter API is running"}

@app.post("/check-trial")
def check_trial(data: TrialCheckRequest):
    vat_hash = hash_vat_id(data.vat_id)
    if not supabase:
        return {"allowed": True, "message": "Trial mode active (Database bypass)."}
    
    response = supabase.table("used_trials").select("*").eq("vat_id_hash", vat_hash).execute()
    if response.data:
        return {"allowed": False, "message": "This VAT ID has already used its free trial."}
    
    return {"allowed": True, "message": "VAT ID eligible for free trial."}

def generate_zugferd_xml(vat_id: str = "DE123456789") -> bytes:
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
    <ram:ID>INV-2026-001</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
  </rsm:ExchangedDocument>
</rsm:CrossIndustryInvoice>"""
    return xml_content.encode('utf-8')

@app.post("/convert")
async def convert_invoice(vat_id: str = Form(...), file: UploadFile = File(...)):
    vat_hash = hash_vat_id(vat_id)
    
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed.")
    
    if supabase:
        check_res = supabase.table("used_trials").select("*").eq("vat_id_hash", vat_hash).execute()
        if check_res.data:
            raise HTTPException(status_code=403, detail="This VAT ID has already used its free trial.")
        
        supabase.table("used_trials").insert({"vat_id_hash": vat_hash}).execute()

    pdf_bytes = await file.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    for page in reader.pages:
        writer.add_page(page)

    xml_data = generate_zugferd_xml(vat_id=vat_id)
    writer.add_attachment("factur-x.xml", xml_data)

    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)

    return Response(
        content=output_stream.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=zugferd_{file.filename}"}
    )
