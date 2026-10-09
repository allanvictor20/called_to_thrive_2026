"""Shared fixtures that mirror the test plan's seed: two partners in different districts,
approved lead and unassigned facilitators, pending and suspended facilitators, an admin,
participants with fictional names, and invites with different limits."""
import itertools
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.accounts.models import FacilitatorProfile, User, UserRole
from apps.cohorts import services as cs
from apps.curriculum.importer import import_manifest
from apps.curriculum.models import CurriculumVersion
from apps.programme.models import District, Partner, PartnerFacilitator, Region

MANIFEST = Path(settings.BASE_DIR) / "content" / "uganda-workbook-v1.json"
PASSWORD = "Correct-horse-42"
_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def version(db):
    v = CurriculumVersion.objects.filter(code="UG-WORKBOOK-V1").first()
    return v or import_manifest(MANIFEST, publish=True)


@pytest.fixture
def version2(db, version):
    v = CurriculumVersion.objects.filter(code="UG-WORKBOOK-V2-TEST").first()
    return v or import_manifest(MANIFEST, publish=True, code_override="UG-WORKBOOK-V2-TEST")


@pytest.fixture
def geo(db):
    central, _ = Region.objects.get_or_create(name="Central")
    eastern, _ = Region.objects.get_or_create(name="Eastern")
    kampala, _ = District.objects.get_or_create(region=central, name="Kampala")
    jinja, _ = District.objects.get_or_create(region=eastern, name="Jinja")
    return {"central": central, "eastern": eastern, "kampala": kampala, "jinja": jinja}


@pytest.fixture
def partner(geo):
    return Partner.objects.create(name="Grace Community Church", organisation_type="church", district=geo["kampala"])


@pytest.fixture
def partner2(geo):
    return Partner.objects.create(name="Hillside School", organisation_type="school", district=geo["jinja"])


def make_user(name=None, *roles, verified=True, email=None, phone=None):
    n = next(_seq)
    user = User.objects.create_user(
        full_name=name or f"Person {n}", email=email or (None if phone else f"person{n}@example.org"), phone_e164=phone, password=PASSWORD
    )
    if verified:
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])
    for r in roles:
        UserRole.objects.get_or_create(user=user, role=r)
    user._role_cache = None
    return user


@pytest.fixture
def admin(db):
    return make_user("Amina Admin", UserRole.ADMIN)


def make_facilitator(admin, partner=None, status="approved", name=None):
    user = make_user(name, UserRole.FACILITATOR)
    FacilitatorProfile.objects.create(
        user=user, approval_status=status,
        approved_by=admin if status == "approved" else None,
        approved_at=timezone.now() if status == "approved" else None,
    )
    if partner is not None:
        PartnerFacilitator.objects.create(partner=partner, facilitator_user=user)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def lead(admin, partner):
    return make_facilitator(admin, partner, name="Daniel Lead")


@pytest.fixture
def outsider(admin, partner, partner2):
    """Approved, affiliated with both partners, but not assigned to the cohort."""
    u = make_facilitator(admin, partner, name="Olivia Outsider")
    PartnerFacilitator.objects.create(partner=partner2, facilitator_user=u)
    return u


@pytest.fixture
def cohort(lead, partner, geo, version):
    c = cs.create_cohort(
        lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="Kampala Youth",
        start_date=date.today() - timedelta(days=7), end_date=None, default_meeting_type="physical",
    )
    return cs.change_cohort_status(lead, c, "active")


@pytest.fixture
def invite_code(cohort, lead):
    _invite, code = cs.create_invite(lead, cohort, expires_at=None, max_uses=None)
    return code


@pytest.fixture
def enrol(cohort, invite_code):
    def _enrol(name=None, code=None):
        user = make_user(name, UserRole.PARTICIPANT)
        enrollment, _ = cs.join_cohort(user, code or invite_code)
        return enrollment

    return _enrol


@pytest.fixture
def client_for():
    def _client(user):
        c = Client()
        c.force_login(user)
        return c

    return _client


def component(version, lesson_number, key):
    from apps.curriculum.models import LessonComponent

    return LessonComponent.objects.select_related("lesson").get(
        lesson__curriculum_version=version, lesson__lesson_number=lesson_number, component_key=key
    )


def complete_lesson(enrollment, lesson):
    """Satisfy every required component of a lesson the normal way."""
    from apps.assessment import services as quiz
    from apps.assessment.models import Assessment
    from apps.learning import services as ls

    for c in lesson.components.filter(required=True).order_by("display_order"):
        if c.completion_method == "acknowledgment":
            ls.acknowledge(enrollment, c)
        elif c.completion_method == "submission":
            payload = {}
            for f in c.response_schema["fields"]:
                if f["type"] == "list":
                    payload[f["key"]] = [f"item {i}" for i in range(f["count"])]
                elif f["type"] == "rating":
                    payload[f["key"]] = f["min"]
                elif f["type"] == "choice":
                    payload[f["key"]] = f["options"][0]["value"]
                elif f["type"] == "multi_choice":
                    payload[f["key"]] = [f["options"][0]["value"]]
                else:
                    payload[f["key"]] = "Synthetic answer"
            ls.submit_response(enrollment, c, payload, 0)
        elif c.completion_method == "assessment":
            a = Assessment.objects.get(component=c)
            attempt = quiz.open_attempt(enrollment, a)
            quiz.submit_attempt(attempt, {str(n): 3 for n in range(1, 25)}, attempt.revision)
        elif c.completion_method == "facilitator_confirmation":
            from django.db import transaction

            with transaction.atomic():
                ls.complete_component(enrollment, c)
