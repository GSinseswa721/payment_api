import hashlib
import hmac
import json
from unittest import mock

from django.test import TestCase, override_settings

from .models import Loan, Payment

SECRET = "test-secret"


@override_settings(PROVIDER_SECRETS={"MTN": SECRET})
class PaymentCallbackTests(TestCase):
    def setUp(self):
        self.loan = Loan.objects.create(
            borrower_name="Test", principal=500000, outstanding_balance=500000
        )

    def send(self, txn="MTN-001", status="success", amount=100000, sign=True, **extra):
        body = {
            "provider": "MTN", "transaction_id": txn, "loan_id": self.loan.id,
            "amount": amount, "currency": "RWF", "status": status,
            "timestamp": "2026-10-05T14:30:00Z", **extra,
        }
        raw = json.dumps(body).encode()
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest() if sign else "bad"
        return self.client.post(
            "/callbacks/payments", data=raw, content_type="application/json",
            headers={"X-Signature": sig},
        )

    def balance(self):
        self.loan.refresh_from_db()
        return self.loan.outstanding_balance

    def test_success_reduces_balance(self):
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.balance(), 400000)
        self.assertEqual(Payment.objects.get().status, "success")

    def test_duplicate_is_ignored(self):
        self.send(); r = self.send()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.balance(), 400000)
        self.assertEqual(Payment.objects.count(), 1)

    def test_failed_leaves_balance_untouched(self):
        self.send(status="failed")
        self.assertEqual(self.balance(), 500000)
        self.assertEqual(Payment.objects.get().status, "failed")

    def test_late_success_after_failed_is_ignored(self):
        self.send(status="failed"); self.send(status="success")
        self.assertEqual(self.balance(), 500000)

    def test_crash_before_commit_rolls_everything_back(self):
        real_save = Payment.save

        def crashing_save(p, *a, **k):
            if p.status == "success":
                raise RuntimeError("simulated crash")
            return real_save(p, *a, **k)

        with mock.patch.object(Payment, "save", crashing_save):
            self.assertEqual(self.send().status_code, 500)
        self.assertEqual(self.balance(), 500000)                     # loan restored
        self.assertEqual(Payment.objects.filter(status="success").count(), 0)
        self.assertEqual(self.send().status_code, 200)               # provider retry works
        self.assertEqual(self.balance(), 400000)

    def test_same_reference_different_amount_conflicts(self):
        self.send(); r = self.send(amount=999)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.balance(), 400000)

    def test_overpayment_caps_balance_and_records_excess(self):
        self.send(amount=700000)
        self.assertEqual(self.balance(), 0)
        p = Payment.objects.get()
        self.assertEqual(p.overpaid_amount, 200000)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "paid_off")

    def test_bad_signature_401(self):
        self.assertEqual(self.send(sign=False).status_code, 401)
        self.assertEqual(Payment.objects.count(), 0)

    def test_validation_422(self):
        self.assertEqual(self.send(status="succes").status_code, 422)
        self.assertEqual(self.send(timestamp="2026-10").status_code, 422)

    def test_bad_json_400(self):
        r = self.client.post("/callbacks/payments", data="nope", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_unknown_loan_422(self):
        self.assertEqual(self.send(loan_id=9999).status_code, 422)
