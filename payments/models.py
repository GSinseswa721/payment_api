from django.db import models


class Loan(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        PAID_OFF = "paid_off"

    borrower_name = models.CharField(max_length=120)
    principal = models.PositiveBigIntegerField()  
    outstanding_balance = models.PositiveBigIntegerField()  
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Loan {self.pk} ({self.borrower_name})"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        SUCCESS = "success"
        FAILED = "failed"

    provider = models.CharField(max_length=20)
    transaction_id = models.CharField(max_length=64) 
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="payments")
    amount = models.PositiveBigIntegerField()
    overpaid_amount = models.PositiveBigIntegerField(default=0)
    currency = models.CharField(max_length=3, default="RWF")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    provider_timestamp = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "transaction_id"], name="uq_provider_txn"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.transaction_id} [{self.status}]"
