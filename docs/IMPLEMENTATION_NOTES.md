# Implementation notes

What was built against the documentation pack in `docs/requirements/`, where it differs from the pack, and what is still open. Read this before a review with the programme owner or curriculum owner.

## Decisions taken during implementation

| Topic | Decision | Why |
|---|---|---|
| Framework (D01) | **Django 5.2 LTS + PostgreSQL 16**, server-rendered templates, one deployable app | The SDD names Laravel + Blade as a *candidate* and allows "an equivalent framework". Django gives the same modular monolith, migrations, CSRF, session auth and transactions. The build environment could not reach Packagist, so Laravel could not be installed. |
| Lesson count | **16 lessons** imported | The workbook (`Called_to_Thrive.pdf`) contains Lesson 16 *Putting it all Together*. The pack says 15. The schema never hard-codes the number. **Curriculum owner to confirm.** |
| Composite primary keys | Surrogate `id` + `UNIQUE(...)` on the same columns for `user_roles`, `partner_facilitators`, `cohort_facilitators`, `assessment_answers`, `assessment_scores` | Same integrity as the composite PKs in `08_Database_Design.md`. It also avoids a migration-ordering limitation in Django's new composite-key support. |
| Cross-scope invariants (NFR02) | PostgreSQL triggers in `apps/core/migrations/0001_integrity_triggers.py` | Covers all ten §9 invariants plus: published curriculum is immutable; history rows are immutable; scores are immutable; facilitator assignments must be approved and affiliated. |
| Join codes | 12 characters from an unambiguous alphabet, shown once; only a SHA-256 digest is stored | Follows the DB design ("store a digest"). Facilitators rotate codes by creating a new one. |
| Contact verification (D10) | Email link implemented. Phone OTP built behind a backend switch; **development prints the code to the console**; production refuses until an SMS provider is chosen | The decision register says to confirm phone OTP cost before promising both channels. |
| Completion policy | Proposed default from the SRS: all lessons + facilitator-confirmed project + own submitted evaluation. Attendance is *not* a criterion. | Stored in the curriculum version (`completion_policy`). Marked `status: proposed`. |
| Required components (proposal) | Scripture readings use an acknowledgement; written activities and all Life Work need a submission; discussions are facilitator-confirmed and **not** required; fill-in-the-blank activities with no answer key in the source are optional | Pending curriculum-owner approval. To change it, edit the manifest and publish a new version. |
| Visibility (D05) | Private by default. Lesson 9 brainstorm and Lesson 10 pitch are facilitator-visible; Lesson 16 presentation is team-visible | Group-oriented activities. Everything else, including every Life Work, is private. |
| Project milestones | Lesson 11 roles, Lesson 12 planning sheet and Lesson 15 evaluation complete automatically from project records | No double entry: the workbook's planning sheet *is* the project page. |
| Multiple teams (D07) | A participant can be in several teams | The decision belongs to the programme owner. It is easy to restrict later. |
| Retries (NFR06) | Joins, draft saves, submissions, quiz submissions and attendance batches are replay-safe | A retry of a save whose reply was lost returns the saved draft instead of a conflict. |

## Content review items for the curriculum owner

These are also stored in `content/uganda-workbook-v1.json` → `review_notes`:

1. The workbook has 16 lessons; the pack says 15.
2. The Lesson 3 title is printed as "Called to a Image" and was transcribed as printed.
3. The Lesson 1 prompt spells "Mikalya" while the story uses "Mikayla". Kept as printed.
4. Lessons 6, 8 and 11 have fill-in-the-blank activities, and the source includes no answer key.
5. Scripture wording was transcribed verbatim and needs a manual check against the print copy.

## Not done yet (needs people, hardware or policy, not code)

- **NFR04 load target** (p95 ≤ 1 s at 50 users): needs agreed staging hardware. Page weight is measured: a participant lesson page is about 3 KB of gzipped HTML, plus about 6 KB of CSS and JS. The budget is 500 KB.
- **NFR05 restore on staging**: `scripts/backup.sh` and `scripts/restore_check.sh` have been rehearsed on development data. Repeat the rehearsal on staging and record the timing.
- **SMTP and SMS providers**: configure through environment variables (see `.env.example`).
- **Programme policies** listed in `06_Decisions_and_User_Guide`: minors and consent, retention and purge, withdrawn-participant denominators, and the facilitator response-sharing policy.
- **UAT with a pilot cohort**, curriculum sign-off, and the release record (see `04_Test_and_Acceptance_Plan`).

## Requirement coverage

`docs/traceability.csv` extends the original matrix with the implementing module and the automated test for each of the 42 requirements.
