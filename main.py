from fastapi import FastAPI, File, UploadFile, Response, HTTPException
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
import io

app = FastAPI(title="ZUGFeRD Converter API")

class TrialCheckRequest(BaseModel):
    vat_id: str

@app.get("/")
def read_root():
    return {"status": "ok", "message": "ZUGFeRD Converter API is running"}

@app.post("/check-trial")
def check_trial(data: TrialCheckRequest):
    if data.vat_id == "DE123456789":
        return {"allowed": False, "message": "This VAT ID has already used the free trial."}
    return {"allowed": True, "message": "Trial granted successfully."}

def generate_zugferd_xml() -> bytes:
    """توليد ملف XML متوافق مع معيار ZUGFeRD / Factur-X"""
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
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
async def convert_invoice(file: UploadFile = File(...)):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed.")
    
    # قراءة الملف بدون حفظه على القرص (Zero Data Retention)
    pdf_bytes = await file.read()
    
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    
    for page in reader.pages:
        writer.add_page(page)
        
    # توليد وحقن XML المرفق مع الـ PDF حسب معيار ZUGFeRD/Factur-X
    xml_data = generate_zugferd_xml()
    writer.add_attachment("factur-x.xml", xml_data)
    
    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)
    
    return Response(
        content=output_stream.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=zugferd_{file.filename}"}
    )
