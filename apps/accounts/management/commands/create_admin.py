"""Create or promote an administrator. Admin can never be granted through public registration (FR02)."""
import getpass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User, UserRole, normalize_email


class Command(BaseCommand):
    help = "Create an administrator account, or grant the admin role to an existing account."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--name", default="Programme Administrator")
        parser.add_argument("--password", help="Omit to be prompted (recommended).")

    @transaction.atomic
    def handle(self, email, name, password, **options):
        email = normalize_email(email)
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            password = password or getpass.getpass("Password: ")
            if len(password) < 8:
                raise CommandError("Password must be at least 8 characters.")
            user = User.objects.create_user(full_name=name, email=email, password=password)
            user.email_verified_at = timezone.now()
            user.save(update_fields=["email_verified_at"])
        UserRole.objects.get_or_create(user=user, role=UserRole.ADMIN)
        self.stdout.write(self.style.SUCCESS(f"{user.full_name} now has the admin role (user id {user.pk})."))
