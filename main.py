import os
import hashlib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI(title="ZUGFeRD Converter API")

# Setup Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
HASH_SALT = os.getenv("HASH_SALT", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing Supabase environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

class TrialCheckRequest(BaseModel):
    vat_id: str

def hash_vat_id(vat_id: str) -> str:
    cleaned_vat = vat_id.strip().upper()
    salted_string = f"{cleaned_vat}{HASH_SALT}"
    return hashlib.sha256(salted_string.encode('utf-8')).hexdigest()

@app.get("/")
def read_root():
    return {"status": "online", "service": "ZUGFeRD Converter API"}

@app.post("/check-trial")
def check_and_register_trial(request: TrialCheckRequest):
    vat_hash = hash_vat_id(request.vat_id)
    
    # Query Supabase for existing VAT hash
    response = supabase.table("used_trials").select("id").eq("vat_id_hash", vat_hash).execute()
    
    if response.data and len(response.data) > 0:
        return {
            "allowed": False,
            "message": "This VAT ID has already used the free trial."
        }
    
    # Register trial
    insert_response = supabase.table("used_trials").insert({"vat_id_hash": vat_hash}).execute()
    
    return {
        "allowed": True,
        "message": "Trial granted successfully."
    }
    from fastapi import File, UploadFile, Response

@app.post("/convert")
async def convert_invoice(file: UploadFile = File(...)):
    content = await file.read()
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=converted_{file.filename}"}
    )
