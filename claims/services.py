"""
Business logic for the claim lifecycle.

Views and the REST API both call these functions, so the rules about who can
do what, and the audit trail, live in exactly one place.
"""

import logging
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import Claim, ClaimEvent
from .rules import AdjudicationEngine, Outcome

logger = logging.getLogger(__name__)


class InvalidTransition(Exception):
    pass


def visible_claims(user):
    """Row-level access control: which claims may this user see?"""
    qs = Claim.objects.select_related("member", "provider", "created_by")
    if user.is_reviewer:
        # Reviewers never see providers' unfinished drafts.
        return qs.exclude(status=Claim.Status.DRAFT)
    if user.is_provider_staff and user.provider_id:
        return qs.filter(provider_id=user.provider_id)
    return qs.none()


def can_edit(user, claim):
    return claim.is_editable and user.is_provider_staff and user.provider_id == claim.provider_id


def can_decide(user, claim):
    # Separation of duties: the person who created a claim can never decide it.
    return claim.status == Claim.Status.IN_REVIEW and user.is_reviewer and user.pk != claim.created_by_id


def _transition(claim, new_status, *, actor, action, note="", details=None):
    if not claim.can_transition_to(new_status):
        raise InvalidTransition(f"Cannot move claim from {claim.get_status_display()} to {Claim.Status(new_status).label}.")
    old_status = claim.status
    claim.status = new_status
    ClaimEvent.objects.create(
        claim=claim,
        actor=actor,
        action=action,
        from_status=old_status,
        to_status=new_status,
        note=note,
        details=details or {},
    )
    logger.info("claim %s: %s -> %s (%s)", claim.claim_number, old_status, new_status, action)


def price_claim(claim):
    """Allowed amount per line = min(billed, fee schedule × units)."""
    total = Decimal("0")
    for line in claim.lines.select_related("procedure"):
        line.allowed_amount = min(line.billed_amount, line.procedure.allowed_amount * line.units)
        line.save(update_fields=["allowed_amount"])
        total += line.allowed_amount
    claim.total_allowed = total


def record_creation(claim, user):
    ClaimEvent.objects.create(claim=claim, actor=user, action="created", to_status=claim.status)


@transaction.atomic
def submit_claim(claim_id, user, engine=None):
    """
    Submit a draft and run auto-adjudication.

    select_for_update() locks the row so two simultaneous submits (double
    click, two tabs) can't both pass the status check.
    """
    claim = Claim.objects.select_for_update().get(pk=claim_id)
    if not can_edit(user, claim):
        raise PermissionDenied("Only staff of the billing provider can submit a draft claim.")
    if not claim.lines.exists():
        raise InvalidTransition("A claim needs at least one service line.")
    if not claim.diagnoses.exists():
        raise InvalidTransition("A claim needs at least one diagnosis code.")

    claim.recalculate_total()
    claim.submitted_at = timezone.now()
    _transition(claim, Claim.Status.SUBMITTED, actor=user, action="submitted")

    result = (engine or AdjudicationEngine()).run(claim)
    details = result.as_dict()
    reason = " ".join(result.reasons)

    if result.outcome == Outcome.DENY:
        claim.decision_reason = reason
        claim.decided_at = timezone.now()
        _transition(claim, Claim.Status.DENIED, actor=None, action="auto_denied", note=reason, details=details)
    elif result.outcome == Outcome.REVIEW:
        _transition(claim, Claim.Status.IN_REVIEW, actor=None, action="sent_to_review", note=reason, details=details)
    else:
        price_claim(claim)
        claim.decision_reason = "All automated checks passed."
        claim.decided_at = timezone.now()
        _transition(claim, Claim.Status.APPROVED, actor=None, action="auto_approved", details=details)

    claim.save()
    return claim


@transaction.atomic
def decide_claim(claim_id, user, *, approve, reason):
    """A reviewer approves or denies a claim that is in review."""
    claim = Claim.objects.select_for_update().get(pk=claim_id)
    if not can_decide(user, claim):
        raise PermissionDenied("You can't decide this claim.")
    reason = (reason or "").strip()
    if not approve and not reason:
        raise InvalidTransition("A reason is required when denying a claim.")

    if approve:
        price_claim(claim)
        new_status, action = Claim.Status.APPROVED, "approved"
    else:
        new_status, action = Claim.Status.DENIED, "denied"

    claim.decision_reason = reason or "Approved by reviewer."
    claim.decided_at = timezone.now()
    _transition(claim, new_status, actor=user, action=action, note=reason)
    claim.save()
    return claim
