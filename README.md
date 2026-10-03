# Homework Hatch

An AI study platform built on each student's own Canvas data. A small browser extension
syncs a student's classes, deadlines, grades, pages and files. The Flask app turns them
into a dashboard, flashcards, practice and live quizzes, an AI tutor that
cites the student's own course materials, class chat, and Buddy Coins.

It works at any school on Canvas, including schools that disable student API tokens. The
extension reads Canvas the way the Canvas website does, under the student's normal
login, so no school password or API key is ever involved.

## Features

| Area | What it does |
|---|---|
| **Canvas sync** | Browser extension (`extension/`) plus ingest API (`/v1/*`). It syncs hourly and uploads only new or changed files. Supports any Canvas host. |
| **Home** | Today view: a week strip of what's due, an agenda grouped by day with countdowns, what's missing (per Canvas), grade bars by class (lecture and discussion sections merged), recent announcements. |
| **Classes** | Assignments with rubrics and teacher feedback, modules, pages (sanitized), files with in-app preview, announcements. |
| **Grades** | Canvas-accurate calculator (weighted groups, optimal drop lowest/highest, extra credit), what-if scores, and "what do I need on X". |
| **Calendar** | Month view, a private iCal feed for any calendar app, and a Google Calendar toggle that adds every upcoming due date and keeps it updated (through Composio). |
| **AI tutor** | Streaming chat per class or across all classes, grounded in synced materials with `[S1]` citations. Attach files (Canvas, uploads, Google Drive) to a chat and it answers from them. Built to teach rather than do the work. |
| **Study sets** | AI-generated flashcards and practice quizzes from any files and pages you tick in a class (plus your own uploads), a topic, or pasted notes. Flip through a deck card by card (arrows, x / n), and edit cards by hand. |
| **My files** | Upload notes and readings from your computer or pick them from Google Drive (through Composio); file them under a class and study from them like Canvas files. |
| **Live quiz** | Kahoot-style: the host shows questions, classmates join with a code (no account needed), and faster correct answers score more. |
| **Summaries** | AI study summaries of any synced file or page. Scanned PDFs (no text layer) can be read by Claude so the tutor and generators can use them too. |
| **Class chat** | A room per Canvas course. Classmates confirm each other through the class rosters their extensions sync, so a made-up account can't get into a real class's room. Profanity masking, slur and threat blocking, reports, and auto-hide after 3 reports. |
| **Buddy Coins** | Original rules: assignment 10, quiz 20, test 30, plus a grade bonus; late work earns half. Also pays for studying and live-quiz wins. Achievements and leaderboards (opt-in). |
| **Probability Lab** | 18+, off by default (`FEATURE_SIMULATIONS=1`). Dice odds with the exact probability and expected value shown. Virtual coins only, as in the original terms of service. |
| **Citations** | MLA 9, APA 7 and Chicago 17 for websites, books and articles. |
| **Plans** | Free / Normal $10 / Premium $20 / Pro $25, differing in monthly AI actions. Stripe Checkout, customer portal, signature-verified webhooks. |
| **Admin** | Stats, user management, approvals, comped plans, coin adjustments, password resets, chat moderation, activity log. |
| **Privacy** | Export all your data as JSON, and delete your account plus files permanently. Terms and privacy pages. |

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # Python 3.13+
cp .env.example .env            # optional: add ANTHROPIC_API_KEY to turn on AI features
export FLASK_APP=wsgi.py
.venv/bin/flask db upgrade      # creates instance/homeworkhatch.db (SQLite)
.venv/bin/flask seed-demo       # optional: demo / demo12345 with realistic sample classes
.venv/bin/flask run --debug
```

Locally, the first account you register becomes the admin. In production nobody becomes an
admin automatically: set `ADMIN_EMAIL`, or run `flask create-admin`.

### Connect a real Canvas

1. Sign in, then open **Connect Canvas**. Download the extension zip and load it unpacked
   at `chrome://extensions` (Developer mode → Load unpacked).
2. Open your school's Canvas, click the extension icon, then **Connect**.
3. On the site, create a **server token**. In the extension's Settings, paste the server
   address and the token, then click **Sync now**.

## Deploy: GitHub → Render + Supabase

The code lives in git, and every push to `main` runs CI (`.github/workflows/ci.yml`: tests on
SQLite and Postgres 17, a migration check, extension tests). Render deploys automatically
**only after CI passes** (`autoDeployTrigger: checksPass`). Supabase provides Postgres and
file storage. No secrets live in the repo: they go in Render's dashboard, and `.env` is
git-ignored.

**1. Supabase** (supabase.com → New project)
- **Database:** click **Connect** and copy the **Session pooler** URI
  (`postgresql://postgres.<ref>:<password>@aws-<n>-<region>.pooler.supabase.com:5432/postgres`).
  Use the session pooler because Render only speaks IPv4, while the direct
  `db.<ref>.supabase.co` host is IPv6-only. The app adds `sslmode=verify-full` itself and
  checks the server certificate against Supabase's root CA (bundled in `app/certs`).
- **Storage:** create a **private** bucket named `canvas-files`. Then go to Storage →
  Settings → **S3 connection**, enable it, and create an access key. Note the region shown
  there.
- **Security:** optionally turn on *Enforce SSL* in Database settings. The app turns on Row
  Level Security for every table after each migration, so Supabase's public Data API can't
  read app data. Supabase's Security Advisor should show no "RLS disabled" errors.
- **File size:** Supabase caps objects at 50 MB. Encrypted files are slightly larger, so the
  app's default limit on Supabase is 49 MB. After upgrading, raise the global limit and set `MAX_FILE_MB`.

**2. Encryption key** (before the first deploy). Data is encrypted with a key that never goes
in the repo; see [SECURITY.md](SECURITY.md).
- On a trusted machine, run `flask encryption new-key k1` and save the line in a password
  manager right away. **Losing it means losing the data.**
- Paste it as `ENCRYPTION_KEYS` in Render.
- After the deploy, Admin shows the key's check value. Compare it with your saved copy.
- Once Admin says everything is encrypted, set `ENCRYPTION_STRICT=1`.

**3. Render** (render.com → New → **Blueprint**, pick this repo)
- It reads `render.yaml` and asks once for the secret values: `DATABASE_URL`,
  `SUPABASE_URL`, `SUPABASE_S3_REGION`, `SUPABASE_S3_ACCESS_KEY_ID`,
  `SUPABASE_S3_SECRET_ACCESS_KEY`, `ENCRYPTION_KEYS`, `ANTHROPIC_API_KEY`, and optionally Stripe.
  `SECRET_KEY` is generated for you.
- Migrations run on start (`python boot.py` runs `flask db upgrade` only when a new one exists). On a paid plan you can move them to
  `preDeployCommand`.
- The app refuses to boot in production with an unsafe config: no `SECRET_KEY`, no
  `ENCRYPTION_KEYS`, a SQLite database, or local file storage. It also refuses an encryption
  key that doesn't match the one the data was written with.

**Keeping it awake (free plan).** Render's free plan sleeps after 15 idle minutes, and
waking takes up to a minute. A Supabase scheduled job pings the site every 5 minutes. Run this
once in Supabase's SQL editor:

```sql
create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;
select cron.schedule('homeworkhatch-keepalive', '*/5 * * * *',
  $$select net.http_get(url := 'https://homeworkhatch.onrender.com/health', timeout_milliseconds := 90000)$$);
```

A free workspace gets 750 instance hours a month, enough for one always-on service. On a paid
instance, drop the job: paid instances never sleep.

The ping also keeps calendar links (Brightspace, Blackboard, Moodle, Schoology) fresh for students who
don't open the site: at most every 5 minutes, `/health` refreshes up to 5 of the stalest links (older
than an hour) on a background thread, so their due dates still reach Google Calendar about hourly. Run
`flask feeds refresh-stale` to do it by hand. Render's health checks call `/health` as well, and the
backup pinger's `/health/db` starts the same sweep.

**4. Verify** from Render's shell (paid plans) or any machine with the same env vars:

```bash
flask check-deploy   # config, database + migrations, row level security, storage round-trip, encryption, AI
flask encryption status   # what's encrypted, key check values
```

`/health` returns the deployed commit (Render's health check), and `/health/db` pings the
database.

Notes:
- Free Render services sleep after 15 minutes idle; the extension's hourly sync wakes them.
- Free Supabase projects pause after about a week with no activity; regular syncs keep
  them awake.
- Keep `WEB_CONCURRENCY × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` under the pooler's *Pool Size*
  in Supabase's database settings (defaults: 2 × (3 + 2) = 10).

**Design.** One stylesheet (`app/static/css/app.css`): warm paper and ink, hard outlines with offset shadows, an egg-yolk accent, Bricolage Grotesque for display type, Geist and Geist Mono for text and numbers, light and dark modes, and hand-drawn line icons (`templates/_icons.html`). The sidebar keeps the six core sections (Home, Classes, Calendar, Study, Tutor, Files); Profile, Canvas, Google, Plan, Coins and data live under Account tabs.

## Architecture

```
extension/            Chrome MV3 extension: canvas.js (sync engine), upload.js (protocol), zip.js
app/
  blueprints/         auth, main (dashboard/calendar/planner), courses, api (ingest), study, live,
                      tutor, chat, coins (wallet/lab), tools, billing, settings, admin
  services/           ingest, retrieval (BM25), ai (Claude + quotas), study (generators), grades,
                      planner, coins, citations, moderation, billing, storage
                      (local / Supabase / S3), dbsecurity (Supabase RLS lockdown), ics
  models.py           SQLAlchemy models (Supabase Postgres in production, SQLite locally)
migrations/           Alembic
tests/                pytest suite + tests/js (real extension code against a live server)
```

**Upload protocol.** The extension POSTs a snapshot to `/v1/snapshots`. The server
upserts everything by (Canvas host, Canvas ID) and replies with the file IDs it lacks
at the current version, with a presigned upload URL for each when storage is Supabase or
S3. The extension PUTs each file straight to storage and confirms it with
`POST /v1/files/<id>/uploaded`, so file bytes never pass through the web instance (without
upload URLs, or if a direct upload keeps failing, it PUTs the bytes to `/v1/files/<id>`
instead). Then it POSTs `/complete`. A background thread in the web process reads each new
file's text afterwards for search and the AI tools (`app/services/textjobs.py`). An
unchanged hourly sync sends only JSON.

**AI.** Claude (`claude-opus-5-5` by default, `AI_MODEL` to change) with server-side
refusal fallbacks. Structured JSON output is used for flashcards and quizzes. Every
action is metered against the plan's monthly quota.

## Tests

```bash
.venv/bin/python -m pytest          # ingest, storage (S3 emulator), grades, AI features, live quiz, chat, billing, pages...
(cd extension && npm test)          # extension sync engine + zip writer
TEST_DATABASE_URL=postgresql+psycopg://... .venv/bin/python -m pytest   # same suite on Postgres
# The shipped extension in real Chrome, clicking its popup (needs Chrome for Testing and `npm install` in extension/):
CHROME_PATH=".../Google Chrome for Testing" .venv/bin/python -m pytest tests/test_extension_chrome.py
```

## Not carried over from the old app

- **Google Drive / OneDrive sync.** Replaced by the Canvas extension, which reaches the same
  course files without the OAuth consent problems.
- **School-admin tools** (disciplinary system, teacher dashboard). They conflict with the
  student-first, no-school-admin privacy design.
- **Video generator.** It was a "coming soon" placeholder.
- **Research resources library.** Its curated data didn't carry over. The citation
  generator and AI tutor cover the use case.
