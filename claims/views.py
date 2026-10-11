from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import services
from .forms import ClaimFilterForm, ClaimForm, ClaimLineFormSet, DecisionForm
from .models import Claim, ClaimEvent


@login_required
def dashboard(request):
    claims = services.visible_claims(request.user)
    # One aggregate query instead of one COUNT per status.
    stats = claims.aggregate(
        total=Count("id"),
        drafts=Count("id", filter=Q(status=Claim.Status.DRAFT)),
        in_review=Count("id", filter=Q(status=Claim.Status.IN_REVIEW)),
        approved=Count("id", filter=Q(status=Claim.Status.APPROVED)),
        denied=Count("id", filter=Q(status=Claim.Status.DENIED)),
        billed=Sum("total_billed"),
        allowed=Sum("total_allowed", filter=Q(status=Claim.Status.APPROVED)),
    )
    decided = (stats["approved"] or 0) + (stats["denied"] or 0)
    stats["approval_rate"] = round(100 * stats["approved"] / decided) if decided else None

    auto_decided = ClaimEvent.objects.filter(
        claim__in=claims, action__in=["auto_approved", "auto_denied"]
    ).count()
    stats["auto_rate"] = round(100 * auto_decided / decided) if decided else None

    review_queue = claims.filter(status=Claim.Status.IN_REVIEW).order_by("submitted_at")[:8]
    recent_events = (
        ClaimEvent.objects.filter(claim__in=claims)
        .select_related("claim", "actor")
        .order_by("-created_at")[:10]
    )
    return render(request, "claims/dashboard.html", {
        "stats": stats,
        "review_queue": review_queue,
        "recent_events": recent_events,
    })


@login_required
def claim_list(request):
    form = ClaimFilterForm(request.GET or None)
    claims = services.visible_claims(request.user)
    if form.is_valid():
        if q := form.cleaned_data["q"].strip():
            claims = claims.filter(Q(claim_number__icontains=q) | Q(member__member_id__iexact=q))
        if status := form.cleaned_data["status"]:
            claims = claims.filter(status=status)
    page = Paginator(claims, 15).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "claims/claim_list.html", {"form": form, "page": page, "query": query.urlencode()})


@login_required
def claim_detail(request, claim_number):
    claim = get_object_or_404(services.visible_claims(request.user), claim_number=claim_number)
    return render(request, "claims/claim_detail.html", {
        "claim": claim,
        "lines": claim.lines.select_related("procedure"),
        "events": claim.events.select_related("actor"),
        "can_edit": services.can_edit(request.user, claim),
        "can_decide": services.can_decide(request.user, claim),
        "decision_form": DecisionForm(),
    })


def _save_claim(request, claim, template):
    form = ClaimForm(request.POST or None, instance=claim)
    formset = ClaimLineFormSet(request.POST or None, instance=claim)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        is_new = claim.pk is None
        with transaction.atomic():
            claim = form.save()
            formset.instance = claim
            formset.save()
            claim.recalculate_total()
            claim.save(update_fields=["total_billed", "updated_at"])
            if is_new:
                services.record_creation(claim, request.user)
        messages.success(request, f"Draft {claim.claim_number} saved. Submit it when it's ready.")
        return redirect("claims:detail", claim_number=claim.claim_number)
    return render(request, template, {"form": form, "formset": formset, "claim": claim})


@login_required
def claim_create(request):
    if not request.user.is_provider_staff:
        raise PermissionDenied("Only provider staff can create claims.")
    claim = Claim(provider=request.user.provider, created_by=request.user)
    return _save_claim(request, claim, "claims/claim_form.html")


@login_required
def claim_edit(request, claim_number):
    claim = get_object_or_404(services.visible_claims(request.user), claim_number=claim_number)
    if not services.can_edit(request.user, claim):
        raise PermissionDenied("Only drafts can be edited.")
    return _save_claim(request, claim, "claims/claim_form.html")


@login_required
@require_POST
def claim_submit(request, claim_number):
    claim = get_object_or_404(services.visible_claims(request.user), claim_number=claim_number)
    try:
        claim = services.submit_claim(claim.pk, request.user)
    except services.InvalidTransition as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{claim.claim_number} submitted: {claim.get_status_display()}.")
    return redirect("claims:detail", claim_number=claim.claim_number)


@login_required
@require_POST
def claim_decide(request, claim_number):
    claim = get_object_or_404(services.visible_claims(request.user), claim_number=claim_number)
    form = DecisionForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, " ".join(errors))
        return redirect("claims:detail", claim_number=claim.claim_number)
    try:
        claim = services.decide_claim(
            claim.pk, request.user,
            approve=form.cleaned_data["decision"] == "approve",
            reason=form.cleaned_data["reason"],
        )
    except services.InvalidTransition as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{claim.claim_number} {claim.get_status_display().lower()}.")
    return redirect("claims:detail", claim_number=claim.claim_number)
