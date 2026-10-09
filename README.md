# Called to Thrive

A web platform for running the **Called to Thrive** programme from OneHope Uganda: cohort-based, facilitated learning built on the Uganda workbook.

Participants join a cohort with a code, work through the lessons, take the career quiz and plan a community project with their team. Facilitators schedule sessions, record attendance, follow progress and confirm completion. OneHope administrators approve facilitators, manage partners and see programme-wide reports.

> **Status:** V1 implementation of the proposed baseline in `docs/requirements/`. It is not yet approved by the programme owner and not yet deployed. See [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md) for the decisions taken and what is still open.

## What's inside

| Area | Highlights |
|---|---|
| Accounts | Email or phone sign-up, verified-contact recovery, one account with several roles, facilitators approved by an admin |
| Cohorts | Partners and affiliations, lead and assistant facilitators, join codes that expire or can be withdrawn, an atomic join that can never overfill |
| Sessions | Physical, online (Google Meet link) or hybrid sessions in Kampala time; attendance with a review step; finalizing a session is separate from learning |
| Learning | All 16 workbook lessons from a versioned, immutable content manifest; typed activities; drafts with explicit save states and retry; private-by-default reflections |
| Career quiz | 24 questions rated 1–5, six category totals using the workbook mapping, ties kept, every attempt preserved |
| Projects | Several teams per cohort, workbook roles, the Project Planning Sheet (goals, actions, action items), revision conflicts, the 0–3 evaluation form, lead confirmation |
| Reporting | Facilitator dashboards; admin filters by year, region, district, partner, facilitator and cohort; documented denominators; CSV export with formula-safe cells |
| Integrity | PostgreSQL triggers enforce cross-cohort, cross-version and cross-project rules, even for direct database writes |

## Quick start (local)

Requirements: Python 3.12+ and PostgreSQL 16.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then edit; load it with your shell or direnv
createdb called_to_thrive       # or: docker compose up db

export DJANGO_DEBUG=1 DATABASE_URL=postgres://ctt:secret@localhost:5432/called_to_thrive
python manage.py migrate
python manage.py import_curriculum content/uganda-workbook-v1.json --publish
python manage.py seed_demo      # fictional demo people, cohort and sessions
python manage.py runserver
```

Open http://localhost:8000 and sign in as any demo user. The password is `Thrive-demo-2026`.

| Role | Email |
|---|---|
| Administrator | `admin@example.org` |
| Lead facilitator | `lead@example.org` |
| Assistant facilitator | `assistant@example.org` |
| Pending facilitator | `pending@example.org` |
| Participants | `allan@example.org`, `brenda@example.org`, `collins@example.org`, `diana@example.org`, `eric@example.org`, `faith@example.org` |

The real first administrator is created from the command line. Admin can never be granted through sign-up:

```bash
python manage.py create_admin --email you@onehope.org --name "Your Name"
```

## Tests

```bash
python -m pytest --create-db         # 189 tests incl. concurrency and integrity checks
python -m playwright install chromium   # optional: enables the real-browser tests
```

The suite covers the acceptance scenarios TC01–TC12 from `04_Test_and_Acceptance_Plan`, plus negative authorization tests for every scope. Some tests use threads against PostgreSQL to prove that a one-seat invite admits exactly one person.

## Project layout

```
apps/
  accounts/     identity, roles, verification, facilitator approval
  programme/    regions, districts, partners, affiliations + admin console
  curriculum/   versions, lessons, components, manifest importer, response schemas
  cohorts/      cohorts, invites, enrollments, sessions, attendance, policies
  learning/     responses, progress, overrides, programme completion + participant pages
  assessment/   career quiz definitions, attempts, scoring
  projects/     teams, planning sheet, tasks, evaluations, completion
  reporting/    metrics, breakdowns, CSV export
  core/         JSON API, middleware (safe logging, no-store), integrity triggers
content/        uganda-workbook-v1.json — the reviewed content manifest
docs/           the original documentation pack + implementation notes + traceability
scripts/        backup.sh, restore_check.sh
```

## JSON API

These are the service contracts from the design document, session-authenticated and CSRF-protected:

| Method & path | Purpose |
|---|---|
| `POST /api/cohort-invites/preview` | Safe cohort preview from a join code |
| `POST /api/cohort-invites/join` | Atomic join (201 new, 200 existing) |
| `PUT /api/sessions/{id}/attendance` | Batch attendance with `expected_revision` |
| `GET/PUT /api/enrollments/{id}/responses/{component}` | Read own response / save draft |
| `POST /api/enrollments/{id}/responses/{component}/submit` | Validate, submit and update progress |
| `POST /api/assessment-attempts/{id}/submit` | Score the quiz (replay-safe) |
| `PATCH /api/projects/{id}/tasks/{task}` | Update an action item (409 on stale revision) |
| `POST /api/projects/{id}/confirm-completion` | Lead confirms a project |
| `GET /api/reports/programme` | Admin summary with filters |

Errors always look like `{"error": {"code", "message", "fields"}}`. Status codes: 401 not signed in, 403 forbidden, 404 out of scope, 409 stale revision, 422 invalid input, 429 throttled.

## Changing the curriculum

Published versions are frozen by the database. To change content:

1. Edit a copy of `content/uganda-workbook-v1.json`.
2. Get curriculum-owner sign-off.
3. Run `python manage.py import_curriculum <file> --code UG-WORKBOOK-V2 --publish`.

New cohorts can then choose V2. Existing cohorts stay on V1 with their answers intact.

## Deployment

See `docs/requirements/05_Deployment_and_Operations_Guide.docx`. A `Dockerfile` (gunicorn + WhiteNoise) and a CI workflow are included. In production you need to set `DJANGO_SECRET_KEY`, `DATABASE_URL`, `DJANGO_ALLOWED_HOSTS` and `DJANGO_SECURE_SSL_REDIRECT=1`, plus SMTP settings and a shared cache. The hosting provider has not been chosen yet.

## Content notice

`content/uganda-workbook-v1.json` contains text from OneHope's *Called to Thrive* workbook, including scripture quotations. Keep this repository private unless OneHope approves publishing that content.
