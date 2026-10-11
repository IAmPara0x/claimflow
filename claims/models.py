import secrets
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Member(models.Model):
    """An insured person (the patient). Fields here count as PHI."""

    member_id = models.CharField(
        max_length=12,
        unique=True,
        validators=[RegexValidator(r"^M\d{6,11}$", "Member ID looks like M123456.")],
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField()
    coverage_start = models.DateField()
    coverage_end = models.DateField(null=True, blank=True, help_text="Empty means coverage is ongoing.")

    class Meta:
        ordering = ["last_name", "first_name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(coverage_end__isnull=True) | Q(coverage_end__gte=models.F("coverage_start")),
                name="coverage_end_after_start",
            )
        ]

    def __str__(self):
        return f"{self.full_name} ({self.masked_member_id})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def masked_member_id(self):
        """Show only the last 3 digits in lists, the way card numbers are masked."""
        return f"{self.member_id[0]}{'*' * (len(self.member_id) - 4)}{self.member_id[-3:]}"

    def is_covered_on(self, day):
        return self.coverage_start <= day and (self.coverage_end is None or day <= self.coverage_end)


class DiagnosisCode(models.Model):
    """ICD-10-CM code: why the patient was treated."""

    code = models.CharField(max_length=8, unique=True)
    description = models.CharField(max_length=255)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} – {self.description}"


class ProcedureCode(models.Model):
    """CPT code: what was done. Carries the fee schedule (max allowed per unit)."""

    code = models.CharField(max_length=5, unique=True)
    description = models.CharField(max_length=255)
    allowed_amount = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    requires_review = models.BooleanField(
        default=False, help_text="Expensive or prior-authorisation procedures always get human review."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} – {self.description}"


class Claim(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        IN_REVIEW = "in_review", "In review"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"

    # Every legal status change. Anything not listed is rejected by the service layer.
    TRANSITIONS = {
        Status.DRAFT: {Status.SUBMITTED},
        Status.SUBMITTED: {Status.IN_REVIEW, Status.APPROVED, Status.DENIED},
        Status.IN_REVIEW: {Status.APPROVED, Status.DENIED},
        Status.APPROVED: set(),
        Status.DENIED: set(),
    }
    FINAL_STATUSES = {Status.APPROVED, Status.DENIED}

    claim_number = models.CharField(max_length=20, unique=True, editable=False)
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="claims")
    provider = models.ForeignKey("accounts.Provider", on_delete=models.PROTECT, related_name="claims")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_claims"
    )
    service_date = models.DateField()
    diagnoses = models.ManyToManyField(DiagnosisCode, related_name="claims")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    total_billed = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_allowed = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Reviewer work queue: "all claims in review, oldest first".
            models.Index(fields=["status", "submitted_at"]),
            # Provider dashboard: "my organisation's claims by status".
            models.Index(fields=["provider", "status"]),
            # Duplicate detection looks up by member + date.
            models.Index(fields=["member", "service_date"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(total_billed__gte=0), name="claim_billed_non_negative"),
        ]

    def __str__(self):
        return self.claim_number

    def save(self, *args, **kwargs):
        if not self.claim_number:
            self.claim_number = self._next_claim_number()
        super().save(*args, **kwargs)

    @staticmethod
    def _next_claim_number():
        # CLM-<year>-<6 random hex chars>: unguessable, and avoids exposing
        # how many claims exist (which a sequential number would leak).
        while True:
            number = f"CLM-{timezone.now():%Y}-{secrets.token_hex(3).upper()}"
            if not Claim.objects.filter(claim_number=number).exists():
                return number

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    @property
    def is_final(self):
        return self.status in self.FINAL_STATUSES

    def can_transition_to(self, new_status):
        return new_status in self.TRANSITIONS[self.Status(self.status)]

    def recalculate_total(self):
        self.total_billed = sum((line.billed_amount for line in self.lines.all()), Decimal("0"))


class ClaimLine(models.Model):
    """One billed service on a claim, e.g. '1 × 99213 office visit, billed $120'."""

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="lines")
    procedure = models.ForeignKey(ProcedureCode, on_delete=models.PROTECT)
    units = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    billed_amount = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    allowed_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(units__gte=1), name="line_units_positive"),
            models.CheckConstraint(condition=Q(billed_amount__gt=0), name="line_billed_positive"),
        ]

    def __str__(self):
        return f"{self.units} × {self.procedure.code}"


class ClaimEvent(models.Model):
    """
    Append-only audit trail. Every status change records who did what and when.

    Rows are never updated or deleted, which is what auditors (and HIPAA-style
    compliance) expect from a history of decisions about patient data.
    """

    # PROTECT: a claim with history can never be deleted, so neither can its audit trail.
    claim = models.ForeignKey(Claim, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        help_text="Empty when the action was taken by the automated rules engine.",
    )
    action = models.CharField(max_length=40)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    note = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    ACTION_LABELS = {
        "created": "Draft created",
        "submitted": "Submitted",
        "auto_approved": "Auto-approved",
        "auto_denied": "Auto-denied",
        "sent_to_review": "Sent to review",
        "approved": "Approved by reviewer",
        "denied": "Denied by reviewer",
    }

    def __str__(self):
        return f"{self.claim} {self.action} at {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def action_label(self):
        return self.ACTION_LABELS.get(self.action, self.action)

    @property
    def to_status_label(self):
        return Claim.Status(self.to_status).label if self.to_status else ""

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("Audit events are append-only and cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events cannot be deleted.")
