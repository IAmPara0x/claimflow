"""
Load demo data: providers, users, members, ICD-10/CPT codes and about 30 claims.

Claims are pushed through the real service layer (submit -> rules engine ->
reviewer decision), so the audit trail looks exactly like real usage.

    python manage.py seed_demo          # add demo data
    python manage.py seed_demo --reset  # wipe claims first
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Provider, User
from claims import services
from claims.models import Claim, ClaimEvent, ClaimLine, DiagnosisCode, Member, ProcedureCode

DEMO_PASSWORD = "claimflow-demo"

DIAGNOSES = [
    ("E11.9", "Type 2 diabetes mellitus without complications"),
    ("I10", "Essential (primary) hypertension"),
    ("J06.9", "Acute upper respiratory infection, unspecified"),
    ("M54.50", "Low back pain, unspecified"),
    ("Z00.00", "General adult medical examination without abnormal findings"),
    ("R07.9", "Chest pain, unspecified"),
    ("M17.11", "Unilateral primary osteoarthritis, right knee"),
    ("S93.401A", "Sprain of unspecified ligament of right ankle, initial encounter"),
]

# (code, description, fee schedule per unit, requires review). Fees are illustrative.
PROCEDURES = [
    ("99213", "Office visit, established patient, low complexity", "95.00", False),
    ("99214", "Office visit, established patient, moderate complexity", "135.00", False),
    ("99396", "Preventive visit, established patient, age 40-64", "130.00", False),
    ("80053", "Comprehensive metabolic panel", "15.00", False),
    ("85025", "Complete blood count with differential", "10.00", False),
    ("93000", "Electrocardiogram with interpretation", "20.00", False),
    ("71046", "Chest X-ray, 2 views", "40.00", False),
    ("97110", "Therapeutic exercise, each 15 minutes", "30.00", False),
    ("72148", "MRI lumbar spine without contrast", "250.00", True),
    ("73721", "MRI knee joint without contrast", "260.00", True),
    ("27447", "Total knee arthroplasty", "1600.00", False),
]

FIRST = ["Aarav", "Maya", "James", "Ishita", "Daniel", "Sara", "Rohan", "Emily", "Kabir", "Grace", "Arjun", "Nina", "Leo", "Ananya"]
LAST = ["Mehta", "Collins", "Rao", "Turner", "Iyer", "Brooks", "Nair", "Foster", "Kapoor", "Hughes", "Das", "Reed", "Sen", "Ward"]

# Typical visits: (diagnosis codes, [(procedure, units, billed per unit)])
VISITS = [
    (["I10"], [("99213", 1, "120.00")]),
    (["E11.9"], [("99214", 1, "160.00"), ("80053", 1, "28.00")]),
    (["J06.9"], [("99213", 1, "115.00")]),
    (["Z00.00"], [("99396", 1, "150.00"), ("85025", 1, "18.00"), ("80053", 1, "25.00")]),
    (["R07.9"], [("99214", 1, "170.00"), ("93000", 1, "45.00"), ("71046", 1, "85.00")]),
    (["S93.401A"], [("99213", 1, "125.00")]),
    (["M54.50"], [("99213", 1, "118.00"), ("97110", 4, "35.00")]),
]
REVIEW_VISITS = [
    (["M54.50"], [("99214", 1, "160.00"), ("72148", 1, "1100.00")]),
    (["M17.11"], [("73721", 1, "1250.00")]),
    (["M17.11"], [("27447", 1, "28500.00")]),
    (["M54.50"], [("97110", 12, "35.00")]),
]


class Command(BaseCommand):
    help = "Load demo data for ClaimFlow."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing claims before seeding.")

    @transaction.atomic
    def handle(self, *args, reset=False, **options):
        rng = random.Random(42)
        if reset:
            # Bypasses ClaimEvent.delete() on purpose: wiping demo data is not an audited action.
            ClaimEvent.objects.all()._raw_delete(ClaimEvent.objects.db)
            ClaimLine.objects.all().delete()
            Claim.objects.all().delete()

        for code, desc in DIAGNOSES:
            DiagnosisCode.objects.update_or_create(code=code, defaults={"description": desc})
        for code, desc, fee, review in PROCEDURES:
            ProcedureCode.objects.update_or_create(
                code=code, defaults={"description": desc, "allowed_amount": Decimal(fee), "requires_review": review}
            )

        sunrise, _ = Provider.objects.get_or_create(npi="1467583921", defaults={"name": "Sunrise Family Clinic", "specialty": "Family medicine"})
        lakeside, _ = Provider.objects.get_or_create(npi="1356294870", defaults={"name": "Lakeside Orthopedics", "specialty": "Orthopedics"})

        clinic = self._user("clinic_staff", "Neha", "Verma", User.Role.PROVIDER, sunrise)
        ortho = self._user("ortho_staff", "Mark", "Ellis", User.Role.PROVIDER, lakeside)
        reviewer = self._user("reviewer", "Priya", "Sharma", User.Role.REVIEWER, None)
        admin = self._user("admin", "Site", "Admin", User.Role.ADMIN, None)
        admin.is_staff = admin.is_superuser = True
        admin.save()

        today = timezone.localdate()
        members = []
        for i, (first, last) in enumerate(zip(FIRST, LAST)):
            coverage_end = today - timedelta(days=60) if i == 3 else None  # one member lost coverage
            member, _ = Member.objects.update_or_create(
                member_id=f"M{100201 + i}",
                defaults={
                    "first_name": first, "last_name": last,
                    "date_of_birth": date(1958 + i * 3 % 40, 1 + i % 12, 1 + i * 2 % 27),
                    "coverage_start": date(2024, 1, 1), "coverage_end": coverage_end,
                },
            )
            members.append(member)

        made = 0
        for i in range(30):
            staff = clinic if i % 3 else ortho
            visit = rng.choice(REVIEW_VISITS if i % 5 == 0 else VISITS)
            claim = self._claim(staff, rng.choice(members), today - timedelta(days=rng.randint(2, 90)), visit)
            services.submit_claim(claim.pk, staff)
            made += 1

        # A deliberate duplicate, so the duplicate rule has something to catch.
        first = Claim.objects.filter(status=Claim.Status.APPROVED).order_by("created_at").first()
        if first:
            lines = [(l.procedure.code, l.units, str(l.billed_amount / l.units)) for l in first.lines.all()]
            dx = list(first.diagnoses.values_list("code", flat=True))
            staff = clinic if first.provider == sunrise else ortho
            dup = self._claim(staff, first.member, first.service_date, (dx, lines))
            services.submit_claim(dup.pk, staff)
            made += 1

        # Reviewer works through part of the queue.
        for n, claim in enumerate(Claim.objects.filter(status=Claim.Status.IN_REVIEW).order_by("submitted_at")[:2]):
            approve = n % 2 == 0
            services.decide_claim(
                claim.pk, reviewer, approve=approve,
                reason="Medical necessity documented in attached notes." if approve
                else "Prior authorisation not on file for this procedure.",
            )

        # A couple of drafts still being prepared.
        for visit in VISITS[:2]:
            self._claim(clinic, rng.choice(members), today - timedelta(days=1), visit)
            made += 1

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {made} claims. Log in as clinic_staff, ortho_staff, reviewer or admin "
            f"with password '{DEMO_PASSWORD}'."
        ))

    def _user(self, username, first, last, role, provider):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"first_name": first, "last_name": last, "role": role, "provider": provider,
                      "email": f"{username}@example.com"},
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        return user

    def _claim(self, staff, member, service_date, visit):
        dx_codes, lines = visit
        claim = Claim.objects.create(member=member, provider=staff.provider, created_by=staff, service_date=service_date)
        claim.diagnoses.set(DiagnosisCode.objects.filter(code__in=dx_codes))
        for code, units, per_unit in lines:
            ClaimLine.objects.create(
                claim=claim, procedure=ProcedureCode.objects.get(code=code),
                units=units, billed_amount=Decimal(per_unit) * units,
            )
        claim.recalculate_total()
        claim.save(update_fields=["total_billed"])
        services.record_creation(claim, staff)
        return claim
