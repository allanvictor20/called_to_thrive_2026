"""Test settings: same as production settings plus fast hashing."""
import os

os.environ.setdefault("DJANGO_TESTING", "1")

from .settings import *  # noqa: E402,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
ALLOWED_HOSTS = ["testserver", "localhost"]

STATIC_ROOT.mkdir(exist_ok=True)  # noqa: F405 - silences WhiteNoise's missing-directory warning
