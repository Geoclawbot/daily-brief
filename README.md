# Daily Brief — app

Weather, commute, tropics, fuel and news for Miramar → Jackson Memorial, as an
installable app that updates itself.

No server. GitHub Actions runs the collectors every 20 minutes and commits a
fresh `data/brief.json`; GitHub Pages serves the app. Free, and there is
nothing to keep running.

---

## What you need to provide

**1. A GitHub account** — free. github.com/signup if you don't have one.

**2. Two API keys.** Both free, both take about three minutes.

| Key | Where | What it unlocks | Free tier |
|---|---|---|---|
| `TOMTOM_API_KEY` | developer.tomtom.com → your account → Keys | Live travel time, delay minutes, alternates, corridor incidents | 2,500 requests/day |
| `AIRNOW_API_KEY` | docs.airnowapi.org/account/request | Real EPA air-quality index and a multi-day forecast | 500 requests/hour |

You already have a TomTom account from the connector — the same key works.
The key itself never expired; only the Claude connector kept dropping it.
Used directly over HTTPS it is stable.

Everything else needs no key: Open-Meteo (weather), api.weather.gov (alerts),
NHC (tropics), AAA (fuel), FDOT and FL511 (closures), and the news RSS feeds.
**The app works without either key** — those two sections just degrade
honestly instead of showing numbers.

---

## Setup, about ten minutes

### 1. Create the repository

On github.com click **New repository**. Name it `daily-brief`. **Public** —
GitHub Pages needs a paid plan for private repos, and nothing here is
sensitive; the keys live in Secrets, never in the code.

### 2. Upload these files

Easiest without touching a terminal: on the empty repo page click
**uploading an existing file**, then drag in everything from this folder.
Keep the folder structure — `.github/workflows/refresh.yml` has to stay at
that exact path or the schedule won't run.

If you prefer the command line:

```bash
cd brief-app
git init && git branch -M main
git add . && git commit -m "Daily brief app"
git remote add origin https://github.com/YOUR-USERNAME/daily-brief.git
git push -u origin main
```

### 3. Add the keys as Secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New
repository secret**. Add both:

- Name `TOMTOM_API_KEY`, value your TomTom key
- Name `AIRNOW_API_KEY`, value your AirNow key

Secrets are write-only. Nobody can read them back, including you, and they
never appear in the committed data.

### 4. Let the workflow write

Repo → **Settings** → **Actions** → **General** → scroll to **Workflow
permissions** → select **Read and write permissions** → Save.

Without this the collector runs fine but can't commit the data, and you'll see
a push error in the log.

### 5. Turn on Pages

Repo → **Settings** → **Pages** → under **Source** choose **Deploy from a
branch**, branch `main`, folder `/ (root)` → Save.

After a minute it gives you a URL:
`https://YOUR-USERNAME.github.io/daily-brief/`

### 6. Run it once by hand

Repo → **Actions** → **Refresh brief data** → **Run workflow**. Takes about
40 seconds. When it goes green, open your Pages URL.

### 7. Put it on your home screen

**iPhone:** open the URL in Safari (it must be Safari, not Chrome) → Share →
**Add to Home Screen**. It launches full-screen with no browser chrome and
works offline against the last data it fetched.

**Android:** Chrome → menu → **Install app**.

---

## How it behaves

- **Refreshes every 20 minutes** on GitHub's schedule, plus whenever you open
  the app or tap the refresh icon.
- **The bar under the clock tells you the truth about age** — green under 45
  minutes, amber up to three hours, red beyond that with "treat these numbers
  with suspicion." GitHub's cron can run late; the app never pretends
  otherwise.
- **Works offline.** The shell is cached; data is network-first, so you always
  get the newest available and never a stale file masquerading as live.
- **A dead source shows a gap, not yesterday's number.** Every block carries
  its own `ok` flag, and the footer names whatever failed on that run. This is
  the rule the whole project is built around: a brief with declared holes
  beats one that is quietly wrong.

---

## The sections

| Card | Source | Key? |
|---|---|---|
| Weather + heat curve + humidity | Open-Meteo | no |
| Alerts | api.weather.gov | no |
| Air quality | AirNow, falls back to Open-Meteo | optional |
| Commute, delay, alternates, incidents | TomTom | **yes** |
| Construction & closures | FDOT District 6 + FL511 | no |
| Tropics | NOAA NHC | no |
| Fuel | AAA | no |
| Wildfires, Local, Colombia, World | RSS: Local 10, WSVN, El Tiempo, Kyiv Independent, NPR | no |

---

## Why the heat curve works here and didn't before

The scraped version had to give it up. Three third-party hourly pages
disagreed about what time it was — one said 8:30 AM EDT, another 1:45 PM EDT,
when it was actually 5:52 AM. They render the *fetcher's* clock and label it
Eastern. A curve plotted on those labels is shifted by hours and looks
perfectly plausible, which is the worst kind of wrong.

Open-Meteo returns ISO-8601 timestamps in a timezone you specify. The hours are
real, so the curve is back.

---

## Changing things

**Different departure time** — `scripts/fetch_all.py`, `_depart_at()`.

**Different route** — the `HOME` and `JACKSON` constants at the top.

**More or fewer news sources** — the `FEEDS` dict. RSS only, on purpose:
feeds carry real `pubDate` stamps, which is what stops a June wildfire story
being reported as August news.

**Refresh more often** — the cron in `.github/workflows/refresh.yml`. Every 20
minutes uses roughly 216 of TomTom's 2,500 daily calls, so you have room. Note
that GitHub throttles scheduled runs on busy shared infrastructure; more
frequent cron does not reliably mean more frequent runs.

**Colours and layout** — the `:root` variables at the top of `index.html`.

---

## If something breaks

**The app says "Could not load the brief"** — the workflow hasn't run yet, or
it failed. Check Actions for a red run.

**Everything shows "not sourced"** — open the failed run's log. Each collector
prints its own error line.

**The commute card says `TOMTOM_API_KEY not set`** — the secret is missing or
misnamed. It is case-sensitive.

**The workflow runs but data never updates** — step 4, workflow write
permissions.

**Actions stopped running after 60 days** — GitHub disables scheduled
workflows on repos with no activity. Push any commit to re-enable.

---

## Tests

`scripts/test_parsers.py` runs 29 offline checks against captured payloads —
unit conversions, category boundaries, the >10-minute alternates rule, the
failure shape. CI runs them before every collection, so a silently broken
parser fails the build instead of committing an empty brief.

```bash
pip install -r scripts/requirements.txt
python3 scripts/test_parsers.py
```

The conversion test is there for a reason: an early version of this brief
reported the commute as 30.1 miles by reading 32 km as 32 miles. The test
asserts 32,078 m → 19.9 mi and 2,291 s → 38 min.
