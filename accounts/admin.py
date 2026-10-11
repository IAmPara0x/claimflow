from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Provider, User


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "npi", "specialty"]
    search_fields = ["name", "npi"]


@admin.register(User)
class ClaimFlowUserAdmin(UserAdmin):
    list_display = ["username", "email", "role", "provider", "is_active"]
    list_filter = ["role", "is_active"]
    fieldsets = UserAdmin.fieldsets + (("ClaimFlow", {"fields": ("role", "provider")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("ClaimFlow", {"fields": ("role", "provider")}),)
