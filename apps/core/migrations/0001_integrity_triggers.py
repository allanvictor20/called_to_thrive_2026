"""NFR02: cross-scope integrity enforced in the database.

Ordinary single-column foreign keys cannot express the invariants listed in
section 9 of the database design, so they are enforced here with triggers.
Every trigger raises SQLSTATE 23514 (check_violation) so that a failed direct
write aborts the statement and leaves no partial state. The service layer
repeats these checks to give friendly validation messages.
"""
from django.db import migrations

FUNCTIONS = r"""
-- Helper: curriculum version of an enrollment's cohort
CREATE OR REPLACE FUNCTION ctt_enrollment_version(p_enrollment uuid) RETURNS uuid AS $$
  SELECT c.curriculum_version_id FROM enrollments e JOIN cohorts c ON c.id = e.cohort_id WHERE e.id = p_enrollment
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION ctt_component_version(p_component uuid) RETURNS uuid AS $$
  SELECT l.curriculum_version_id FROM lesson_components lc JOIN lessons l ON l.id = lc.lesson_id WHERE lc.id = p_component
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION ctt_fail(msg text) RETURNS void AS $$
BEGIN
  RAISE EXCEPTION '%', msg USING ERRCODE = 'check_violation';
END $$ LANGUAGE plpgsql;

-- 1. Attendance: session and enrollment belong to the same cohort
CREATE OR REPLACE FUNCTION ctt_attendance_scope() RETURNS trigger AS $$
BEGIN
  IF (SELECT cohort_id FROM sessions WHERE id = NEW.session_id) IS DISTINCT FROM
     (SELECT cohort_id FROM enrollments WHERE id = NEW.enrollment_id) THEN
    PERFORM ctt_fail('attendance: session and enrollment belong to different cohorts');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER attendance_scope BEFORE INSERT OR UPDATE ON attendance
  FOR EACH ROW EXECUTE FUNCTION ctt_attendance_scope();

-- 2. Component responses use the enrollment cohort's curriculum version
CREATE OR REPLACE FUNCTION ctt_response_version() RETURNS trigger AS $$
BEGIN
  IF ctt_component_version(NEW.component_id) IS DISTINCT FROM ctt_enrollment_version(NEW.enrollment_id) THEN
    PERFORM ctt_fail('component_responses: component is not part of the enrollment curriculum version');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER component_responses_version BEFORE INSERT OR UPDATE ON component_responses
  FOR EACH ROW EXECUTE FUNCTION ctt_response_version();

-- 3. Lesson progress uses the enrollment cohort's curriculum version
CREATE OR REPLACE FUNCTION ctt_progress_version() RETURNS trigger AS $$
BEGIN
  IF (SELECT curriculum_version_id FROM lessons WHERE id = NEW.lesson_id) IS DISTINCT FROM ctt_enrollment_version(NEW.enrollment_id) THEN
    PERFORM ctt_fail('lesson_progress: lesson is not part of the enrollment curriculum version');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER lesson_progress_version BEFORE INSERT OR UPDATE ON lesson_progress
  FOR EACH ROW EXECUTE FUNCTION ctt_progress_version();

-- 4. Assessment attempts use the enrollment cohort's curriculum version
CREATE OR REPLACE FUNCTION ctt_attempt_version() RETURNS trigger AS $$
BEGIN
  IF ctt_component_version((SELECT component_id FROM assessments WHERE id = NEW.assessment_id))
     IS DISTINCT FROM ctt_enrollment_version(NEW.enrollment_id) THEN
    PERFORM ctt_fail('assessment_attempts: assessment is not part of the enrollment curriculum version');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER assessment_attempts_version BEFORE INSERT OR UPDATE ON assessment_attempts
  FOR EACH ROW EXECUTE FUNCTION ctt_attempt_version();

-- 5. Assessment answers: question belongs to the attempt's assessment; value within scale
CREATE OR REPLACE FUNCTION ctt_answer_scope() RETURNS trigger AS $$
DECLARE a record;
BEGIN
  SELECT s.id, s.min_answer, s.max_answer INTO a
    FROM assessment_attempts t JOIN assessments s ON s.id = t.assessment_id WHERE t.id = NEW.attempt_id;
  IF (SELECT assessment_id FROM assessment_questions WHERE id = NEW.question_id) IS DISTINCT FROM a.id THEN
    PERFORM ctt_fail('assessment_answers: question does not belong to the attempt assessment');
  END IF;
  IF NEW.value < a.min_answer OR NEW.value > a.max_answer THEN
    PERFORM ctt_fail('assessment_answers: value outside the assessment scale');
  END IF;
  IF (SELECT status FROM assessment_attempts WHERE id = NEW.attempt_id) = 'submitted' THEN
    PERFORM ctt_fail('assessment_answers: submitted attempts are immutable');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER assessment_answers_scope BEFORE INSERT OR UPDATE ON assessment_answers
  FOR EACH ROW EXECUTE FUNCTION ctt_answer_scope();

-- 6. Assessment scores: category belongs to the attempt's assessment; immutable once written
CREATE OR REPLACE FUNCTION ctt_score_scope() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    PERFORM ctt_fail('assessment_scores: scores are immutable snapshots');
  END IF;
  IF (SELECT assessment_id FROM assessment_categories WHERE id = NEW.category_id) IS DISTINCT FROM
     (SELECT assessment_id FROM assessment_attempts WHERE id = NEW.attempt_id) THEN
    PERFORM ctt_fail('assessment_scores: category does not belong to the attempt assessment');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER assessment_scores_scope BEFORE INSERT OR UPDATE OR DELETE ON assessment_scores
  FOR EACH ROW EXECUTE FUNCTION ctt_score_scope();

-- 7. Assessment questions: category belongs to the same assessment
CREATE OR REPLACE FUNCTION ctt_question_scope() RETURNS trigger AS $$
BEGIN
  IF (SELECT assessment_id FROM assessment_categories WHERE id = NEW.category_id) IS DISTINCT FROM NEW.assessment_id THEN
    PERFORM ctt_fail('assessment_questions: category belongs to another assessment');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER assessment_questions_scope BEFORE INSERT OR UPDATE ON assessment_questions
  FOR EACH ROW EXECUTE FUNCTION ctt_question_scope();

-- 8. Project members belong to the project's cohort
CREATE OR REPLACE FUNCTION ctt_member_scope() RETURNS trigger AS $$
BEGIN
  IF (SELECT cohort_id FROM projects WHERE id = NEW.project_id) IS DISTINCT FROM
     (SELECT cohort_id FROM enrollments WHERE id = NEW.enrollment_id) THEN
    PERFORM ctt_fail('project_members: enrollment belongs to another cohort');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER project_members_scope BEFORE INSERT OR UPDATE ON project_members
  FOR EACH ROW EXECUTE FUNCTION ctt_member_scope();

-- 9. Project actions: goal belongs to the same project
CREATE OR REPLACE FUNCTION ctt_action_scope() RETURNS trigger AS $$
BEGIN
  IF NEW.goal_id IS NOT NULL AND (SELECT project_id FROM project_goals WHERE id = NEW.goal_id) IS DISTINCT FROM NEW.project_id THEN
    PERFORM ctt_fail('project_actions: goal belongs to another project');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER project_actions_scope BEFORE INSERT OR UPDATE ON project_actions
  FOR EACH ROW EXECUTE FUNCTION ctt_action_scope();

-- 10. Project tasks: responsible member belongs to the action's project
CREATE OR REPLACE FUNCTION ctt_task_scope() RETURNS trigger AS $$
BEGIN
  IF NEW.responsible_member_id IS NOT NULL AND
     (SELECT project_id FROM project_members WHERE id = NEW.responsible_member_id) IS DISTINCT FROM
     (SELECT project_id FROM project_actions WHERE id = NEW.action_id) THEN
    PERFORM ctt_fail('project_tasks: responsible member belongs to another project');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER project_tasks_scope BEFORE INSERT OR UPDATE ON project_tasks
  FOR EACH ROW EXECUTE FUNCTION ctt_task_scope();

-- 11. Project evaluations: evaluator is a member of the evaluated project
CREATE OR REPLACE FUNCTION ctt_evaluation_scope() RETURNS trigger AS $$
BEGIN
  IF (SELECT project_id FROM project_members WHERE id = NEW.evaluator_member_id) IS DISTINCT FROM NEW.project_id THEN
    PERFORM ctt_fail('project_evaluations: evaluator is not a member of this project');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER project_evaluations_scope BEFORE INSERT OR UPDATE ON project_evaluations
  FOR EACH ROW EXECUTE FUNCTION ctt_evaluation_scope();

-- 12. Facilitator assignments need an approved facilitator affiliated with the cohort partner
CREATE OR REPLACE FUNCTION ctt_assignment_scope() RETURNS trigger AS $$
BEGIN
  IF NEW.ended_at IS NULL THEN
    IF NOT EXISTS (SELECT 1 FROM facilitator_profiles WHERE user_id = NEW.facilitator_user_id AND approval_status = 'approved') THEN
      PERFORM ctt_fail('cohort_facilitators: facilitator is not approved');
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM partner_facilitators pf JOIN cohorts c ON c.partner_id = pf.partner_id
       WHERE c.id = NEW.cohort_id AND pf.facilitator_user_id = NEW.facilitator_user_id AND pf.status = 'active') THEN
      PERFORM ctt_fail('cohort_facilitators: facilitator is not affiliated with the cohort partner');
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER cohort_facilitators_scope BEFORE INSERT OR UPDATE ON cohort_facilitators
  FOR EACH ROW EXECUTE FUNCTION ctt_assignment_scope();

-- 13. Cohorts bind to a published version; current lesson within that version
CREATE OR REPLACE FUNCTION ctt_cohort_scope() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' OR NEW.curriculum_version_id IS DISTINCT FROM OLD.curriculum_version_id THEN
    IF TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM enrollments WHERE cohort_id = NEW.id) THEN
      PERFORM ctt_fail('cohorts: curriculum version cannot change after participants enrol');
    END IF;
    IF (SELECT status FROM curriculum_versions WHERE id = NEW.curriculum_version_id) <> 'published' THEN
      PERFORM ctt_fail('cohorts: curriculum version must be published');
    END IF;
  END IF;
  IF NEW.current_lesson_id IS NOT NULL AND
     (SELECT curriculum_version_id FROM lessons WHERE id = NEW.current_lesson_id) IS DISTINCT FROM NEW.curriculum_version_id THEN
    PERFORM ctt_fail('cohorts: current lesson belongs to another curriculum version');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER cohorts_scope BEFORE INSERT OR UPDATE ON cohorts
  FOR EACH ROW EXECUTE FUNCTION ctt_cohort_scope();

-- 14. Sessions reference lessons of the cohort's version
CREATE OR REPLACE FUNCTION ctt_session_scope() RETURNS trigger AS $$
BEGIN
  IF NEW.lesson_id IS NOT NULL AND
     (SELECT curriculum_version_id FROM lessons WHERE id = NEW.lesson_id) IS DISTINCT FROM
     (SELECT curriculum_version_id FROM cohorts WHERE id = NEW.cohort_id) THEN
    PERFORM ctt_fail('sessions: lesson belongs to another curriculum version');
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER sessions_scope BEFORE INSERT OR UPDATE ON sessions
  FOR EACH ROW EXECUTE FUNCTION ctt_session_scope();

-- 15. Published curriculum is immutable (FR21)
CREATE OR REPLACE FUNCTION ctt_version_lock() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN PERFORM ctt_fail('curriculum_versions: published versions cannot be deleted'); END IF;
    RETURN OLD;
  END IF;
  IF OLD.status <> 'draft' THEN
    -- Only the status may move forward (published -> retired); content stays frozen.
    IF NEW.code IS DISTINCT FROM OLD.code OR NEW.title IS DISTINCT FROM OLD.title
       OR NEW.source_reference IS DISTINCT FROM OLD.source_reference OR NEW.language_code IS DISTINCT FROM OLD.language_code
       OR NEW.completion_policy IS DISTINCT FROM OLD.completion_policy OR NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NOT (NEW.status = OLD.status OR (OLD.status = 'published' AND NEW.status = 'retired')) THEN
      PERFORM ctt_fail('curriculum_versions: published content is immutable; create a new version');
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER curriculum_versions_lock BEFORE UPDATE OR DELETE ON curriculum_versions
  FOR EACH ROW EXECUTE FUNCTION ctt_version_lock();

CREATE OR REPLACE FUNCTION ctt_lesson_lock() RETURNS trigger AS $$
DECLARE v uuid;
BEGIN
  v := CASE WHEN TG_OP = 'INSERT' THEN NEW.curriculum_version_id ELSE OLD.curriculum_version_id END;
  IF (SELECT status FROM curriculum_versions WHERE id = v) <> 'draft'
     OR (TG_OP = 'UPDATE' AND (SELECT status FROM curriculum_versions WHERE id = NEW.curriculum_version_id) <> 'draft') THEN
    PERFORM ctt_fail('lessons: published curriculum is immutable');
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER lessons_lock BEFORE INSERT OR UPDATE OR DELETE ON lessons
  FOR EACH ROW EXECUTE FUNCTION ctt_lesson_lock();

CREATE OR REPLACE FUNCTION ctt_component_lock() RETURNS trigger AS $$
DECLARE l uuid;
BEGIN
  l := CASE WHEN TG_OP = 'INSERT' THEN NEW.lesson_id ELSE OLD.lesson_id END;
  IF (SELECT v.status FROM lessons x JOIN curriculum_versions v ON v.id = x.curriculum_version_id WHERE x.id = l) <> 'draft'
     OR (TG_OP = 'UPDATE' AND (SELECT v.status FROM lessons x JOIN curriculum_versions v ON v.id = x.curriculum_version_id WHERE x.id = NEW.lesson_id) <> 'draft') THEN
    PERFORM ctt_fail('lesson_components: published curriculum is immutable');
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER lesson_components_lock BEFORE INSERT OR UPDATE OR DELETE ON lesson_components
  FOR EACH ROW EXECUTE FUNCTION ctt_component_lock();

-- Assessment definitions are frozen with the curriculum version
CREATE OR REPLACE FUNCTION ctt_assessment_def_lock() RETURNS trigger AS $$
DECLARE a uuid; st text;
BEGIN
  IF TG_TABLE_NAME = 'assessments' THEN
    SELECT v.status INTO st FROM lesson_components lc JOIN lessons x ON x.id = lc.lesson_id
      JOIN curriculum_versions v ON v.id = x.curriculum_version_id
     WHERE lc.id = CASE WHEN TG_OP = 'DELETE' THEN OLD.component_id ELSE NEW.component_id END;
  ELSE
    a := CASE WHEN TG_OP = 'DELETE' THEN OLD.assessment_id ELSE NEW.assessment_id END;
    SELECT v.status INTO st FROM assessments s JOIN lesson_components lc ON lc.id = s.component_id
      JOIN lessons x ON x.id = lc.lesson_id JOIN curriculum_versions v ON v.id = x.curriculum_version_id WHERE s.id = a;
  END IF;
  IF st <> 'draft' THEN
    PERFORM ctt_fail(TG_TABLE_NAME || ': published assessment definitions are immutable');
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER assessments_lock BEFORE INSERT OR UPDATE OR DELETE ON assessments
  FOR EACH ROW EXECUTE FUNCTION ctt_assessment_def_lock();
CREATE TRIGGER assessment_categories_lock BEFORE INSERT OR UPDATE OR DELETE ON assessment_categories
  FOR EACH ROW EXECUTE FUNCTION ctt_assessment_def_lock();
CREATE TRIGGER assessment_questions_lock BEFORE INSERT OR UPDATE OR DELETE ON assessment_questions
  FOR EACH ROW EXECUTE FUNCTION ctt_assessment_def_lock();

-- 16. Immutable history rows
CREATE OR REPLACE FUNCTION ctt_history_immutable() RETURNS trigger AS $$
BEGIN
  IF TG_TABLE_NAME = 'programme_completion_overrides' AND TG_OP = 'UPDATE'
     AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
     AND NEW.enrollment_id = OLD.enrollment_id AND NEW.authorized_by_id = OLD.authorized_by_id
     AND NEW.reason = OLD.reason AND NEW.created_at = OLD.created_at THEN
    RETURN NEW; -- revocation is the only permitted change
  END IF;
  PERFORM ctt_fail(TG_TABLE_NAME || ': history rows are immutable');
  RETURN NULL;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER cohort_status_events_immutable BEFORE UPDATE OR DELETE ON cohort_status_events
  FOR EACH ROW EXECUTE FUNCTION ctt_history_immutable();
CREATE TRIGGER programme_overrides_immutable BEFORE UPDATE OR DELETE ON programme_completion_overrides
  FOR EACH ROW EXECUTE FUNCTION ctt_history_immutable();

-- 17. Invite capacity can never be exceeded, even by direct writes
--     (also enforced by CHECK use_count <= max_uses)
"""

DROP = r"""
DROP TRIGGER IF EXISTS attendance_scope ON attendance;
DROP TRIGGER IF EXISTS component_responses_version ON component_responses;
DROP TRIGGER IF EXISTS lesson_progress_version ON lesson_progress;
DROP TRIGGER IF EXISTS assessment_attempts_version ON assessment_attempts;
DROP TRIGGER IF EXISTS assessment_answers_scope ON assessment_answers;
DROP TRIGGER IF EXISTS assessment_scores_scope ON assessment_scores;
DROP TRIGGER IF EXISTS assessment_questions_scope ON assessment_questions;
DROP TRIGGER IF EXISTS project_members_scope ON project_members;
DROP TRIGGER IF EXISTS project_actions_scope ON project_actions;
DROP TRIGGER IF EXISTS project_tasks_scope ON project_tasks;
DROP TRIGGER IF EXISTS project_evaluations_scope ON project_evaluations;
DROP TRIGGER IF EXISTS cohort_facilitators_scope ON cohort_facilitators;
DROP TRIGGER IF EXISTS cohorts_scope ON cohorts;
DROP TRIGGER IF EXISTS sessions_scope ON sessions;
DROP TRIGGER IF EXISTS curriculum_versions_lock ON curriculum_versions;
DROP TRIGGER IF EXISTS lessons_lock ON lessons;
DROP TRIGGER IF EXISTS lesson_components_lock ON lesson_components;
DROP TRIGGER IF EXISTS assessments_lock ON assessments;
DROP TRIGGER IF EXISTS assessment_categories_lock ON assessment_categories;
DROP TRIGGER IF EXISTS assessment_questions_lock ON assessment_questions;
DROP TRIGGER IF EXISTS cohort_status_events_immutable ON cohort_status_events;
DROP TRIGGER IF EXISTS programme_overrides_immutable ON programme_completion_overrides;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_initial"),
        ("programme", "0001_initial"),
        ("curriculum", "0001_initial"),
        ("cohorts", "0001_initial"),
        ("learning", "0001_initial"),
        ("assessment", "0001_initial"),
        ("projects", "0001_initial"),
    ]

    operations = [migrations.RunSQL(FUNCTIONS, DROP)]
