# Chrome Web Store listing

Copy-paste text for the Chrome Web Store developer dashboard. Build the upload with
`npm run package` (from `extension/`), which writes `dist/homework-hatch-extension.zip`.
Images for the listing are in `dist/store/`: regenerate the icon from `app/static/img/logo.svg`,
and take screenshots from the demo account (never a real student's data).

## Package

Upload `dist/homework-hatch-extension.zip`. For an update, bump `version` in `manifest.json`
first; the store rejects a version it has already seen.

## Store listing tab

**Name:** Homework Hatch (from the manifest). Don't put "Canvas" in the name, icon or promo art; say
"for Canvas LMS" in plain text only.

**Summary** (from the manifest description):
Syncs your own Canvas classes, deadlines, grades and files to Homework Hatch using your normal Canvas login.

**Description:**

    Homework Hatch puts every deadline from your Canvas LMS classes in one place, and helps you
    study from your own course material.

    While you're signed in to Canvas in Chrome, this extension reads your own classes and
    syncs them to your Homework Hatch account about every hour:
    • classes, assignments, due dates, grades and your instructors' feedback
    • pages, modules, announcements, discussion topics and calendar events
    • course files, only for the classes you choose, into your private storage

    It only reads the Canvas site you connect and never sees your school password. Before
    connecting, it shows exactly what it syncs and asks you to agree.

    On Homework Hatch you get a calendar (with optional Google Calendar sync), a week view with
    countdowns, grades, your course files, flashcards and practice quizzes made from the files
    you pick, and an AI tutor that cites your own materials.

    Setup: add the extension, sign in at https://homeworkhatch.onrender.com and open
    "Connect Canvas" (the extension links itself to your account). Then open your Canvas, click
    the extension, read what it syncs and press "Agree and connect". Finally, choose which
    classes' files to keep on the Connect Canvas page.

    Canvas is a trademark of Instructure, Inc. Homework Hatch is independent and not affiliated
    with, endorsed by or sponsored by Instructure or any school.

**Category:** Education
**Language:** English

**Graphic assets:**
- Store icon: `dist/store/icon-128.png`
- Screenshots (1280x800): `dist/store/screenshot-1-dashboard.png`, `-3-calendar.png`. Retake the second one
  (the study plan was removed), for example the Files or flashcard viewer, from the demo account.
- Small promo tile (440x280): `dist/store/promo-tile-440x280.png`

## Privacy practices tab

**Single purpose:**
Sync your Canvas coursework (assignments, due dates, grades, instructor feedback, pages, announcements,
discussion topics, calendar events, and the course files of classes you choose) to your Homework Hatch
account so you can plan and study.

**Permission justifications:**
- `storage`: Saves the user's settings: their Canvas address, their Homework Hatch server address and
  sync token, and the status of the last sync.
- `unlimitedStorage`: When the user downloads their course files as one zip, the zip is assembled
  locally and can be larger than the default storage quota.
- `alarms`: Runs the automatic sync once an hour.
- `scripting`: Checks whether the tab the user is on is a Canvas site when they click Connect. Also,
  some schools only serve course files to the Canvas page itself, so the extension then fetches a file
  the user can already open from inside their open Canvas tab.
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
- Personal communications (instructors' comments on the user's submissions, announcements and discussion
  topics, which can include other people's names)
- Website content (the user's course content, grades and chosen course files from Canvas)

The extension shows this disclosure and an "Agree and connect" button before it syncs anything (the
only thing it does before that is check whether the current tab is a Canvas site). It no
longer collects class rosters (removed in 1.4.1), and Canvas's signed file links never leave the browser.

Then certify all three statements: data isn't sold to third parties, isn't used or transferred for
purposes unrelated to the single purpose, and isn't used to determine creditworthiness or for
lending.

**Privacy policy URL:** https://homeworkhatch.onrender.com/privacy

**Homepage URL:** https://homeworkhatch.onrender.com
**Support URL:** https://homeworkhatch.onrender.com/support

## Distribution tab

- **Visibility:** Unlisted, so only people with the link can install it. Switch to Public later.
- **Regions:** All regions.

## Notes for the reviewer (Test instructions tab)

    The extension only does something on a Canvas LMS site. To try it: create a free account at
    https://canvas.instructure.com, sign up at https://homeworkhatch.onrender.com and open the
    "Connect Canvas" page (the extension links itself to the account there), then open Canvas,
    click the extension and press Connect.

If sign-up approval is on (`REQUIRE_APPROVAL=1`), approve the reviewer's account under Admin if one
shows up while the extension is in review.
