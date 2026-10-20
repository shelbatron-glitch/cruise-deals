# Bridget's cruise tracker

Checks CruiseMapper once a day for budget cruises, posts the results to a phone-friendly page, and emails when a new deal appears. Runs on GitHub's free scheduled jobs, so it doesn't need anyone's computer to be on and doesn't use Claude.

## What's here

| File | What it does |
|---|---|
| `cruise_tracker.py` | The checker. Reads the settings sheet, searches CruiseMapper, writes the results. |
| `tracker_config.json` | Which Google Sheet holds the settings, and the page's address (for the email button). |
| `.github/workflows/check-cruises.yml` | Runs the checker every morning, and adds a "Run workflow" button for a check on demand. |
| `docs/` | The web page Bridget opens. `docs/data/` is filled in by each check. |
| `state.json` | Created by the first check. Remembers which deals were already emailed. |

## Settings sheet

Bridget changes searches, the deal threshold, months ahead, travelers and cruise lines to skip in the Google Sheet named in `tracker_config.json`. The sheet must be shared as **Anyone with the link: Viewer** so the checker can read it; give Bridget **Editor** access by email so she can change it. If the sheet can't be read, or a row doesn't make sense, the checker uses the usual settings and says so at the top of the page.

## One-time setup

1. Put these files in a **public** GitHub repository (free GitHub Pages needs public).
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `EMAIL_SENDER`: the Gmail address that sends alerts
   - `EMAIL_PASSWORD`: that account's Gmail App Password (the same one in `email_settings.txt`)
   - `EMAIL_RECIPIENT`: who gets alerts (comma-separate for more than one)
3. **Settings → Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`. Save. The page address appears at the top after a minute.
4. Put that address in `tracker_config.json` as `page_url`.
5. **Actions → Check cruises → Run workflow** to do the first check now.

## On Bridget's phone

Open the page address, then:
- iPhone (Safari): Share button → **Add to Home Screen**
- Android (Chrome): ⋮ menu → **Add to Home screen**

## If something goes wrong

- **The page says today's check didn't go through.** CruiseMapper may be refusing GitHub's servers. The page keeps showing the last good results. If it keeps happening, run `cruise_tracker.py` on a home computer on a timer and push `docs/data` up instead.
- **A port "wasn't recognized".** Fix its spelling in the sheet to match cruisemapper.com/ports.
- **GitHub emails about a failed run.** Open the run under Actions to see the log.
- Scheduled runs pause after 60 days without any change to the repository. Each check commits its results, so this shouldn't happen while it's working.

## Testing locally

```
pip install -r requirements.txt
python -m playwright install chromium
python cruise_tracker.py --no-email      # real check, no email
python cruise_tracker.py --sample        # made-up data, to look at the page
```
