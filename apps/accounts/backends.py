from django.contrib.auth.backends import BaseBackend

from .models import User, normalize_email, normalize_phone


class ContactBackend(BaseBackend):
    """Authenticate with an email address or phone number plus password."""

    def authenticate(self, request, contact=None, password=None, **kwargs):
        if not contact or not password:
            return None
        user = None
        if "@" in contact:
            email = normalize_email(contact)
            user = User.objects.filter(email__iexact=email).first()
        else:
            phone = normalize_phone(contact)
            user = User.objects.filter(phone_e164=phone).first()
        if user is None:
            # Run the hasher anyway to reduce timing differences between unknown and known contacts.
            User().set_password(password)
            return None
        if user.check_password(password) and user.is_active:
            return user
        return None

    def get_user(self, user_id):
        try:
            user = User.objects.select_related("facilitator_profile").get(pk=user_id)
        except (User.DoesNotExist, ValueError):
            return None
        return user if user.is_active else None
