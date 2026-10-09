from django.core.management.base import BaseCommand, CommandError

from apps.curriculum.importer import ManifestError, import_manifest


class Command(BaseCommand):
    help = "Import a reviewed curriculum manifest (JSON). Use --publish only after curriculum-owner sign-off."

    def add_arguments(self, parser):
        parser.add_argument("manifest")
        parser.add_argument("--publish", action="store_true", help="Publish (freeze) the version after import.")
        parser.add_argument("--code", help="Override the version code, e.g. for a second version.")

    def handle(self, manifest, publish, code, **options):
        try:
            version = import_manifest(manifest, publish=publish, code_override=code)
        except ManifestError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Imported {version.code}: {version.lessons.count()} lessons, status {version.status}."
        ))
