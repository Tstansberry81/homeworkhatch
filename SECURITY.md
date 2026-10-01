# Security

Homework Hatch stores students' coursework, so it encrypts that data itself, on top of what its
hosting providers do. This page describes how. It never contains keys.

## What's encrypted

**In transit.** Browsers reach the site over HTTPS only (HSTS). The app's connection to the
database is TLS with the server's certificate verified against Supabase's own root CA
(`app/certs/supabase-root-2021.crt`, valid to April 2031). File storage is reached over HTTPS.

**At rest, by the app (AES-256-GCM), on top of the providers' disk encryption:**

- Course content synced from Canvas: assignment descriptions, page and syllabus bodies,
  announcement text, and calendar event locations.
- Grades and feedback: scores, grades, rubrics and rubric results, and comments on submissions.
- Course files and uploads: every stored object. The extension's uploads go through the app,
  which encrypts them before they reach storage. Their extracted text and search chunks are
  encrypted too.
- What students make: flashcards, quizzes and quiz answers, summaries, tutor conversations
  (titles, messages, sources and attachments), and class chat messages.
- The student's Canvas name, the calendar feed token (looked up by its SHA-256 hash), and
  activity-log details such as IP addresses.

**Not covered by the extra layer** (they still have the providers' disk encryption):

- Account details: email, username, display name and settings.
- Names, titles, dates and flags of classes, assignments, files and study sets, which the app
  needs to sort and query.
- Usage counts, and plan and payment references. Card details never reach us; Stripe holds them.

Passwords are hashed with scrypt. API tokens are stored as SHA-256 hashes.

## How it works

- **Keys.** `ENCRYPTION_KEYS` holds `kid:base64(32 bytes)` entries, with the active key first.
  Each key is split with HKDF-SHA256 into separate subkeys:
  - one for database fields;
  - one for wrapping file keys;
  - one for a key check value.
- **Fields** (`app/encrypted_types.py`). Each value is encrypted with AES-256-GCM and a random
  96-bit nonce.
  - The format version, key id and column name are authenticated, so a value can't be moved
    to another column.
  - Plaintext is padded to 32-byte multiples, so a short value like a grade doesn't show its
    length.
  - Stored as `enc1:<kid>:<base64url>`.
- **Files** (`app/services/crypto.py`, `EncryptedStorage`). Each object gets a random data key,
  wrapped under the file subkey.
  - The body is sealed in 64 KiB AES-256-GCM segments (the STREAM construction).
  - The object's storage path is authenticated, so a file copied into another student's folder
    won't open.
  - Reordering, truncation and appending are detected.
- **Key checks.** At startup the app compares each configured key's check value with the ones
  recorded in the `encryption_key` table, and refuses to start if a key is wrong or missing.
  Data is never written under an unknown key.
- **Migration.** After a deploy, the app re-encrypts in the background, one process at a time:
  - anything stored before encryption existed;
  - anything still under an older key.

  It works row by row with compare-and-swap. Each file is verified before and after it's
  replaced. Progress shows on Admin. Once nothing unencrypted is left, set `ENCRYPTION_STRICT=1`:
  unencrypted values are then refused instead of read.

## What it protects against, and what it doesn't

It protects copies of the data that someone can **read**: a leaked database dump or backup, a
leaked storage bucket, or a SQL injection read. Without the key those copies are ciphertext.

It does **not** protect against:

- **Write access to the database.** Someone who can change rows (for example with a leaked
  database password) can forge a login.
- **A compromise of the running app or the Render account.** The key lives in Render's
  environment, and the app decrypts data to show it to students and to send it to the AI
  provider.
- **What a student's own browser keeps.** The extension keeps the latest sync in the
  browser's storage on the student's device.

Whoever can read Render's environment variables can read the key. Protect those accounts with
two-factor authentication and revoke any credential that has been exposed.

## Operating the keys

- **Back up every key in a password manager.** Losing a key loses everything encrypted with it,
  and backups don't help. `flask encryption status` and Admin show each key's check value, so you
  can confirm your saved copy without revealing the key.
- **Never remove an old key from `ENCRYPTION_KEYS` while anything still uses it.** The app
  refuses to start if you do. Never delete one from the password manager.
- **To rotate:**
  1. Add a new entry (`flask encryption new-key k2`, on a trusted machine) in front of the old one.
  2. Deploy. The background migration moves everything to the new key.
  3. Wait until `status` reports nothing under the old key.
  4. Wait out the database backup retention.
  5. Run `flask encryption retire k1`, then remove the old key.
- **To undo encryption** before deploying code that predates it, run
  `flask encryption decrypt-all --yes`. Alembic refuses to downgrade while encrypted values exist.

## Reporting a problem

Email the address on the site's Support page. Please don't open a public issue for security
reports.
