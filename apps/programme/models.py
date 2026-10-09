from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class Region(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = "regions"
        ordering = ["name"]

    def __str__(self):
        return self.name


class District(TimeStampedModel):
    region = models.ForeignKey(Region, on_delete=models.RESTRICT, related_name="districts")
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "districts"
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["region", "name"], name="districts_region_name_unique")]

    def __str__(self):
        return self.name


class Partner(TimeStampedModel):
    TYPE_CHOICES = [("church", "Church"), ("school", "School"), ("ministry", "Ministry"), ("other", "Other")]
    STATUS_CHOICES = [("active", "Active"), ("archived", "Archived")]

    name = models.CharField(max_length=200)
    organisation_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    district = models.ForeignKey(District, on_delete=models.RESTRICT, null=True, blank=True, related_name="partners")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")

    class Meta:
        db_table = "partners"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(organisation_type__in=["church", "school", "ministry", "other"]), name="partners_type_valid"
            ),
            models.CheckConstraint(condition=Q(status__in=["active", "archived"]), name="partners_status_valid"),
        ]

    def __str__(self):
        return self.name


class PartnerFacilitator(models.Model):
    STATUS_CHOICES = [("active", "Active"), ("inactive", "Inactive")]

    partner = models.ForeignKey(Partner, on_delete=models.RESTRICT, related_name="affiliations")
    facilitator_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.RESTRICT, related_name="affiliations")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "partner_facilitators"
        constraints = [
            models.UniqueConstraint(fields=["partner", "facilitator_user"], name="partner_facilitators_pair_unique"),
            models.CheckConstraint(condition=Q(status__in=["active", "inactive"]), name="partner_facilitators_status_valid")
        ]
