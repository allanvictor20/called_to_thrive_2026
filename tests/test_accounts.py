"""TC-FR01..FR04: accounts, roles, approval, profile."""
import re
from datetime import date, timedelta

import pytest
from django.core import mail

from apps.accounts import services
from apps.accounts.models import FacilitatorProfile, User, UserRole
from apps.cohorts import services as cs
from apps.core.errors import Forbidden, ValidationFailed

from .conftest import PASSWORD, make_facilitator, make_user

pytestmark = pytest.mark.django_db


def register(client, **data):
    base = {"full_name": "Brenda Nakato", "email": "", "phone": "", "password": PASSWORD}
    base.update(data)
    return client.post("/accounts/register/", base)


class TestFR01Registration:
    def test_register_with_email_sends_verification_and_never_echoes_password(self, client):
        r = register(client, email="Brenda@Example.org")
        assert r.status_code == 302
        user = User.objects.get(email="brenda@example.org")
        assert user.check_password(PASSWORD) and user.email_verified_at is None
        assert len(mail.outbox) == 1
        page = client.get("/accounts/profile/")
        assert PASSWORD.encode() not in page.content

    def test_duplicate_normalized_email_rejected(self, client):
        make_user(email="dup@example.org")
        r = register(client, email="  DUP@example.ORG ")
        assert r.status_code == 422
        assert b"already uses this email" in r.content
        assert User.objects.filter(email__iexact="dup@example.org").count() == 1

    def test_duplicate_normalized_phone_rejected(self, client):
        make_user(phone="+256772123456")
        r = register(client, phone="0772 123 456")
        assert r.status_code == 422
        assert User.objects.filter(phone_e164="+256772123456").count() == 1

    def test_database_rejects_case_variant_duplicate(self):
        from django.db import IntegrityError, transaction

        User.objects.create_user(full_name="A", email="case@example.org", password=PASSWORD)
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(full_name="B", email="CASE@example.org", password="x")

    def test_contact_required(self, client):
        r = register(client)
        assert r.status_code == 422 and b"email address or a phone" in r.content

    def test_email_verification_link(self, client):
        register(client, email="verify@example.org")
        link = re.search(r"/accounts/verify/email/[^/\s]+/", mail.outbox[0].body).group(0)
        client.get(link)
        assert User.objects.get(email="verify@example.org").email_verified_at is not None

    def test_verification_token_is_single_use_and_stored_as_digest(self, client):
        from apps.accounts.models import ContactVerification

        register(client, email="once@example.org")
        token = re.search(r"/accounts/verify/email/([^/\s]+)/", mail.outbox[0].body).group(1)
        assert not ContactVerification.objects.filter(token_digest=token).exists()
        assert services.verify_email_token(token) is not None
        assert services.verify_email_token(token) is None

    def test_phone_otp_verification(self, capsys):
        user = make_user(phone="+256700000001", verified=False)
        services.start_verification(user, "phone")
        code = re.search(r": (\d{6})", capsys.readouterr().out).group(1)
        assert services.verify_phone_code(user, "000000") is False or code == "000000"
        assert services.verify_phone_code(user, code) is True
        user.refresh_from_db()
        assert user.phone_verified_at is not None

    def test_recovery_only_to_verified_email(self, client):
        make_user(email="unverified@example.org", verified=False)
        make_user(email="verified@example.org")
        client.post("/accounts/password/reset/", {"email": "unverified@example.org"})
        assert len(mail.outbox) == 0
        client.post("/accounts/password/reset/", {"email": "verified@example.org"})
        assert len(mail.outbox) == 1 and "verified@example.org" in mail.outbox[0].to

    def test_login_rate_limited(self, client, settings):
        settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login": (3, 300)}
        make_user(email="rl@example.org")
        codes = [client.post("/accounts/login/", {"contact": "rl@example.org", "password": "wrong"}).status_code for _ in range(4)]
        assert codes[:3] == [401, 401, 401] and codes[3] == 429

    def test_login_by_phone(self, client):
        make_user(phone="+256781111111")
        r = client.post("/accounts/login/", {"contact": "0781 111111", "password": PASSWORD})
        assert r.status_code == 302


class TestFR02Roles:
    def test_public_registration_cannot_grant_admin(self, client):
        register(client, email="sneaky@example.org", role="admin", roles="admin", is_admin="1")
        user = User.objects.get(email="sneaky@example.org")
        assert user.role_set() == {"participant"}

    def test_role_switch_preserves_identity(self, client, admin, partner):
        user = make_facilitator(admin, partner)
        UserRole.objects.get_or_create(user=user, role="participant")
        client.force_login(user)
        client.post("/accounts/role/", {"role": "participant"})
        assert client.session["active_role"] == "participant"
        assert str(client.session["_auth_user_id"]) == str(user.pk)
        client.post("/accounts/role/", {"role": "admin"})  # not held: ignored
        assert client.session["active_role"] == "participant"

    def test_only_admin_grants_admin(self, admin):
        user = make_user()
        with pytest.raises(Forbidden):
            services.grant_admin(user, user)
        services.grant_admin(admin, user)
        assert User.objects.get(pk=user.pk).has_role("admin")


class TestFR03FacilitatorApproval:
    @pytest.mark.parametrize("status", ["pending", "suspended", "rejected"])
    def test_unapproved_facilitator_cannot_create_or_manage(self, status, admin, partner, geo, version, cohort, client_for):
        fac = make_facilitator(admin, partner, status=status)
        with pytest.raises(Forbidden):
            cs.create_cohort(fac, partner=partner, district=geo["kampala"], curriculum_version=version, name="X",
                             start_date=date.today(), end_date=None, default_meeting_type="physical")
        r = client_for(fac).get(f"/facilitate/{cohort.pk}/")
        assert r.status_code == 403

    def test_suspending_lead_removes_access(self, admin, lead, cohort, client_for):
        assert client_for(lead).get(f"/facilitate/{cohort.pk}/").status_code == 200
        services.set_facilitator_status(admin, lead.facilitator_profile, FacilitatorProfile.SUSPENDED)
        assert client_for(User.objects.get(pk=lead.pk)).get(f"/facilitate/{cohort.pk}/").status_code == 403

    def test_apply_creates_pending_profile(self, client_for):
        user = make_user()
        client_for(user).post("/accounts/facilitator/apply/", {"note": "Grace Church"})
        assert FacilitatorProfile.objects.get(user=user).approval_status == "pending"

    def test_approval_records_approver(self, admin):
        user = make_user()
        profile = services.apply_as_facilitator(user)
        services.set_facilitator_status(admin, profile, "approved")
        profile.refresh_from_db()
        assert profile.approved_by == admin and profile.approved_at is not None


class TestFR04Profile:
    def test_name_and_contact_suffice(self, client):
        r = register(client, email="min@example.org", date_of_birth="", gender="", district="")
        assert r.status_code == 302
        prof = User.objects.get(email="min@example.org").participant_profile
        assert prof.date_of_birth is None and prof.gender is None and prof.district is None

    def test_optional_demographics_saved(self, client, geo):
        register(client, email="demo@example.org", date_of_birth=str(date.today() - timedelta(days=7000)), gender="female", district=str(geo["kampala"].pk))
        prof = User.objects.get(email="demo@example.org").participant_profile
        assert prof.gender == "female" and prof.district == geo["kampala"]

    def test_weak_password_rejected_with_field_error(self):
        with pytest.raises(ValidationFailed) as exc:
            services.register_participant(full_name="A", email="weak@example.org", phone=None, password="123")
        assert "password" in exc.value.fields
