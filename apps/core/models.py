import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """Common columns from the database design: UUID id, created_at, updated_at."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
