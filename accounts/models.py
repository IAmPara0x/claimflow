from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models


class Provider(models.Model):
    """A healthcare organisation (clinic, hospital) that submits claims."""

    name = models.CharField(max_length=200)
    # National Provider Identifier: a unique 10-digit number in the US system.
    npi = models.CharField(
        "NPI",
        max_length=10,
        unique=True,
        validators=[RegexValidator(r"^\d{10}$", "NPI must be exactly 10 digits.")],
    )
    specialty = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.npi})"


class User(AbstractUser):
    """
    Custom user with a role.

    Defined at the start of the project because swapping AUTH_USER_MODEL
    after the first migration is very painful in Django.
    """

    class Role(models.TextChoices):
        PROVIDER = "provider", "Provider staff"
        REVIEWER = "reviewer", "Claims reviewer"
        ADMIN = "admin", "Administrator"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PROVIDER)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="staff",
        help_text="Required for provider staff; empty for reviewers and admins.",
    )

    class Meta:
        constraints = [
            # A provider-staff account must belong to a provider organisation.
            models.CheckConstraint(
                condition=~models.Q(role="provider") | models.Q(provider__isnull=False),
                name="provider_staff_has_provider",
            )
        ]

    @property
    def is_provider_staff(self):
        return self.role == self.Role.PROVIDER

    @property
    def is_reviewer(self):
        return self.role in (self.Role.REVIEWER, self.Role.ADMIN)
