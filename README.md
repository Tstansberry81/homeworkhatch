# Homework Hatch

An AI study platform built on each student's own Canvas data. A small browser extension
syncs a student's classes, deadlines, grades, pages and files. The Flask app turns them
into a dashboard, a study plan, flashcards, practice and live quizzes, an AI tutor that
cites the student's own course materials, class chat, and Buddy Coins.

It works at any school on Canvas, including schools that disable student API tokens. The
extension reads Canvas the way the Canvas website does, under the student's normal
login, so no school password or API key is ever involved.

## Features

| Area | What it does |
|---|---|
| **Canvas sync** | Browser extension (`extension/`) plus ingest API (`/v1/*`). It syncs hourly and uploads only new or changed files. Supports any Canvas host. |
| **Dashboard** | What's due, what's missing (per Canvas), grades by class (lecture and discussion sections merged), recent announcements. |
| **Classes** | Assignments with rubrics and teacher feedback, modules, pages (sanitized), files with in-app preview, announcements. |
| **Grades** | Canvas-accurate calculator (weighted groups, optimal drop lowest/highest, extra credit), what-if scores, and "what do I need on X". |
| **Calendar** | Month view, plus a private iCal feed for Google, Apple or Outlook calendars. |
| **Study plan** | Spreads the next two weeks of work across daily study time and flags what won't fit. |
| **AI tutor** | Streaming chat per class or across all classes, grounded in synced materials with `[S1]` citations. Built to teach rather than do the work. |
| **Study sets** | AI-generated flashcards and practice quizzes from a file, page, topic or pasted notes. SM-2 spaced repetition, cram mode, a manual editor. |
| **Live quiz** | Kahoot-style: the host shows questions, classmates join with a code (no account needed), and faster correct answers score more. |
| **Summaries** | AI study summaries of any synced file or page. |
| **Class chat** | A room per Canvas course, shared by enrolled classmates. Profanity masking, slur and threat blocking, reports, and auto-hide after 3 reports. |
| **Buddy Coins** | Original rules: assignment 10, quiz 20, test 30, plus a grade bonus; late work earns half. Also pays for studying and live-quiz wins. Achievements and leaderboards (opt-in). |
| **Arcade** | Math Sprint, Snake, Memory Match. Plays cost coins, and scores are validated server-side. |
| **Probability Lab** | 18+, off by default (`FEATURE_SIMULATIONS=1`). Dice odds with the exact probability and expected value shown. Virtual coins only, as in the original terms of service. |
| **College odds** | College Scorecard search (or manual entry), out-of-state rates, and a transparent reach/target/safety estimate. |
| **Citations** | MLA 9, APA 7 and Chicago 17 for websites, books and articles. |
| **Plans** | Free / Normal $10 / Premium $20 / Pro $25, differing in monthly AI actions. Stripe Checkout, customer portal, signature-verified webhooks. |
| **Admin** | Stats, user management, approvals, comped plans, coin adjustments, password resets, chat moderation, activity log. |
| **Privacy** | Export all your data as JSON, and delete your account plus files permanently. Terms and privacy pages. |

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # optional: add ANTHROPIC_API_KEY to turn on AI features
export FLASK_APP=wsgi.py
.venv/bin/flask db upgrade      # creates instance/homeworkhatch.db (SQLite)
.venv/bin/flask seed-demo       # optional: demo / demo12345 with realistic sample classes
.venv/bin/flask run --debug
```

The first account you register becomes the admin. You can also create one with
`flask create-admin`.

### Connect a real Canvas

1. Sign in, then open **Connect Canvas**. Download the extension zip and load it unpacked
   at `chrome://extensions` (Developer mode → Load unpacked).
2. Open your school's Canvas, click the extension icon, then **Connect**.
3. On the site, create a **server token**. In the extension's Settings, paste the server
   address and the token, then click **Sync now**.

## Deploy (Render)

`render.yaml` is a Blueprint for a web service plus Postgres. Migrations run on every
deploy (`flask db upgrade`). Set these:

- `PUBLIC_URL`
- `ANTHROPIC_API_KEY`
- storage: `STORAGE_BACKEND=s3`, `S3_BUCKET`, `S3_ENDPOINT_URL`, and AWS-style keys.
  Render's disk is wiped on every deploy, and Cloudflare R2 works well here.
- Stripe keys and price IDs, optionally.
- `COLLEGE_SCORECARD_API_KEY`, optionally.

See `.env.example` for every setting.

## Architecture

```
extension/            Chrome MV3 extension: canvas.js (sync engine), upload.js (protocol), zip.js
app/
  blueprints/         auth, main (dashboard/calendar/planner), courses, api (ingest), study, live,
                      tutor, chat, coins (wallet/arcade/lab), college, tools, billing, settings, admin
  services/           ingest, retrieval (BM25), ai (Claude + quotas), study (generators), grades,
                      planner, srs, coins, college, citations, moderation, billing, storage, ics
  models.py           SQLAlchemy models (Postgres in production, SQLite locally)
migrations/           Alembic
tests/                pytest suite + tests/js (real extension code against a live server)
```

**Upload protocol.** The extension POSTs a snapshot to `/v1/snapshots`. The server
upserts everything by (Canvas host, Canvas ID) and replies with the file IDs it lacks
at the current version. The extension then PUTs just those to `/v1/files/<id>` and
POSTs `/complete`. An unchanged hourly sync sends only JSON.

**AI.** Claude (`claude-opus-5-5` by default, `AI_MODEL` to change) with server-side
refusal fallbacks. Structured JSON output is used for flashcards and quizzes. Every
action is metered against the plan's monthly quota.

## Tests

```bash
.venv/bin/python -m pytest          # 52 tests: ingest, grades, AI features, live quiz, chat, billing, pages...
(cd extension && npm test)          # extension sync engine + zip writer
TEST_DATABASE_URL=postgresql+psycopg://... .venv/bin/python -m pytest   # same suite on Postgres
```

## Not carried over from the old app

- **Google Drive / OneDrive sync.** Replaced by the Canvas extension, which reaches the same
  course files without the OAuth consent problems.
- **School-admin tools** (disciplinary system, teacher dashboard). They conflict with the
  student-first, no-school-admin privacy design.
- **Video generator.** It was a "coming soon" placeholder.
- **Research resources library.** Its curated data didn't carry over. The citation
  generator and AI tutor cover the use case.
