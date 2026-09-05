from fastapi import FastAPI, File, UploadFile, Response
from pydantic import BaseModel

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

@app.post("/convert")
async def convert_invoice(file: UploadFile = File(...)):
    content = await file.read()
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=converted_{file.filename}"}
    )
