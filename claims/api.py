"""
REST API (Django REST Framework).

  GET    /api/claims/                     list claims visible to the user (paginated, ?status= filter)
  POST   /api/claims/                     create a draft claim with its lines (provider staff)
  GET    /api/claims/{claim_number}/      claim detail with lines and audit trail
  POST   /api/claims/{claim_number}/submit/   submit + auto-adjudicate
  POST   /api/claims/{claim_number}/decide/   reviewer decision {"approve": bool, "reason": str}
  GET    /api/procedures/  /api/diagnoses/    reference codes
"""

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import transaction
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from . import services
from .models import Claim, ClaimEvent, ClaimLine, DiagnosisCode, Member, ProcedureCode


class ProcedureCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcedureCode
        fields = ["code", "description", "allowed_amount", "requires_review"]


class DiagnosisCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiagnosisCode
        fields = ["code", "description"]


class ClaimLineSerializer(serializers.ModelSerializer):
    procedure = serializers.SlugRelatedField(slug_field="code", queryset=ProcedureCode.objects.all())

    class Meta:
        model = ClaimLine
        fields = ["procedure", "units", "billed_amount", "allowed_amount"]
        read_only_fields = ["allowed_amount"]


class ClaimEventSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    class Meta:
        model = ClaimEvent
        fields = ["action", "from_status", "to_status", "actor", "note", "details", "created_at"]

    def get_actor(self, event):
        return event.actor.username if event.actor else "rules-engine"


class ClaimSerializer(serializers.ModelSerializer):
    member_id = serializers.SlugRelatedField(
        source="member", slug_field="member_id", queryset=Member.objects.all()
    )
    diagnoses = serializers.SlugRelatedField(
        slug_field="code", many=True, queryset=DiagnosisCode.objects.all()
    )
    lines = ClaimLineSerializer(many=True)
    provider = serializers.CharField(source="provider.name", read_only=True)

    class Meta:
        model = Claim
        fields = [
            "claim_number", "member_id", "provider", "service_date", "diagnoses", "lines",
            "status", "total_billed", "total_allowed", "decision_reason",
            "created_at", "submitted_at", "decided_at",
        ]
        read_only_fields = [
            "claim_number", "status", "total_billed", "total_allowed", "decision_reason",
            "created_at", "submitted_at", "decided_at",
        ]

    def validate_diagnoses(self, value):
        if not value:
            raise serializers.ValidationError("At least one diagnosis code is required.")
        return value

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("At least one service line is required.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        diagnoses = validated_data.pop("diagnoses")
        user = self.context["request"].user
        claim = Claim.objects.create(provider=user.provider, created_by=user, **validated_data)
        claim.diagnoses.set(diagnoses)
        ClaimLine.objects.bulk_create(ClaimLine(claim=claim, **line) for line in lines)
        claim.recalculate_total()
        claim.save(update_fields=["total_billed"])
        services.record_creation(claim, user)
        return claim


class ClaimDetailSerializer(ClaimSerializer):
    events = ClaimEventSerializer(many=True, read_only=True)

    class Meta(ClaimSerializer.Meta):
        fields = ClaimSerializer.Meta.fields + ["events"]


class DecisionSerializer(serializers.Serializer):
    approve = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ClaimViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    lookup_field = "claim_number"

    def get_queryset(self):
        qs = services.visible_claims(self.request.user).prefetch_related("lines__procedure", "diagnoses")
        if status_filter := self.request.query_params.get("status"):
            qs = qs.filter(status=status_filter)
        return qs

    def get_serializer_class(self):
        return ClaimDetailSerializer if self.action == "retrieve" else ClaimSerializer

    def perform_create(self, serializer):
        if not self.request.user.is_provider_staff:
            raise PermissionDenied("Only provider staff can create claims.")
        serializer.save()

    def _run(self, fn):
        try:
            claim = fn()
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc))
        except services.InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ClaimDetailSerializer(claim, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, claim_number=None):
        claim = self.get_object()
        return self._run(lambda: services.submit_claim(claim.pk, request.user))

    @action(detail=True, methods=["post"])
    def decide(self, request, claim_number=None):
        claim = self.get_object()
        decision = DecisionSerializer(data=request.data)
        decision.is_valid(raise_exception=True)
        return self._run(lambda: services.decide_claim(
            claim.pk, request.user,
            approve=decision.validated_data["approve"],
            reason=decision.validated_data["reason"],
        ))


class ProcedureCodeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ProcedureCode.objects.all()
    serializer_class = ProcedureCodeSerializer
    lookup_field = "code"


class DiagnosisCodeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DiagnosisCode.objects.all()
    serializer_class = DiagnosisCodeSerializer
    lookup_field = "code"
