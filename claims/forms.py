from django import forms
from django.forms import inlineformset_factory

from .models import Claim, ClaimLine, DiagnosisCode, Member


class ClaimForm(forms.ModelForm):
    # Staff type the member ID from the insurance card instead of picking from
    # a dropdown, so the page never lists every patient (PHI minimisation).
    member_id = forms.CharField(label="Member ID", max_length=12, help_text="As printed on the insurance card, e.g. M100234.")
    diagnoses = forms.ModelMultipleChoiceField(
        queryset=DiagnosisCode.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        help_text="Select at least one ICD-10 code.",
    )

    class Meta:
        model = Claim
        fields = ["service_date", "diagnoses"]
        widgets = {"service_date": forms.DateInput(attrs={"type": "date"})}

    field_order = ["member_id", "service_date", "diagnoses"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["member_id"].initial = self.instance.member.member_id

    def clean_member_id(self):
        member_id = self.cleaned_data["member_id"].strip().upper()
        try:
            self.member = Member.objects.get(member_id=member_id)
        except Member.DoesNotExist:
            raise forms.ValidationError("No member found with this ID.")
        return member_id

    def save(self, commit=True):
        self.instance.member = self.member
        return super().save(commit)


ClaimLineFormSet = inlineformset_factory(
    Claim,
    ClaimLine,
    fields=["procedure", "units", "billed_amount"],
    extra=2,
    min_num=1,
    validate_min=True,
    can_delete=True,
    widgets={"billed_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"})},
)


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Approve"), ("deny", "Deny")])
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def clean(self):
        data = super().clean()
        if data.get("decision") == "deny" and not data.get("reason", "").strip():
            self.add_error("reason", "Give a reason when denying a claim; the provider will see it.")
        return data


class ClaimFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search", widget=forms.TextInput(attrs={"placeholder": "Claim number or member ID"}))
    status = forms.ChoiceField(required=False, choices=[("", "All statuses")] + list(Claim.Status.choices))
