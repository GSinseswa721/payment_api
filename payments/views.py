import hashlib
import hmac
import json
from datetime import datetime

from django.conf import settings
from django.db import IntegrityError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Loan
from .services import process_payment

ALLOWED_STATUSES = {"success", "failed", "pending"}
ALLOWED_CURRENCIES = {"RWF"}


def valid_signature(secret, raw_body, header_sig):
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig or "")


def validate(data):
    errors = {}
    if not isinstance(data.get("transaction_id"), str) or not data["transaction_id"].strip():
        errors["transaction_id"] = "required non-empty string"
    loan_id = data.get("loan_id")
    if isinstance(loan_id, bool) or not isinstance(loan_id, int) or loan_id <= 0:
        errors["loan_id"] = "must be a positive integer"
    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        errors["amount"] = "must be a positive integer (RWF has no decimals)"
    if str(data.get("currency", "")).upper() not in ALLOWED_CURRENCIES:
        errors["currency"] = f"must be one of {sorted(ALLOWED_CURRENCIES)}"
    if str(data.get("status", "")).lower() not in ALLOWED_STATUSES:
        errors["status"] = f"must be one of {sorted(ALLOWED_STATUSES)}"
    ts = str(data.get("timestamp", ""))
    try:
        if len(ts) < 10:
            raise ValueError
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        errors["timestamp"] = "must be ISO 8601, e.g. 2026-10-05T14:30:00Z"
    return errors


@csrf_exempt  
@require_POST
def payment_callback(request):
    raw = request.body

    # 1. Parse JSON -> 400
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    provider = str(data.get("provider", "")).upper()
    secret = settings.PROVIDER_SECRETS.get(provider)
    if not secret or not valid_signature(secret, raw, request.headers.get("X-Signature")):
        return JsonResponse({"error": "invalid signature"}, status=401)

    errors = validate(data)
    if errors:
        return JsonResponse({"error": "validation failed", "details": errors}, status=422)
    if not Loan.objects.filter(pk=data["loan_id"]).exists():
        return JsonResponse({"error": "unknown loan_id"}, status=422)

    try:
        message, status = process_payment(data)
    except IntegrityError:
        return JsonResponse({"error": "unknown loan_id"}, status=422)
    except Exception:
        return JsonResponse({"error": "internal error"}, status=500)
    return JsonResponse({"message": message}, status=status)
