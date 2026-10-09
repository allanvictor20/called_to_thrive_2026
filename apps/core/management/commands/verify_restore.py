"""Post-restore verification for TC10: record counts, relationships, published content and guards."""
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction

from apps.cohorts.models import Attendance, Cohort, Enrollment
from apps.curriculum.models import CurriculumVersion, Lesson
from apps.learning.models import ComponentResponse, LessonProgress


class Command(BaseCommand):
    help = "Verify a restored database: counts, orphan checks, published curriculum and integrity triggers."

    def handle(self, **options):
        counts = {
            "curriculum_versions": CurriculumVersion.objects.count(),
            "published_versions": CurriculumVersion.objects.filter(status="published").count(),
            "lessons": Lesson.objects.count(),
            "cohorts": Cohort.objects.count(),
            "enrollments": Enrollment.objects.count(),
            "responses": ComponentResponse.objects.count(),
            "lesson_progress": LessonProgress.objects.count(),
            "attendance": Attendance.objects.count(),
        }
        for k, v in counts.items():
            self.stdout.write(f"{k:22} {v}")
        if counts["published_versions"] == 0:
            raise CommandError("No published curriculum version found.")
        with connection.cursor() as cur:
            cur.execute("""SELECT count(*) FROM attendance a JOIN sessions s ON s.id = a.session_id
                           JOIN enrollments e ON e.id = a.enrollment_id WHERE s.cohort_id <> e.cohort_id""")
            if cur.fetchone()[0]:
                raise CommandError("Cross-cohort attendance rows found.")
            cur.execute("SELECT count(*) FROM pg_trigger WHERE tgname IN ('attendance_scope','component_responses_version','lessons_lock')")
            if cur.fetchone()[0] != 3:
                raise CommandError("Integrity triggers are missing from the restored database.")
        lesson = Lesson.objects.filter(curriculum_version__status="published").first()
        try:
            with transaction.atomic():
                Lesson.objects.filter(pk=lesson.pk).update(title=lesson.title + " (restore check)")
            raise CommandError("Published curriculum was editable after restore.")
        except DatabaseError:
            pass
        self.stdout.write(self.style.SUCCESS("Restore verification passed."))
