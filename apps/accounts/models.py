import re
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from apps.core.models import TimeStampedModel

PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower() or None


def normalize_phone(value: str | None) -> str | None:
    """Normalize to E.164. Ugandan local numbers (07XXXXXXXX) become +2567XXXXXXXX."""
    if not value:
        return None
    digits = re.sub(r"[\s\-().]", "", value.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "+256" + digits[1:]
    if digits.startswith("256") and not digits.startswith("+"):
        digits = "+" + digits
    return digits or None


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, full_name, password=None, email=None, phone_e164=None, **extra):
        email = normalize_email(email)
        phone_e164 = normalize_phone(phone_e164)
        if not email and not phone_e164:
            raise ValueError("An email or phone contact is required.")
        user = self.model(full_name=full_name.strip(), email=email, phone_e164=phone_e164, **extra)
        user.set_password(password)
        user.save(using=self._db)
        UserRole.objects.get_or_create(user=user, role=UserRole.PARTICIPANT)
        return user


class User(AbstractBaseUser):
    STATUS_CHOICES = [("active", "Active"), ("suspended", "Suspended"), ("archived", "Archived")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField(max_length=200)
    email = models.EmailField(null=True, blank=True)
    phone_e164 = models.CharField(max_length=20, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Django's AbstractBaseUser expects a username field; we authenticate by
    # contact in ContactBackend, so the id is the stable identifier.
    USERNAME_FIELD = "id"
    REQUIRED_FIELDS = ["full_name"]

    objects = UserManager()

    class Meta:
        db_table = "users"
        constraints = [
            models.CheckConstraint(
                condition=Q(email__isnull=False) | Q(phone_e164__isnull=False), name="users_contact_required"
            ),
            models.UniqueConstraint(Lower("email"), name="users_email_ci_unique"),
            models.UniqueConstraint(fields=["phone_e164"], name="users_phone_unique"),
            models.CheckConstraint(
                condition=Q(status__in=["active", "suspended", "archived"]), name="users_status_valid"
            ),
        ]

    def __str__(self):
        return self.full_name

    @property
    def is_active(self):
        return self.status == "active"

    @property
    def has_verified_contact(self) -> bool:
        return bool(self.email_verified_at or self.phone_verified_at)

    def role_set(self) -> set[str]:
        cached = getattr(self, "_role_cache", None)
        if cached is None:
            cached = set(self.roles.values_list("role", flat=True))
            self._role_cache = cached
        return cached

    def has_role(self, role: str) -> bool:
        return role in self.role_set()

    @property
    def is_admin(self) -> bool:
        return self.has_role(UserRole.ADMIN)

    @property
    def is_approved_facilitator(self) -> bool:
        if not self.has_role(UserRole.FACILITATOR):
            return False
        profile = getattr(self, "facilitator_profile", None)
        return profile is not None and profile.approval_status == FacilitatorProfile.APPROVED

    def contact_display(self) -> str:
        return self.email or self.phone_e164 or ""


class UserRole(models.Model):
    PARTICIPANT = "participant"
    FACILITATOR = "facilitator"
    ADMIN = "admin"
    ROLE_CHOICES = [(PARTICIPANT, "Participant"), (FACILITATOR, "Facilitator"), (ADMIN, "Administrator")]

    user = models.ForeignKey(User, on_delete=models.RESTRICT, related_name="roles")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_roles"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="user_roles_user_role_unique"),
            models.CheckConstraint(condition=Q(role__in=["participant", "facilitator", "admin"]), name="user_roles_role_valid")
        ]


class ParticipantProfile(TimeStampedModel):
    GENDER_CHOICES = [("female", "Female"), ("male", "Male"), ("prefer_not", "Prefer not to say")]

    user = models.OneToOneField(User, on_delete=models.RESTRICT, related_name="participant_profile")
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, null=True, blank=True)
    district = models.ForeignKey("programme.District", on_delete=models.RESTRICT, null=True, blank=True)

    class Meta:
        db_table = "participant_profiles"


class FacilitatorProfile(TimeStampedModel):
    PENDING, APPROVED, REJECTED, SUSPENDED = "pending", "approved", "rejected", "suspended"
    STATUS_CHOICES = [(PENDING, "Pending"), (APPROVED, "Approved"), (REJECTED, "Rejected"), (SUSPENDED, "Suspended")]

    user = models.OneToOneField(User, on_delete=models.RESTRICT, related_name="facilitator_profile")
    approval_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    approved_by = models.ForeignKey(User, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    application_note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "facilitator_profiles"
        constraints = [
            models.CheckConstraint(
                condition=Q(approval_status__in=["pending", "approved", "rejected", "suspended"]),
                name="facilitator_profiles_status_valid",
            ),
            models.CheckConstraint(
                condition=~Q(approval_status="approved") | (Q(approved_by__isnull=False) & Q(approved_at__isnull=False)),
                name="facilitator_profiles_approval_recorded",
            ),
        ]


class ContactVerification(TimeStampedModel):
    """Single-use verification token, stored as a digest only."""

    CHANNEL_CHOICES = [("email", "Email"), ("phone", "Phone")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verifications")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    contact_value = models.CharField(max_length=254)
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contact_verifications"
