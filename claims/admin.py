from django.contrib import admin

from .models import Claim, ClaimEvent, ClaimLine, DiagnosisCode, Member, ProcedureCode


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["member_id", "last_name", "first_name", "coverage_start", "coverage_end"]
    search_fields = ["member_id", "last_name"]


@admin.register(DiagnosisCode)
class DiagnosisCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "description"]
    search_fields = ["code", "description"]


@admin.register(ProcedureCode)
class ProcedureCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "description", "allowed_amount", "requires_review"]
    list_filter = ["requires_review"]


class ClaimLineInline(admin.TabularInline):
    model = ClaimLine
    extra = 0


class ClaimEventInline(admin.TabularInline):
    model = ClaimEvent
    extra = 0
    can_delete = False
    readonly_fields = ["action", "from_status", "to_status", "actor", "note", "created_at"]
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ["claim_number", "provider", "service_date", "status", "total_billed", "total_allowed"]
    list_filter = ["status", "provider"]
    search_fields = ["claim_number", "member__member_id"]
    list_select_related = ["provider"]
    inlines = [ClaimLineInline, ClaimEventInline]
    readonly_fields = ["claim_number", "status", "submitted_at", "decided_at"]


@admin.register(ClaimEvent)
class ClaimEventAdmin(admin.ModelAdmin):
    list_display = ["claim", "action", "from_status", "to_status", "actor", "created_at"]
    list_filter = ["action"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
