from django.db import transaction

from .models import Loan, Payment


def process_payment(data):
    """
    Record a provider callback and, on success, reduce the loan balance.
    Runs in ONE transaction: any exception rolls back everything.
    Returns (message, http_status).
    """
    incoming = data["status"].lower()

    with transaction.atomic():
        Payment.objects.get_or_create(
            provider=data["provider"].upper(),
            transaction_id=data["transaction_id"],
            defaults={
                "loan_id": data["loan_id"],
                "amount": data["amount"],
                "currency": data["currency"].upper(),
                "provider_timestamp": data["timestamp"],
                "status": Payment.Status.PENDING,
            },
        )
        payment = Payment.objects.select_for_update().get(
            provider=data["provider"].upper(), transaction_id=data["transaction_id"]
        )

        if payment.amount != data["amount"] or payment.loan_id != data["loan_id"]:
            return "conflict: reference reused with different details", 409
        if payment.status != Payment.Status.PENDING:
            return f"already processed ({payment.status})", 200

        if incoming == "pending":
            return "recorded as pending", 200

        if incoming == "failed":
            payment.status = Payment.Status.FAILED
            payment.save()  
            return "failed payment recorded, loan unchanged", 200

        loan = Loan.objects.select_for_update().get(pk=payment.loan_id)
        applied = min(payment.amount, loan.outstanding_balance)
        loan.outstanding_balance -= applied
        loan.status = Loan.Status.PAID_OFF if loan.outstanding_balance == 0 else Loan.Status.ACTIVE
        loan.save()

        payment.overpaid_amount = payment.amount - applied
        payment.status = Payment.Status.SUCCESS
        payment.save()
        return f"success applied, new balance {loan.outstanding_balance}", 200
