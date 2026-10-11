from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from claims import services
from claims.models import Claim, ClaimEvent

from . import factories as f


class SubmitTests(TestCase):
    def setUp(self):
        self.staff = f.user()
        self.reviewer = f.user(role=User.Role.REVIEWER)

    def test_clean_claim_is_auto_approved_and_priced(self):
        # Billed 120 for 99213 whose fee schedule is 95 -> allowed 95.
        claim = services.submit_claim(f.claim(self.staff).pk, self.staff)
        self.assertEqual(claim.status, Claim.Status.APPROVED)
        self.assertEqual(claim.total_allowed, Decimal("95.00"))
        self.assertIsNotNone(claim.decided_at)

    def test_allowed_never_exceeds_billed(self):
        claim = services.submit_claim(f.claim(self.staff, lines=[("99213", 1, "60.00")]).pk, self.staff)
        self.assertEqual(claim.total_allowed, Decimal("60.00"))

    def test_allowed_scales_with_units(self):
        claim = services.submit_claim(f.claim(self.staff, lines=[("97110", 3, "300.00")]).pk, self.staff)
        self.assertEqual(claim.total_allowed, Decimal("285.00"))

    def test_ineligible_member_is_auto_denied_with_reason(self):
        lapsed = f.member(coverage_end=timezone.localdate() - timedelta(days=30))
        claim = services.submit_claim(f.claim(self.staff, member_obj=lapsed).pk, self.staff)
        self.assertEqual(claim.status, Claim.Status.DENIED)
        self.assertIn("no active coverage", claim.decision_reason)
        self.assertIsNone(claim.total_allowed)

    def test_flagged_claim_goes_to_review(self):
        f.procedure("72148", fee="250", requires_review=True)
        claim = services.submit_claim(f.claim(self.staff, lines=[("72148", 1, "900")]).pk, self.staff)
        self.assertEqual(claim.status, Claim.Status.IN_REVIEW)

    def test_audit_trail_records_every_step(self):
        claim = services.submit_claim(f.claim(self.staff).pk, self.staff)
        actions = list(claim.events.values_list("action", "actor"))
        self.assertEqual(actions, [("created", self.staff.pk), ("submitted", self.staff.pk), ("auto_approved", None)])
        self.assertEqual(claim.events.last().details["outcome"], "PASS")

    def test_cannot_submit_twice(self):
        claim = f.claim(self.staff)
        services.submit_claim(claim.pk, self.staff)
        with self.assertRaises(PermissionDenied):  # no longer an editable draft
            services.submit_claim(claim.pk, self.staff)

    def test_other_provider_cannot_submit(self):
        claim = f.claim(self.staff)
        with self.assertRaises(PermissionDenied):
            services.submit_claim(claim.pk, f.user())

    def test_claim_without_lines_cannot_be_submitted(self):
        claim = f.claim(self.staff, lines=[])
        with self.assertRaises(services.InvalidTransition):
            services.submit_claim(claim.pk, self.staff)


class DecisionTests(TestCase):
    def setUp(self):
        self.staff = f.user()
        self.reviewer = f.user(role=User.Role.REVIEWER)
        f.procedure("72148", fee="250", requires_review=True)
        self.claim = services.submit_claim(f.claim(self.staff, lines=[("72148", 1, "900")]).pk, self.staff)

    def test_reviewer_can_approve(self):
        claim = services.decide_claim(self.claim.pk, self.reviewer, approve=True, reason="")
        self.assertEqual(claim.status, Claim.Status.APPROVED)
        self.assertEqual(claim.total_allowed, Decimal("250.00"))
        self.assertEqual(claim.events.last().actor, self.reviewer)

    def test_denial_requires_reason(self):
        with self.assertRaises(services.InvalidTransition):
            services.decide_claim(self.claim.pk, self.reviewer, approve=False, reason="  ")

    def test_provider_staff_cannot_decide(self):
        with self.assertRaises(PermissionDenied):
            services.decide_claim(self.claim.pk, self.staff, approve=True, reason="")

    def test_decided_claim_is_final(self):
        services.decide_claim(self.claim.pk, self.reviewer, approve=False, reason="No prior auth.")
        with self.assertRaises(PermissionDenied):
            services.decide_claim(self.claim.pk, self.reviewer, approve=True, reason="")

    def test_state_machine_rejects_illegal_jumps(self):
        self.assertFalse(Claim(status=Claim.Status.DRAFT).can_transition_to(Claim.Status.APPROVED))
        self.assertFalse(Claim(status=Claim.Status.APPROVED).can_transition_to(Claim.Status.DENIED))


class AuditLogTests(TestCase):
    def test_events_are_append_only(self):
        staff = f.user()
        claim = f.claim(staff)
        event = claim.events.first()
        event.note = "tampered"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        self.assertEqual(ClaimEvent.objects.get(pk=event.pk).note, "")


class VisibilityTests(TestCase):
    def test_row_level_access(self):
        staff_a, staff_b = f.user(), f.user()
        reviewer = f.user(role=User.Role.REVIEWER)
        draft_a = f.claim(staff_a)
        submitted_b = services.submit_claim(f.claim(staff_b).pk, staff_b)

        self.assertEqual(list(services.visible_claims(staff_a)), [draft_a])
        self.assertEqual(list(services.visible_claims(staff_b)), [submitted_b])
        # Reviewers see everything except other people's drafts.
        self.assertEqual(list(services.visible_claims(reviewer)), [submitted_b])
