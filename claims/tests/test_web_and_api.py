from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from claims.models import Claim

from . import factories as f


class WebViewTests(TestCase):
    def setUp(self):
        self.staff = f.user()
        self.reviewer = f.user(role=User.Role.REVIEWER)
        self.member = f.member()
        f.diagnosis("I10")
        f.procedure("99213")

    def test_login_required(self):
        response = self.client.get(reverse("claims:dashboard"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_dashboard_and_list_render(self):
        f.claim(self.staff)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("claims:dashboard")), "Total claims")
        self.assertContains(self.client.get(reverse("claims:list")), "CLM-")

    def test_member_id_is_masked_in_list(self):
        f.claim(self.staff, member_obj=self.member)
        self.client.force_login(self.staff)
        html = self.client.get(reverse("claims:list")).content.decode()
        self.assertNotIn(self.member.member_id, html)
        self.assertIn(self.member.masked_member_id, html)

    def test_create_claim_through_form(self):
        self.client.force_login(self.staff)
        dx = f.diagnosis("I10")
        cpt = f.procedure("99213")
        response = self.client.post(reverse("claims:create"), {
            "member_id": self.member.member_id.lower(),
            "service_date": (timezone.localdate() - timedelta(days=2)).isoformat(),
            "diagnoses": [dx.pk],
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "1", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-procedure": cpt.pk, "lines-0-units": "1", "lines-0-billed_amount": "110.00",
            "lines-1-procedure": "", "lines-1-units": "1", "lines-1-billed_amount": "",
        })
        claim = Claim.objects.get()
        self.assertRedirects(response, reverse("claims:detail", args=[claim.claim_number]))
        self.assertEqual(claim.status, Claim.Status.DRAFT)
        self.assertEqual(str(claim.total_billed), "110.00")
        self.assertEqual(claim.lines.count(), 1)

    def test_unknown_member_id_is_rejected(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("claims:create"), {"member_id": "M999999", "lines-TOTAL_FORMS": "0", "lines-INITIAL_FORMS": "0"})
        self.assertContains(response, "No member found")

    def test_reviewer_cannot_create_claims(self):
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("claims:create")).status_code, 403)

    def test_other_providers_claim_is_404(self):
        claim = f.claim(self.staff)
        self.client.force_login(f.user())
        self.assertEqual(self.client.get(reverse("claims:detail", args=[claim.claim_number])).status_code, 404)

    def test_submit_and_decide_via_web(self):
        f.procedure("72148", fee="250", requires_review=True)
        claim = f.claim(self.staff, lines=[("72148", 1, "900")])
        self.client.force_login(self.staff)
        self.client.post(reverse("claims:submit", args=[claim.claim_number]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, Claim.Status.IN_REVIEW)

        self.client.force_login(self.reviewer)
        self.client.post(reverse("claims:decide", args=[claim.claim_number]), {"decision": "deny", "reason": "No prior auth."})
        claim.refresh_from_db()
        self.assertEqual(claim.status, Claim.Status.DENIED)

    def test_submit_requires_post(self):
        claim = f.claim(self.staff)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("claims:submit", args=[claim.claim_number])).status_code, 405)


class ApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.staff = f.user()
        self.reviewer = f.user(role=User.Role.REVIEWER)
        self.member = f.member()
        f.diagnosis("I10")
        f.procedure("99213")
        f.procedure("72148", fee="250", requires_review=True)

    def payload(self, cpt="99213"):
        return {
            "member_id": self.member.member_id,
            "service_date": (timezone.localdate() - timedelta(days=2)).isoformat(),
            "diagnoses": ["I10"],
            "lines": [{"procedure": cpt, "units": 1, "billed_amount": "500.00"}],
        }

    def test_requires_authentication(self):
        self.assertEqual(self.api.get("/api/claims/").status_code, 401)

    def test_full_lifecycle(self):
        self.api.force_authenticate(self.staff)
        created = self.api.post("/api/claims/", self.payload("72148"), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        number = created.data["claim_number"]
        self.assertEqual(created.data["status"], "draft")

        submitted = self.api.post(f"/api/claims/{number}/submit/")
        self.assertEqual(submitted.data["status"], "in_review")

        self.api.force_authenticate(self.reviewer)
        decided = self.api.post(f"/api/claims/{number}/decide/", {"approve": True}, format="json")
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.data["status"], "approved")
        self.assertEqual(decided.data["total_allowed"], "250.00")
        self.assertEqual([e["action"] for e in decided.data["events"]],
                         ["created", "submitted", "sent_to_review", "approved"])

    def test_validation_errors(self):
        self.api.force_authenticate(self.staff)
        bad = self.payload()
        bad["lines"] = []
        bad["diagnoses"] = ["NOPE"]
        response = self.api.post("/api/claims/", bad, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("lines", response.data)
        self.assertIn("diagnoses", response.data)

    def test_invalid_transition_returns_409(self):
        self.api.force_authenticate(self.staff)
        number = self.api.post("/api/claims/", self.payload("72148"), format="json").data["claim_number"]
        self.api.post(f"/api/claims/{number}/submit/")
        self.api.force_authenticate(self.reviewer)
        response = self.api.post(f"/api/claims/{number}/decide/", {"approve": False, "reason": ""}, format="json")
        self.assertEqual(response.status_code, 409)

    def test_reviewer_cannot_create(self):
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.post("/api/claims/", self.payload(), format="json").status_code, 403)

    def test_list_is_scoped_to_provider(self):
        f.claim(f.user())  # someone else's claim
        mine = f.claim(self.staff)
        self.api.force_authenticate(self.staff)
        results = self.api.get("/api/claims/").data["results"]
        self.assertEqual([c["claim_number"] for c in results], [mine.claim_number])
