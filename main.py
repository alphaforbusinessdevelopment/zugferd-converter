import stripe
import secrets

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
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
        amount_total = session.get("amount_total", 0)  # المبلغ بالسنت (EUR)

        # تحديد الباقة والرصيد حسب المبلغ المدفوع
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

        # توليد مفتاح API جديد وحفظه في Supabase
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

        # يمكن ربط خدمة إرسال بريد آلي (مثل Resend أو SendGrid) لإرسال raw_key للعميل فوراً

    return {"status": "success"}
