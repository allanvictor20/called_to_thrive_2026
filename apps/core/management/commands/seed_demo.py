"""Synthetic demo data for development and staging (test plan: fictional names, no real reflections).

Never run against production.
"""
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import FacilitatorProfile, User, UserRole
from apps.cohorts import services as cs
from apps.cohorts.models import Cohort
from apps.curriculum.importer import import_manifest
from apps.curriculum.models import CurriculumVersion, Lesson, LessonComponent
from apps.learning import services as ls
from apps.programme.models import District, Partner, PartnerFacilitator, Region

PASSWORD = "Thrive-demo-2026"
GEOGRAPHY = {
    "Central": ["Kampala", "Wakiso", "Mukono"],
    "Eastern": ["Jinja", "Mbale"],
    "Northern": ["Gulu", "Lira"],
    "Western": ["Mbarara", "Fort Portal"],
}


def _user(name, email, *roles):
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(full_name=name, email=email, password=PASSWORD)
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])
    for r in roles:
        UserRole.objects.get_or_create(user=user, role=r)
    return user


class Command(BaseCommand):
    help = "Load synthetic demo data (fictional people) for development."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even when DEBUG is off (staging only).")

    @transaction.atomic
    def handle(self, force, **options):
        if not settings.DEBUG and not force:
            raise CommandError("Refusing to seed demo data with DEBUG off. Use --force on staging only.")
        version = CurriculumVersion.objects.filter(code="UG-WORKBOOK-V1").first()
        if version is None:
            version = import_manifest(Path(settings.BASE_DIR) / "content" / "uganda-workbook-v1.json", publish=True)
        districts = {}
        for region_name, names in GEOGRAPHY.items():
            region, _ = Region.objects.get_or_create(name=region_name)
            for n in names:
                districts[n], _ = District.objects.get_or_create(region=region, name=n)

        church, _ = Partner.objects.get_or_create(name="Grace Community Church", defaults={"organisation_type": "church", "district": districts["Kampala"]})
        school, _ = Partner.objects.get_or_create(name="Hillside Secondary School", defaults={"organisation_type": "school", "district": districts["Jinja"]})

        admin = _user("Amina Programme", "admin@example.org", UserRole.ADMIN)
        lead = _user("Daniel Facilitator", "lead@example.org", UserRole.FACILITATOR)
        assistant = _user("Grace Assistant", "assistant@example.org", UserRole.FACILITATOR)
        pending = _user("Peter Pending", "pending@example.org", UserRole.FACILITATOR)
        for u, status in ((lead, "approved"), (assistant, "approved"), (pending, "pending")):
            p, _ = FacilitatorProfile.objects.get_or_create(user=u)
            if p.approval_status != status:
                p.approval_status = status
                if status == "approved":
                    p.approved_by, p.approved_at = admin, timezone.now()
                p.save()
        for u in (lead, assistant):
            PartnerFacilitator.objects.get_or_create(partner=church, facilitator_user=u)
        PartnerFacilitator.objects.get_or_create(partner=school, facilitator_user=lead)

        participants = [
            _user(n, f"{n.split()[0].lower()}@example.org", UserRole.PARTICIPANT)
            for n in ["Allan Learner", "Brenda Nakato", "Collins Okello", "Diana Achieng", "Eric Mugisha", "Faith Namubiru"]
        ]

        cohort = Cohort.objects.filter(name="Kampala Youth — Demo").first()
        if cohort is None:
            cohort = cs.create_cohort(
                lead, partner=church, district=districts["Kampala"], curriculum_version=version, name="Kampala Youth — Demo",
                start_date=timezone.localdate() - timedelta(days=21), end_date=None, default_meeting_type="hybrid",
                default_meet_url="https://meet.google.com/abc-defg-hij",
            )
            cs.assign_facilitator(lead, cohort, assistant, "assistant")
            cs.change_cohort_status(lead, cohort, "active")
            invite, code = cs.create_invite(lead, cohort, expires_at=timezone.now() + timedelta(days=30), max_uses=30)
            self.stdout.write(f"Demo join code: {cs.format_code(code)}")
            for p in participants[:5]:
                cs.join_cohort(p, code)
            lessons = list(Lesson.objects.filter(curriculum_version=version).order_by("lesson_number")[:3])
            for i, lesson in enumerate(lessons):
                s = cs.schedule_session(
                    lead, cohort, title=f"Week {i + 1}: {lesson.title}", starts_at=timezone.now() - timedelta(days=21 - 7 * i),
                    ends_at=timezone.now() - timedelta(days=21 - 7 * i) + timedelta(hours=2), meeting_type="hybrid",
                    meet_url="https://meet.google.com/abc-defg-hij", venue="Church hall", lesson=lesson,
                )
                statuses = {str(e.pk): ("present" if j % 4 else "absent") for j, e in enumerate(cohort.enrollments.all())}
                s = cs.record_attendance(lead, s, statuses, s.revision)
                cs.finalize_session(lead, s, s.revision)
            cs.set_current_lesson(lead, cohort, lessons[-1])
            cs.schedule_session(
                lead, cohort, title="Week 4: Called by Name", starts_at=timezone.now() + timedelta(days=3), ends_at=None,
                meeting_type="online", meet_url="https://meet.google.com/abc-defg-hij", venue=None,
                lesson=Lesson.objects.get(curriculum_version=version, lesson_number=4),
            )
            # Synthetic Lesson 1 progress for the first participant (no real reflections).
            e = cohort.enrollments.get(participant_user=participants[0])
            for c in LessonComponent.objects.filter(lesson=lessons[0]).order_by("display_order"):
                if c.completion_method == "acknowledgment":
                    ls.acknowledge(e, c)
                elif c.completion_method == "submission":
                    payload = {f["key"]: "Sample answer (synthetic demo data)." for f in c.response_schema["fields"]}
                    ls.submit_response(e, c, payload, 0)
            # Revoked and expired invites for the test plan.
            revoked, _ = cs.create_invite(lead, cohort, expires_at=None, max_uses=None)
            cs.revoke_invite(lead, revoked)
        self.stdout.write(self.style.SUCCESS(
            "Demo data ready. Sign in with any of: admin@example.org, lead@example.org, assistant@example.org, "
            f"pending@example.org, allan@example.org … faith@example.org — password {PASSWORD}"
        ))
