"""TC-FR05..FR10 and TC01 (concurrent invite redemption)."""
import threading
from datetime import date, timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.cohorts import services as cs
from apps.cohorts.models import Cohort, CohortFacilitator, CohortInvite, Enrollment
from apps.core.errors import Forbidden, NotFound, ValidationFailed

from .conftest import make_facilitator, make_user


@pytest.mark.django_db
class TestFR05Partners:
    def test_facilitator_can_serve_several_partners(self, admin, partner, partner2, geo, version, outsider):
        c1 = cs.create_cohort(outsider, partner=partner, district=geo["kampala"], curriculum_version=version, name="A",
                              start_date=date.today(), end_date=None, default_meeting_type="physical")
        c2 = cs.create_cohort(outsider, partner=partner2, district=geo["jinja"], curriculum_version=version, name="B",
                              start_date=date.today(), end_date=None, default_meeting_type="physical")
        assert c1.lead == outsider and c2.lead == outsider

    def test_affiliation_checked_on_assignment(self, admin, partner2, geo, version, lead):
        with pytest.raises(ValidationFailed) as exc:
            cs.create_cohort(lead, partner=partner2, district=geo["jinja"], curriculum_version=version, name="X",
                             start_date=date.today(), end_date=None, default_meeting_type="physical")
        assert "facilitator" in exc.value.fields
        assert not Cohort.objects.filter(name="X").exists()


@pytest.mark.django_db
class TestFR06FR07Cohorts:
    def test_creation_assigns_lead_atomically(self, cohort, lead):
        assert CohortFacilitator.objects.get(cohort=cohort).facilitator_user == lead
        assert cohort.status_events.first().to_status == "planned"

    def test_invalid_date_range_rejected(self, lead, partner, geo, version):
        with pytest.raises(ValidationFailed) as exc:
            cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="Bad",
                             start_date=date.today(), end_date=date.today() - timedelta(days=1), default_meeting_type="physical")
        assert "end_date" in exc.value.fields

    def test_one_active_lead_in_database(self, cohort, outsider):
        with pytest.raises(IntegrityError), transaction.atomic():
            CohortFacilitator.objects.create(cohort=cohort, facilitator_user=outsider, assignment_role="lead")

    def test_lead_replacement_keeps_single_lead(self, cohort, lead, outsider):
        cs.assign_facilitator(lead, cohort, outsider, "lead")
        leads = CohortFacilitator.objects.filter(cohort=cohort, assignment_role="lead", ended_at__isnull=True)
        assert list(leads.values_list("facilitator_user", flat=True)) == [outsider.pk]

    def test_cannot_demote_only_lead(self, cohort, lead):
        with pytest.raises(ValidationFailed):
            cs.assign_facilitator(lead, cohort, lead, "assistant")

    def test_unrelated_facilitator_sees_nothing(self, cohort, outsider, client_for, enrol):
        e = enrol()
        c = client_for(outsider)
        for url in [f"/facilitate/{cohort.pk}/", f"/facilitate/{cohort.pk}/participants/{e.pk}/", f"/facilitate/{cohort.pk}/lessons/1/"]:
            assert c.get(url).status_code == 404
        assert cohort.name.encode() not in c.get("/facilitate/").content

    def test_assistant_has_scoped_access_but_not_lead_actions(self, cohort, lead, outsider, client_for):
        cs.assign_facilitator(lead, cohort, outsider, "assistant")
        c = client_for(outsider)
        assert c.get(f"/facilitate/{cohort.pk}/").status_code == 200
        assert c.get(f"/facilitate/{cohort.pk}/invites/").status_code == 403


@pytest.mark.django_db
class TestFR08FR09Join:
    def test_preview_shows_cohort_partner_facilitator_only(self, cohort, invite_code, enrol, client_for):
        e = enrol("Hidden Rostered Person")
        viewer = make_user(None, UserRole.PARTICIPANT)
        r = client_for(viewer).post("/api/cohort-invites/preview", {"code": invite_code}, content_type="application/json")
        data = r.json()
        assert r.status_code == 200
        assert data["cohort_name"] == cohort.name and data["partner"] == cohort.partner.name and data["lead_facilitator"] == "Daniel Lead"
        assert "Hidden Rostered Person" not in r.content.decode() and str(e.pk) not in r.content.decode()
        assert not Enrollment.objects.filter(participant_user=viewer).exists()

    def test_code_is_case_and_dash_insensitive(self, cohort, invite_code):
        user = make_user(None, UserRole.PARTICIPANT)
        messy = "-".join(invite_code[i:i + 4] for i in range(0, 12, 4)).lower()
        enrollment, created = cs.join_cohort(user, messy)
        assert created and enrollment.cohort == cohort

    def test_unverified_contact_cannot_join(self, invite_code):
        user = make_user(None, UserRole.PARTICIPANT, verified=False)
        with pytest.raises(Forbidden):
            cs.join_cohort(user, invite_code)

    def test_expired_revoked_and_full_rejected(self, cohort, lead):
        expired, code_e = cs.create_invite(lead, cohort, expires_at=timezone.now() + timedelta(seconds=1), max_uses=None)
        CohortInvite.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        revoked, code_r = cs.create_invite(lead, cohort, expires_at=None, max_uses=None)
        cs.revoke_invite(lead, revoked)
        _one, code_1 = cs.create_invite(lead, cohort, expires_at=None, max_uses=1)
        cs.join_cohort(make_user(None, UserRole.PARTICIPANT), code_1)
        for code, word in ((code_e, "expired"), (code_r, "withdrawn"), (code_1, "full")):
            with pytest.raises(ValidationFailed) as exc:
                cs.join_cohort(make_user(None, UserRole.PARTICIPANT), code)
            assert word in exc.value.message

    def test_unknown_code_not_found(self, db):
        with pytest.raises(NotFound):
            cs.preview_invite(make_user(), "NOPE-NOPE-NOPE")

    def test_duplicate_join_returns_existing_and_consumes_one_use(self, cohort, lead):
        invite, code = cs.create_invite(lead, cohort, expires_at=None, max_uses=5)
        user = make_user(None, UserRole.PARTICIPANT)
        e1, created1 = cs.join_cohort(user, code)
        e2, created2 = cs.join_cohort(user, code)
        invite.refresh_from_db()
        assert created1 and not created2 and e1.pk == e2.pk and invite.use_count == 1

    def test_new_cohort_creates_separate_enrollment(self, cohort, lead, partner, geo, version, enrol):
        e1 = enrol()
        other = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="Second",
                                 start_date=date.today(), end_date=None, default_meeting_type="physical")
        _i, code = cs.create_invite(lead, other, expires_at=None, max_uses=None)
        e2, created = cs.join_cohort(e1.participant_user, code)
        assert created and e2.pk != e1.pk and Enrollment.objects.filter(participant_user=e1.participant_user).count() == 2

    def test_rejoin_after_withdrawal_resumes_same_enrollment(self, enrol, invite_code):
        e = enrol()
        cs.withdraw_enrollment(e.participant_user, e)
        e2, created = cs.join_cohort(e.participant_user, invite_code)
        assert not created and e2.pk == e.pk and e2.status == "active"

    def test_full_join_flow_via_pages(self, cohort, invite_code, client_for):
        user = make_user(None, UserRole.PARTICIPANT)
        c = client_for(user)
        r = c.post("/join/", {"code": invite_code})
        assert r.status_code == 200 and cohort.name.encode() in r.content
        r = c.post("/join/", {"code": invite_code, "confirm": "1"})
        assert r.status_code == 302 and Enrollment.objects.filter(participant_user=user, cohort=cohort).exists()


@pytest.mark.django_db
class TestFR10CohortStates:
    def test_complete_requires_reviewed_eligibility(self, cohort, lead):
        with pytest.raises(ValidationFailed):
            cs.change_cohort_status(lead, cohort, "completed")
        c = cs.change_cohort_status(lead, cohort, "completed", reviewed=True)
        assert c.status == "completed" and c.completed_at is not None

    def test_cancel_requires_reason_and_never_counts_completed(self, cohort, lead):
        with pytest.raises(ValidationFailed):
            cs.change_cohort_status(lead, cohort, "cancelled")
        c = cs.change_cohort_status(lead, cohort, "cancelled", reason="Venue closed")
        assert c.status == "cancelled" and c.completed_at is None
        with pytest.raises(ValidationFailed):
            cs.change_cohort_status(lead, c, "completed", reviewed=True)

    def test_reopen_is_admin_correction_with_reason(self, cohort, lead, admin):
        cs.change_cohort_status(lead, cohort, "completed", reviewed=True)
        with pytest.raises(Forbidden):
            cs.change_cohort_status(lead, cohort, "active", reason="oops")
        c = cs.change_cohort_status(admin, cohort, "active", reason="Completed by mistake")
        assert c.status == "active" and c.status_events.last().reason == "Completed by mistake"

    def test_status_history_is_immutable(self, cohort):
        from django.db import DatabaseError

        ev = cohort.status_events.first()
        with pytest.raises(DatabaseError), transaction.atomic():
            ev.reason = "rewritten"
            ev.save()


@pytest.mark.django_db(transaction=True)
def test_TC01_concurrent_one_seat_invite(admin, partner, geo, version):
    lead = make_facilitator(admin, partner)
    cohort = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="Race",
                              start_date=date.today(), end_date=None, default_meeting_type="physical")
    invite, code = cs.create_invite(lead, cohort, expires_at=None, max_uses=1)
    users = [make_user(None, UserRole.PARTICIPANT) for _ in range(2)]
    results, barrier = {}, threading.Barrier(2)

    def attempt(u):
        barrier.wait()
        try:
            results[u.pk] = cs.join_cohort(u, code)
        except ValidationFailed as exc:
            results[u.pk] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(u,)) for u in users]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [u for u in users if isinstance(results[u.pk], tuple)]
    losers = [u for u in users if isinstance(results[u.pk], ValidationFailed)]
    assert len(winners) == 1 and len(losers) == 1 and "full" in results[losers[0].pk].message
    # Replay the winning request: same enrollment, use_count stays one.
    again, created = cs.join_cohort(winners[0], code)
    invite.refresh_from_db()
    assert not created and again.pk == results[winners[0].pk][0].pk and invite.use_count == 1
    assert Enrollment.objects.filter(cohort=cohort).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_join_same_user(admin, partner, geo, version):
    lead = make_facilitator(admin, partner)
    cohort = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="Dup",
                              start_date=date.today(), end_date=None, default_meeting_type="physical")
    invite, code = cs.create_invite(lead, cohort, expires_at=None, max_uses=10)
    user = make_user(None, UserRole.PARTICIPANT)
    out, barrier = [], threading.Barrier(4)

    def attempt():
        barrier.wait()
        try:
            out.append(cs.join_cohort(user, code)[0].pk)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    invite.refresh_from_db()
    assert len(set(out)) == 1 and invite.use_count == 1
