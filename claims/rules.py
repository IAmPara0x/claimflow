"""
Auto-adjudication rules engine.

Each rule is a small class with one job (Strategy pattern). The engine runs
every rule against a claim and combines the outcomes:

    any DENY   -> claim is denied automatically
    any REVIEW -> claim goes to a human reviewer
    all PASS   -> claim is approved automatically

Adding a new business rule means writing one new class and adding it to
DEFAULT_RULES; nothing else changes (open/closed principle).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from enum import IntEnum

from django.conf import settings
from django.utils import timezone


class Outcome(IntEnum):
    # Ordered by severity so the engine can take max() of all outcomes.
    PASS = 0
    REVIEW = 1
    DENY = 2


@dataclass(frozen=True)
class RuleResult:
    rule: str
    outcome: Outcome
    message: str = ""


@dataclass
class AdjudicationResult:
    outcome: Outcome
    results: list[RuleResult] = field(default_factory=list)

    @property
    def reasons(self):
        """Messages from the rules that caused the final outcome."""
        return [r.message for r in self.results if r.outcome == self.outcome and r.message]

    def as_dict(self):
        return {
            "outcome": self.outcome.name,
            "rules": [{"rule": r.rule, "outcome": r.outcome.name, "message": r.message} for r in self.results],
        }


class Rule(ABC):
    """Base class for all adjudication rules."""

    code: str = ""

    @abstractmethod
    def evaluate(self, claim) -> RuleResult:
        ...

    def passed(self):
        return RuleResult(self.code, Outcome.PASS)

    def review(self, message):
        return RuleResult(self.code, Outcome.REVIEW, message)

    def deny(self, message):
        return RuleResult(self.code, Outcome.DENY, message)


class EligibilityRule(Rule):
    """The member must have active coverage on the date of service."""

    code = "ELIGIBILITY"

    def evaluate(self, claim):
        if claim.member.is_covered_on(claim.service_date):
            return self.passed()
        return self.deny(f"Member had no active coverage on {claim.service_date:%d %b %Y}.")


class TimelyFilingRule(Rule):
    """Service date can't be in the future or older than the filing limit."""

    code = "TIMELY_FILING"

    def evaluate(self, claim):
        today = timezone.localdate()
        limit = settings.CLAIMFLOW["TIMELY_FILING_DAYS"]
        if claim.service_date > today:
            return self.deny("Service date is in the future.")
        if claim.service_date < today - timedelta(days=limit):
            return self.deny(f"Claim filed more than {limit} days after the service date.")
        return self.passed()


class DuplicateClaimRule(Rule):
    """Deny if the same member, provider, date and procedures were already claimed."""

    code = "DUPLICATE"

    def evaluate(self, claim):
        from .models import Claim

        procedures = sorted(claim.lines.values_list("procedure__code", flat=True))
        candidates = (
            Claim.objects.filter(
                member=claim.member,
                provider=claim.provider,
                service_date=claim.service_date,
            )
            .exclude(pk=claim.pk)
            .exclude(status__in=[Claim.Status.DRAFT, Claim.Status.DENIED])
            .prefetch_related("lines__procedure")
        )
        for other in candidates:
            if sorted(line.procedure.code for line in other.lines.all()) == procedures:
                return self.deny(f"Duplicate of claim {other.claim_number}.")
        return self.passed()


class HighCostRule(Rule):
    """Expensive claims always get a human look."""

    code = "HIGH_COST"

    def evaluate(self, claim):
        threshold = settings.CLAIMFLOW["HIGH_COST_THRESHOLD"]
        if claim.total_billed > threshold:
            return self.review(f"Billed amount ${claim.total_billed:,.2f} exceeds ${threshold:,} threshold.")
        return self.passed()


class ProcedureReviewRule(Rule):
    """Some procedures (e.g. MRI) need manual review / prior authorisation."""

    code = "PROCEDURE_REVIEW"

    def evaluate(self, claim):
        flagged = [line.procedure.code for line in claim.lines.all() if line.procedure.requires_review]
        if flagged:
            return self.review(f"Procedure(s) {', '.join(flagged)} require manual review.")
        return self.passed()


class UnitsLimitRule(Rule):
    """Unusually high unit counts on a single line are a common billing error."""

    code = "UNITS_LIMIT"
    max_units = 10

    def evaluate(self, claim):
        if any(line.units > self.max_units for line in claim.lines.all()):
            return self.review(f"A line has more than {self.max_units} units.")
        return self.passed()


DEFAULT_RULES = [
    EligibilityRule(),
    TimelyFilingRule(),
    DuplicateClaimRule(),
    HighCostRule(),
    ProcedureReviewRule(),
    UnitsLimitRule(),
]


class AdjudicationEngine:
    def __init__(self, rules=None):
        self.rules = rules if rules is not None else DEFAULT_RULES

    def run(self, claim) -> AdjudicationResult:
        results = [rule.evaluate(claim) for rule in self.rules]
        outcome = max((r.outcome for r in results), default=Outcome.PASS)
        return AdjudicationResult(outcome=outcome, results=results)
