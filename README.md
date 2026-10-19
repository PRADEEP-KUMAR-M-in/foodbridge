# FOODBRIDGE v0.9.1

**Getting surplus food to people who need it, without the phone-tag.**
FOODBRIDGE has a Python (FastAPI) backend and a mobile-first website with a bright, colourful landing page. Bakeries, hotels and shops post the food they can't sell. The app figures out who can safely take it, offers it fairly, follows the pickup, and keeps a tamper-evident record of everything that happened.

> Quick reminder: this is a **coordination tool**. It is **not** a food-safety certification system.

**About this version:** v0.9.1 is the v0.8.1 backend and logic with the new v0.9 look (landing page, scroll story, animated food icons, new colours). Everything from v0.8.1 still works, including the full Tamil dictionary, Tamil match explanations and translated server errors.

---

## What's in here
1. [Quick start](#quick-start)
2. [Demo accounts](#demo-accounts)
3. [The website](#the-website-v09-interface)
4. [What each role can do](#what-each-role-can-do)
5. [Features](#features)
6. [Tamil support](#tamil-support)
7. [Simulator and evidence reports](#simulator-and-evidence-reports)
8. [Speak-to-List](#speak-to-list)
9. [API overview](#api-overview)
10. [Project layout](#project-layout)
11. [Going online](#going-online)
12. [Configuration](#configuration)
13. [Honest limits](#honest-limits-fix-next)

---

## Quick start
Run these in order:
```
pip install -r requirements-dev.txt     # runtime + test tools (use requirements.txt on a server)
python -m pytest -q                     # runs all 79 tests
python reset_demo.py                    # puts the database back to the demo state (fictional data)
uvicorn app.main:app --reload           # starts the server
```
Then open:
- The website: http://127.0.0.1:8000/ (on a phone on the same Wi-Fi, use `http://<your-ip>:8000/`)
- The API docs: http://127.0.0.1:8000/docs
- The simulator page: http://127.0.0.1:8000/simulator.html

What do the 79 tests check? Quite a lot: the safe window, matching, two organisations claiming at the same moment, tamper detection, login, roles and ownership, the Speak-to-List parser, tracking, the passport, impact numbers, the planner, the public stats endpoint, and a check that every English interface string has Tamil text.

**Want to watch a rescue happen live?** Run `python reset_demo.py`, start the server, log in as `bakery` or `admin` and open the **Live** tab. Then run `python demo_story.py` in a second terminal (set `FB_URL` if you're using a hosted copy).

Upgrading from an older version? Run `python reset_demo.py` again. The database layout has changed over time, so this resets the demo data.

## Demo accounts
These are all FICTIONAL and get created by `python reset_demo.py`.

| Role | Username | Password |
|---|---|---|
| Admin | admin | demo-admin-123 |
| Donor (bakery) | bakery | demo-pass-123 |
| Recipients | shelter-a, shelter-b, kitchen-c | demo-pass-123 |

New organisations press **Get started** on the home page and sign up. An admin then has to approve them (Approvals tab) before they can publish or claim anything; until then they can only look around. Before any real use, change every demo password and set `FOODBRIDGE_SECRET`.

---

## The website (v0.9 interface)
It's one file with **no build step**: `web/index.html` (plus `web/simulator.html`, `web/sw.js`, `web/manifest.webmanifest` and `web/icon.svg`). You can install it on a phone like an app (standalone mode).

### How it looks and feels
- **Bright and playful:** leaf-green header, orange call-to-action buttons, pink, blue and purple accent tiles, and a soft cream, mint and sky background with slowly drifting colour blobs.
- **Hand-drawn food icons** (bread, apple, carrot, rice bowl, milk, banana, tomato, orange, cake) show up everywhere: the logo, a scrolling ribbon under the header, floating background food, the navigation buttons, and next to every listing.
- **Smooth motion:** sections fade and slide in as you scroll, cards tilt under the mouse, and successful actions (claim, handover, registration) throw confetti. All of the animation switches off for people who have "reduce motion" turned on.
- **Mobile first:** under 760px wide the menu folds into a burger button, forms use 16px text so phones don't zoom in, and larger screens get a flexible grid.
- **Light theme only** (the dark theme from v0.8 was dropped in the redesign).
- **Fonts:** Baloo Thambi 2 and Mukta Malar (both include Tamil) load from Google Fonts when you're online, and fall back to your device's Tamil or system font when you're not. If you'd rather not make that third-party request, save the font files into `web/` and swap the Google Fonts `<link>` for `@font-face` rules.
- **Refreshing the look on phones that already installed the app:** bump `CACHE` in `web/sw.js` (it's currently `fb-ui5`).

### The landing page (before you log in)
1. **Hero:** "Turn today's surplus into someone's meal", with floating food icons and a **Get started** button.
2. **Scroll story:** a sticky scene where food flies into a bowl as you scroll, with a live counter ("3 of 9 rescued") and a caption that changes at each step.
3. **How FoodBridge works:** four cards: List, Match, Claim, Pick up.
4. **The food waste problem:** animated counters with figures from the UNEP Food Waste Index Report 2024 (the source is shown on the page).
5. **Rescued on FoodBridge so far:** live totals (units rescued, pickups, donors, recipients) from the public endpoint `GET /api/public/stats`. Only counts are shown, never names or listings.
6. **Join as...:** Donor and Recipient cards, then a final **Get started** banner.

The top bar has Home, Login and Get started, the A+ large-button mode, and the English / தமிழ் toggle.

### After you log in
Every page opens with a title block: a role icon, your organisation's name, a **Verified** or **Pending approval** badge, and an icon strip. On the donor "My listings" page and the recipient "Available food" page, coloured tiles show live impact numbers (listed, collected, completed and so on).

---

## What each role can do
| Role | Tabs |
|---|---|
| **Donor** | New listing (with Speak-to-List), My listings (ranked and excluded recipients with reasons), Live (tracking), Pickups (check the recipient's code or QR), Proof |
| **Recipient** | Available food (live clock, Claim), Rescue plan (route), My claims (accept or refuse at handover, QR code to show the donor), Proof |
| **Admin** | Live, Approvals, Incidents, Fairness, Proof |

---

## Features

### The core (v0.1 to v0.5)
- **Safe-Window Clock** (`app/safe_window.py`): the rules live in a data table, and any food category without a reviewed rule is simply refused.
- **Rules-first matching** (`app/matching.py`): recipients who can't take the food are removed, each with a reason code. The rest are ranked by score, with a fair-share band so nobody gets left out. Every recommendation comes with a plain explanation.
- **Atomic claims** using a partial unique index in the database, so two organisations can never both claim the same listing. Listings expire, and are only republished if the safe window is still open (`app/actions.py`).
- **A hash-chained audit log** and `/audit/verify` (`app/proof_chain.py`). Impact numbers are always recomputed from the recorded events.
- **Login, roles and ownership checks** on every route, plus admin approval of new organisations.

### Offer cascade, pickup codes, incidents and fairness (v0.6)
- **First dibs and the cascade:** a new listing is offered first to the best-ranked recipient for `FOODBRIDGE_OFFER_WINDOW_MIN` minutes (default 2; the simulator showed that longer exclusive windows slow claims down). If they don't claim it, the next best gets a turn, and so on. Once everyone has had a turn, it opens to all. A background timer (every `FOODBRIDGE_EXPIRY_INTERVAL` seconds, default 30) handles this automatically. A claim nobody confirms expires after 15 minutes, the no-show can't claim that listing again, and the listing moves on. Listings whose safe window closes are marked expired and counted as "units not collected".
- **Pickup code and QR:** a claim shows the recipient a QR and a code (`FB-<id>-<12 hex>`, derived from the server secret). The donor types or scans it under Pickups. It works once, only for that donor, and the recipient can only accept the delivery after it's been verified. Refusing never needs a code.
- **Report a problem:** donors and recipients involved in a listing can report a problem. A live listing and its claim freeze straight away, and an admin resolves it (Release or Discard) under Incidents. Reports on food that's already been collected are recorded without changing the listing.
- **Fairness (admin view):** offers, claims, completions, no-shows, collected units and each recipient's share, with a warning if one organisation receives more than half.
- **Try the cascade locally** (Windows PowerShell): run `$env:FOODBRIDGE_OFFER_WINDOW_MIN=1` before starting uvicorn (in Command Prompt it's `set FOODBRIDGE_OFFER_WINDOW_MIN=1`), publish a listing, and watch "first dibs" move on after about a minute. The seeded demo listing is open to everyone from the start.

### Built for the demo (v0.7)
- **Rescue Plan (recipients):** picks the best order for up to 7 eligible stops (your claims plus open listings), rescuing as many packs as possible within each deadline and your vehicle's capacity. It shows the next stop, a backup in case it falls through, why other listings were left out, and flags anything "only you can rescue". It's an exact search over a few stops with openly stated assumptions (25 km/h, 5 minutes per stop, straight-line distance), not AI. Claim one stop at a time, because a claim lapses after 15 minutes.
- **Live tracking (donors, admin):** each listing shows Listed > Offered > Claimed > Pickup > Handed over, plus countdowns, risk flags (window closing, claim about to lapse, no taker) and, once finished, the recorded reason it worked or didn't. It's built from the audit trail and refreshes every 5 seconds. Recipients see the same bar under My claims.
- **Food traceability passport:** for each listing you get the food record, the donor's own declaration (ingredients or allergens note, optional), the full handling history with event hashes, incidents, refusals, an integrity check, and a QR code that opens the record (login required; only the donor, the claimant and admins can view it). It documents what was declared and what happened. It does not certify that the food is safe.
- **Verified impact (Proof tab):** completion rate, average minutes from listing to first claim and to collection, claims that lapsed, reasons food wasn't collected, refusal reasons, units by food type, and participating organisations. All recomputed from events. There are no meal or emissions figures because unit sizes aren't recorded.
- **Read aloud and large buttons:** Read aloud buttons (Tamil or English, using the phone's own speech voices; if there's no Tamil voice installed, it tells you) and the A+ large-button mode.
- **Offline drafts:** the New listing form saves a draft on the phone. If publishing fails because there's no network, the listing is kept with absolute times and sent when you're back online. The server re-checks the safe window, so a stale listing gets refused, and claims or handovers are never confirmed offline. A banner shows when the last successful update happened.

---

## Tamil support
- All 258 interface strings have Tamil text, along with server error messages, match explanations, storage names, dates and role names. The test `test_tamil_has_a_translation_for_every_english_string` fails if any English string is missing its Tamil.
- Every screen was checked for every role in Tamil with realistic data. A few things stay exactly as typed: organisation names, usernames, pickup codes, and anything users write themselves (refusal reasons, problem notes, declarations).
- **Landing page:** in v0.9 the hero heading and subtitle switch to Tamil, but the rest of the landing page (scroll story, how it works, waste statistics, role cards) is English only for now.
- `/simulator.html` is English only.
- **Important: the Tamil was written by the developer, not a native speaker. Please get a Tamil speaker to review it before judges or real users see it.** The strings live in the `ta` dictionary near the top of `web/index.html`.

---

## Simulator and evidence reports
- **The simulator** (`app/simulator.py`, page `/simulator.html`, linked from the Proof tab, API `POST /api/simulate`) compares today's "broadcast and wait" approach with FOODBRIDGE over simulated weeks. It uses the app's real scoring code, lets you adjust the assumptions, and includes a drop-one-part comparison and a week-by-week win count. It's a MODEL built on stated assumptions, not a measurement, and the page says so.
- **The reports:** `python run_simulation.py` writes `reports/SIMULATION_REPORT.md` and two charts (the numbers are identical every run). `python make_test_report.py` writes `reports/TEST_REPORT.md` from real test, parser-evaluation and simulation runs, and lists what was NOT tested.
- **What the simulation found (default assumptions):** FOODBRIDGE removes wasted trips (about 16 per 100 listings today, versus 0 with filters). But it does NOT deliver a bigger share of the food, and a long first-dibs window makes claims slower and, past about 10 minutes, delivers less. So please don't claim "more food rescued" from this model. Claim fewer wasted trips and a safer, auditable process. That's why the first-dibs default is 2 minutes. The right value depends on how quickly real organisations reply, and only a pilot can measure that.

---

## Speak-to-List
Go to Donor > New listing. Speak (the mic button works in Chrome, Edge and Android Chrome) or type a sentence like "20 packs of bread, packed at 6 pm, collect before 8 pm, room temperature", then press **Fill the form**.
- It's a **rule-based parser, not AI** (`app/speak_to_list.py`), and the screen says so. It handles English plus a limited amount of Tamil.
- Food-safety fields are never guessed. Storage is only filled in if you said it, otherwise the donor has to choose it. Before publishing, the donor must tick a confirmation of food type, storage and packed time.
- The microphone only works on `http://localhost` or an HTTPS link (browsers block it on plain `http://<ip>`). Typing works everywhere.
- `python check_speak_to_list.py` reports field accuracy on 26 developer-written sentences. We used those sentences to build the parser, so the numbers look better than they should. Collect a fresh set from real donors before quoting any accuracy figure.
- **Upgrade path:** an LLM extractor could replace `parse_listing()` behind the same output format and the same human confirmation step.

---

## API overview
Interactive docs are at `/docs`. Everything needs a login token except the routes marked public.

| Area | Routes (all start with `/api`) |
|---|---|
| Public | `GET /public/stats` (anonymous counts for the landing page), `GET /rules`, `POST /register`, `POST /auth/login` |
| Listings | `/donations`, `/feed` (recipients), publish, ranked candidates, passport, incident |
| Claims | `/claims`, claim, verify pickup code, handover (accept / refuse), problem report |
| Admin | `/orgs`, approvals, `/incidents` and resolve, `/fairness`, `/maintenance/expire` |
| Evidence | `/impact`, `/audit/verify`, `POST /simulate` |

---

## Project layout
```
app/          the backend:
                main.py          all the web routes
                actions.py       the main things you can do (list, claim, hand over, expire, report)
                matching.py      who can take the food, and in what order
                safe_window.py   how long food stays safe to collect
                proof_chain.py   the tamper-evident log
                speak_to_list.py Speak-to-List
                tracking.py      live tracking, passport, impact details, route planner
                simulator.py     the what-if simulator
                tables.py        database tables
                security.py      login and tokens
                qr.py            QR code pictures
                database.py      database connection
                demo_data.py     fictional demo accounts and listings
web/          index.html (the whole website), simulator.html, sw.js (offline fallback),
              manifest.webmanifest, icon.svg
tests/        79 tests (core, speak_to_list, tracking, simulator)
reports/      SIMULATION_REPORT.md, TEST_REPORT.md, charts
reset_demo.py        puts the demo data back          demo_story.py   a scripted live demo
run_simulation.py, make_test_report.py, check_speak_to_list.py     the evidence scripts
rules.example.json   EXAMPLE safe-window rules (waiting on expert review)
DEPLOY.md, Dockerfile, render.yaml           hosting
```

### Files that were renamed
If you remember the older names, here's where things went:

| Old | New |
|---|---|
| app/service.py | app/actions.py |
| app/insights.py | app/tracking.py |
| app/audit.py | app/proof_chain.py |
| app/auth.py | app/security.py |
| app/intake.py | app/speak_to_list.py |
| app/models.py | app/tables.py |
| app/db.py | app/database.py |
| app/demo_seed.py | app/demo_data.py |
| seed.py | reset_demo.py |
| eval_intake.py | check_speak_to_list.py |
| tests/test_intake.py | tests/test_speak_to_list.py |
| tests/test_v07.py | tests/test_tracking.py |
| tests/intake_cases.py | tests/speak_to_list_cases.py |

A note on the code: every line in the Python files now has a plain-English comment explaining what it does, so you can read any file top to bottom without needing to decode it. The comments don't change how anything runs.

## Going online
`DEPLOY.md` walks you through free hosting. You need HTTPS so the microphone and QR scanning work. In hosted mode, `FOODBRIDGE_AUTOSEED=1` re-creates the fictional demo data every time the server starts; also set `FOODBRIDGE_ADMIN_PASSWORD` and `FOODBRIDGE_SECRET`.

## Configuration
| Variable | What it does |
|---|---|
| `FOODBRIDGE_SECRET` | a long random string, so sessions and pickup codes stay stable |
| `FOODBRIDGE_ADMIN_PASSWORD` | the admin password in hosted mode |
| `FOODBRIDGE_AUTOSEED` | set to `1` to re-create the demo data on every start |
| `FOODBRIDGE_OFFER_WINDOW_MIN` | minutes of first dibs before the offer moves on (default 2) |
| `FOODBRIDGE_EXPIRY_INTERVAL` | seconds between background expiry checks (default 30) |
| `FOODBRIDGE_RULES` | path to your own reviewed safe-window rules file |
| `FOODBRIDGE_ALLOW_EXAMPLE_RULES` | allow the example rules (demo only, never for real food) |
| `FOODBRIDGE_DEMO_PASSWORD` | the password used for the seeded demo accounts |
| `FB_URL` | the server address `demo_story.py` talks to |

---

## Honest limits (fix next)
We'd rather tell you what doesn't work yet than have you find out the hard way.
- **The simulator runs on assumptions** (reply times, no-show and misfit rates), so its results are only as good as those assumptions. The sensitivity tables show how much they matter.
- **`rules.example.json` holds EXAMPLE values** that still need expert review. Never use them for real food.
- **Login is hackathon-grade:** the session token sits in the browser's localStorage, the login throttle lives in memory, there's no password reset, and there's no HTTPS built in (deploy behind HTTPS).
- **Approval is manual:** in a pilot, do real identity and document checks before approving anyone.
- **SQLite is for development.** On Postgres, add an advisory lock around `append_event` (the Postgres path hasn't been tested here).
- **QR scanning** uses the browser's BarcodeDetector (Chrome on Android; needs HTTPS or localhost) and hasn't been tried on a real phone. Typing the code always works.
- **The background timer runs inside the one web process,** so run a single instance.
- **Fairness figures describe what happened;** they don't guarantee fairness. Real incident handling needs a written procedure and a named safety lead.
- **Free hosting loses data on every restart** (see `DEPLOY.md`). A real pilot needs a persistent database.
- **Not built yet:** a Telegram bot. The route planner uses straight-line distance and covers at most 7 stops. Tamil read-aloud depends on the device having a Tamil voice, and Tamil speech coverage is small and unvalidated.
- **Testing gaps:** the website was smoke-tested in a simulated browser, not on real phones, and the v0.9 animations (scroll story, confetti, background food) haven't been performance-tested on low-end phones.
- **Double-check the numbers:** the landing page's waste statistics are quoted from the UNEP Food Waste Index Report 2024. Re-check them against the source before presenting.
