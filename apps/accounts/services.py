"""Identity (FR01–FR04): registration, contact verification, roles and facilitator approval."""
import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.core.errors import Forbidden, ValidationFailed

from .models import (
    PHONE_RE,
    ContactVerification,
    FacilitatorProfile,
    ParticipantProfile,
    User,
    UserRole,
    normalize_email,
    normalize_phone,
)

logger = logging.getLogger("ctt.accounts")
VERIFY_TTL = timedelta(hours=24)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@transaction.atomic
def register_participant(*, full_name: str, email: str | None, phone: str | None, password: str,
                         date_of_birth=None, gender=None, district=None) -> User:
    """Public registration grants the participant role only — never facilitator approval or admin."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    errors = {}
    full_name = (full_name or "").strip()
    email = normalize_email(email)
    phone = normalize_phone(phone)
    if not full_name:
        errors["full_name"] = "Enter your name."
    if not email and not phone:
        errors["email"] = "Enter an email address or a phone number."
    if email:
        try:
            validate_email(email)
        except ValidationError:
            errors["email"] = "Enter a valid email address."
        else:
            if User.objects.filter(email__iexact=email).exists():
                errors["email"] = "An account already uses this email. Sign in or recover your password."
    if phone:
        if not PHONE_RE.match(phone):
            errors["phone"] = "Enter a phone number like 0772 123456 or +256772123456."
        elif User.objects.filter(phone_e164=phone).exists():
            errors["phone"] = "An account already uses this phone number."
    try:
        validate_password(password or "", User(full_name=full_name, email=email))
    except ValidationError as exc:
        errors["password"] = " ".join(exc.messages)
    if errors:
        raise ValidationFailed("Please correct the highlighted fields.", fields=errors)
    try:
        user = User.objects.create_user(full_name=full_name, email=email, phone_e164=phone, password=password)
    except IntegrityError:
        # A concurrent registration with the same contact won the race.
        raise ValidationFailed("An account already uses this contact.", fields={"email": "Already registered."})
    ParticipantProfile.objects.create(user=user, date_of_birth=date_of_birth or None, gender=gender or None, district=district)
    return user


def start_verification(user: User, channel: str, request=None) -> None:
    """Send a single-use token. Only its digest is stored. Tokens are never logged."""
    if channel == "email" and user.email:
        token = secrets.token_urlsafe(32)
        ContactVerification.objects.create(
            user=user, channel="email", contact_value=user.email, token_digest=_digest(token), expires_at=timezone.now() + VERIFY_TTL
        )
        path = reverse("accounts:verify_email", args=[token])
        link = request.build_absolute_uri(path) if request else path
        send_mail(
            "Confirm your email for Called to Thrive",
            f"Hello {user.full_name},\n\nConfirm your email address by opening this link within 24 hours:\n{link}\n\n"
            "If you did not create this account you can ignore this message.",
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
        logger.info("verification_sent user=%s channel=email", user.pk)
    elif channel == "phone" and user.phone_e164:
        code = f"{secrets.randbelow(10**6):06d}"
        ContactVerification.objects.create(
            user=user, channel="phone", contact_value=user.phone_e164, token_digest=_digest(f"{user.pk}:{code}"),
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        send_phone_code(user.phone_e164, code)
        logger.info("verification_sent user=%s channel=phone", user.pk)


def send_phone_code(phone: str, code: str) -> None:
    """D10: phone OTP needs an approved SMS provider. Until one is chosen, development prints to the console
    (like Django's console email backend). Production refuses rather than silently failing."""
    backend = getattr(settings, "PHONE_OTP_BACKEND", "console")
    if backend == "console":
        print(f"[phone-otp console backend] code for {phone[:4]}…{phone[-2:]}: {code}")  # noqa: T201 - dev only
        return
    raise RuntimeError("No SMS provider is configured for phone verification.")


@transaction.atomic
def verify_email_token(token: str) -> User | None:
    v = ContactVerification.objects.select_for_update().filter(token_digest=_digest(token), channel="email").select_related("user").first()
    if v is None or v.used_at or v.expires_at <= timezone.now():
        return None
    user = v.user
    if (user.email or "").lower() != v.contact_value.lower():
        return None  # contact changed since the token was issued
    v.used_at = timezone.now()
    v.save(update_fields=["used_at"])
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    return user


@transaction.atomic
def verify_phone_code(user: User, code: str) -> bool:
    v = ContactVerification.objects.select_for_update().filter(
        user=user, channel="phone", token_digest=_digest(f"{user.pk}:{(code or '').strip()}")
    ).first()
    if v is None or v.used_at or v.expires_at <= timezone.now() or v.contact_value != user.phone_e164:
        return False
    v.used_at = timezone.now()
    v.save(update_fields=["used_at"])
    user.phone_verified_at = timezone.now()
    user.save(update_fields=["phone_verified_at"])
    return True


@transaction.atomic
def apply_as_facilitator(user: User, note: str = "") -> FacilitatorProfile:
    """Adds the facilitator role with a pending profile. Pending facilitators cannot operate cohorts (FR03)."""
    UserRole.objects.get_or_create(user=user, role=UserRole.FACILITATOR)
    profile, created = FacilitatorProfile.objects.get_or_create(user=user, defaults={"application_note": (note or "").strip()})
    if not created and profile.approval_status == FacilitatorProfile.REJECTED:
        profile.approval_status = FacilitatorProfile.PENDING
        profile.application_note = (note or "").strip()
        profile.save(update_fields=["approval_status", "application_note", "updated_at"])
    user._role_cache = None
    return profile


@transaction.atomic
def set_facilitator_status(admin: User, profile: FacilitatorProfile, status: str) -> FacilitatorProfile:
    if not admin.is_admin:
        raise Forbidden("Only administrators approve facilitators.")
    if status not in {FacilitatorProfile.APPROVED, FacilitatorProfile.REJECTED, FacilitatorProfile.SUSPENDED, FacilitatorProfile.PENDING}:
        raise ValidationFailed("Unknown status.")
    profile.approval_status = status
    if status == FacilitatorProfile.APPROVED:
        profile.approved_by = admin
        profile.approved_at = timezone.now()
    profile.save()
    logger.info("facilitator_status user=%s status=%s by=%s", profile.user_id, status, admin.pk)
    return profile


@transaction.atomic
def set_affiliation(admin: User, partner, facilitator: User, active: bool):
    from apps.programme.models import PartnerFacilitator

    if not admin.is_admin:
        raise Forbidden("Only administrators manage affiliations.")
    if not facilitator.has_role(UserRole.FACILITATOR):
        raise ValidationFailed("That account has not applied as a facilitator.")
    affiliation, _ = PartnerFacilitator.objects.get_or_create(partner=partner, facilitator_user=facilitator)
    affiliation.status = "active" if active else "inactive"
    affiliation.save()
    return affiliation


@transaction.atomic
def grant_admin(admin: User, user: User) -> None:
    if not admin.is_admin:
        raise Forbidden("Only administrators can grant the admin role.")
    UserRole.objects.get_or_create(user=user, role=UserRole.ADMIN)
    logger.info("role_granted user=%s role=admin by=%s", user.pk, admin.pk)
