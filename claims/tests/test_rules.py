from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from claims import services
from claims.models import Claim
from claims.rules import (
    AdjudicationEngine, DuplicateClaimRule, EligibilityRule, HighCostRule,
    Outcome, ProcedureReviewRule, Rule, TimelyFilingRule, UnitsLimitRule,
)

from . import factories as f


class RuleTests(TestCase):
    def setUp(self):
        self.staff = f.user()

    def test_eligibility_denies_when_coverage_ended(self):
        lapsed = f.member(coverage_end=timezone.localdate() - timedelta(days=30))
        claim = f.claim(self.staff, member_obj=lapsed)
        self.assertEqual(EligibilityRule().evaluate(claim).outcome, Outcome.DENY)

    def test_eligibility_passes_on_last_covered_day(self):
        day = timezone.localdate() - timedelta(days=10)
        claim = f.claim(self.staff, member_obj=f.member(coverage_end=day), service_date=day)
        self.assertEqual(EligibilityRule().evaluate(claim).outcome, Outcome.PASS)

    def test_timely_filing_rejects_future_and_stale_dates(self):
        future = f.claim(self.staff, service_date=timezone.localdate() + timedelta(days=1))
        stale = f.claim(self.staff, service_date=timezone.localdate() - timedelta(days=400))
        self.assertEqual(TimelyFilingRule().evaluate(future).outcome, Outcome.DENY)
        self.assertEqual(TimelyFilingRule().evaluate(stale).outcome, Outcome.DENY)

    @override_settings(CLAIMFLOW={"HIGH_COST_THRESHOLD": 1000, "TIMELY_FILING_DAYS": 365})
    def test_high_cost_threshold_is_configurable(self):
        claim = f.claim(self.staff, lines=[("99213", 1, "1000.01")])
        self.assertEqual(HighCostRule().evaluate(claim).outcome, Outcome.REVIEW)

    def test_procedure_review_flag(self):
        f.procedure("72148", fee="250", requires_review=True)
        claim = f.claim(self.staff, lines=[("72148", 1, "900")])
        result = ProcedureReviewRule().evaluate(claim)
        self.assertEqual(result.outcome, Outcome.REVIEW)
        self.assertIn("72148", result.message)

    def test_units_limit(self):
        claim = f.claim(self.staff, lines=[("97110", 11, "385")])
        self.assertEqual(UnitsLimitRule().evaluate(claim).outcome, Outcome.REVIEW)

    def test_duplicate_detection_ignores_denied_and_draft_claims(self):
        m = f.member()
        day = timezone.localdate() - timedelta(days=3)
        original = f.claim(self.staff, member_obj=m, service_date=day)
        services.submit_claim(original.pk, self.staff)

        repeat = f.claim(self.staff, member_obj=m, service_date=day)
        self.assertEqual(DuplicateClaimRule().evaluate(repeat).outcome, Outcome.DENY)

        Claim.objects.filter(pk=original.pk).update(status=Claim.Status.DENIED)
        self.assertEqual(DuplicateClaimRule().evaluate(repeat).outcome, Outcome.PASS)

    def test_different_procedures_on_same_day_are_not_duplicates(self):
        m = f.member()
        day = timezone.localdate() - timedelta(days=3)
        services.submit_claim(f.claim(self.staff, member_obj=m, service_date=day).pk, self.staff)
        other = f.claim(self.staff, member_obj=m, service_date=day, lines=[("93000", 1, "40")])
        self.assertEqual(DuplicateClaimRule().evaluate(other).outcome, Outcome.PASS)


class EngineTests(TestCase):
    """The engine takes the most severe outcome: DENY > REVIEW > PASS."""

    class Always(Rule):
        def __init__(self, outcome):
            self.outcome = outcome
            self.code = outcome.name

        def evaluate(self, claim):
            return self.passed() if self.outcome == Outcome.PASS else (
                self.review("r") if self.outcome == Outcome.REVIEW else self.deny("d"))

    def test_most_severe_outcome_wins(self):
        P, R, D = (self.Always(o) for o in (Outcome.PASS, Outcome.REVIEW, Outcome.DENY))
        self.assertEqual(AdjudicationEngine([P, P]).run(None).outcome, Outcome.PASS)
        self.assertEqual(AdjudicationEngine([P, R]).run(None).outcome, Outcome.REVIEW)
        self.assertEqual(AdjudicationEngine([R, D, P]).run(None).outcome, Outcome.DENY)

    def test_only_reasons_for_final_outcome_are_reported(self):
        result = AdjudicationEngine([self.Always(Outcome.REVIEW), self.Always(Outcome.DENY)]).run(None)
        self.assertEqual(result.reasons, ["d"])

    def test_rule_base_class_is_abstract(self):
        with self.assertRaises(TypeError):
            Rule()
