"""Small helpers that build valid test objects with sensible defaults."""

from datetime import date, timedelta
from decimal import Decimal
from itertools import count

from django.utils import timezone

from accounts.models import Provider, User
from claims import services
from claims.models import Claim, ClaimLine, DiagnosisCode, Member, ProcedureCode

_seq = count(1)


def provider(**kw):
    n = next(_seq)
    return Provider.objects.create(name=kw.get("name", f"Clinic {n}"), npi=kw.get("npi", f"{1000000000 + n}"))


def user(role=User.Role.PROVIDER, provider_obj=None, **kw):
    n = next(_seq)
    if role == User.Role.PROVIDER and provider_obj is None:
        provider_obj = provider()
    return User.objects.create_user(username=kw.get("username", f"user{n}"), password="pw-123456!", role=role, provider=provider_obj)


def member(**kw):
    n = next(_seq)
    defaults = {
        "member_id": f"M{200000 + n}",
        "first_name": "Test",
        "last_name": f"Member{n}",
        "date_of_birth": date(1980, 1, 1),
        "coverage_start": date(2020, 1, 1),
        "coverage_end": None,
    }
    defaults.update(kw)
    return Member.objects.create(**defaults)


def diagnosis(code="I10"):
    return DiagnosisCode.objects.get_or_create(code=code, defaults={"description": "Hypertension"})[0]


def procedure(code="99213", fee="95.00", requires_review=False):
    return ProcedureCode.objects.get_or_create(
        code=code, defaults={"description": f"Procedure {code}", "allowed_amount": Decimal(fee), "requires_review": requires_review}
    )[0]


def claim(staff, *, member_obj=None, service_date=None, lines=(("99213", 1, "120.00"),), dx=("I10",)):
    """A draft claim with lines; `lines` items are (cpt, units, billed_total)."""
    c = Claim.objects.create(
        member=member_obj or member(),
        provider=staff.provider,
        created_by=staff,
        service_date=service_date or timezone.localdate() - timedelta(days=5),
    )
    c.diagnoses.set([diagnosis(code) for code in dx])
    for code, units, billed in lines:
        ClaimLine.objects.create(claim=c, procedure=procedure(code), units=units, billed_amount=Decimal(billed))
    c.recalculate_total()
    c.save()
    services.record_creation(c, staff)
    return c
