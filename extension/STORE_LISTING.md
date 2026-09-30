# Chrome Web Store listing

Copy-paste text for the Chrome Web Store developer dashboard. Build the upload with
`npm run package` (from `extension/`), which writes `dist/homework-hatch-extension.zip`.
Images for the listing are in `dist/store/`: regenerate the icon from `app/static/img/logo.svg`,
and take screenshots from the demo account (never a real student's data).

## Package

Upload `dist/homework-hatch-extension.zip`. For an update, bump `version` in `manifest.json`
first; the store rejects a version it has already seen.

## Store listing tab

**Name:** Homework Hatch (from the manifest)

**Summary** (from the manifest description):
Syncs your own Canvas classes, deadlines, grades and files to Homework Hatch using your normal Canvas login.

**Description:**

    Homework Hatch turns your Canvas into one clean place to plan and study.

    This extension reads your own Canvas account, using the login you already have, and
    syncs it to your Homework Hatch account every hour:
    • classes, assignments, due dates and grades
    • pages, modules, announcements and discussions
    • course files, uploaded directly to your private storage

    It works even at schools that turn off Canvas API tokens, because it uses your normal
    browser session. Your school password never leaves Canvas.

    On Homework Hatch you get a calendar, a two-week study plan, grade what-ifs, flashcards
    and practice quizzes made from your own course material, and an AI tutor that knows
    your classes.

    Setup: add the extension, sign in at https://homeworkhatch.onrender.com and open
    "Connect Canvas" (the extension links itself to your account), then open your Canvas,
    click the extension and press Connect.

**Category:** Education
**Language:** English

**Graphic assets:**
- Store icon: `dist/store/icon-128.png`
- Screenshots (1280x800): `dist/store/screenshot-1-dashboard.png`, `-2-study-plan.png`, `-3-calendar.png`
- Small promo tile (440x280): `dist/store/promo-tile-440x280.png`

## Privacy practices tab

**Single purpose:**
Sync the user's own Canvas course data (classes, assignments, grades, pages, announcements and
files) to their Homework Hatch account so they can plan and study.

**Permission justifications:**
- `storage`: Saves the user's settings: their Canvas address, their Homework Hatch server address and
  sync token, and the status of the last sync.
- `unlimitedStorage`: When the user downloads their course files as one zip, the zip is assembled
  locally and can be larger than the default storage quota.
- `alarms`: Runs the automatic sync once an hour.
- `scripting`: Some schools only serve course files to the Canvas page itself. The extension then
  fetches the file from inside the user's open Canvas tab.
- `downloads`: Saves the zip of course files when the user asks for it.
- `activeTab`: Reads the address of the Canvas tab the user is on when they click Connect.
- `offscreen`: Extension service workers can't create download URLs, so an offscreen document turns
  the finished zip into one.
- **Host permissions** (optional, requested at runtime): Every school has its own Canvas address,
  so the extension asks for access only to the Canvas site the user connects and to the Homework
  Hatch server they sync to. It never asks for access to other sites.

**Remote code:** No, I am not using remote code. All code ships in the package.

**Externally connectable** (if asked): only https://homeworkhatch.onrender.com can message the
extension, to link it to the signed-in account (it hands over a sync token) and to show sync status.
The extension never sends Canvas data or the token back to the page.

**Data usage.** Tick these:
- Personally identifiable information (the user's name and Canvas user ID)
- Authentication information (the Homework Hatch sync token, stored in the extension)
- Website content (the user's course content, grades and files from Canvas)

Then certify all three statements: data isn't sold to third parties, isn't used or transferred for
purposes unrelated to the single purpose, and isn't used to determine creditworthiness or for
lending.

**Privacy policy URL:** https://homeworkhatch.onrender.com/privacy

## Distribution tab

- **Visibility:** Unlisted, so only people with the link can install it. Switch to Public later.
- **Regions:** All regions.

## Notes for the reviewer (Test instructions tab)

    The extension only does something on a Canvas LMS site. To try it: create a free account at
    https://canvas.instructure.com, sign up at https://homeworkhatch.onrender.com and open the
    "Connect Canvas" page (the extension links itself to the account there), then open Canvas,
    click the extension and press Connect.

New sign-ups wait for approval (`REQUIRE_APPROVAL=1`), so approve the reviewer's account under
Admin if one shows up while the extension is in review.
