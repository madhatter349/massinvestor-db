# Investment Firm Database (Massinvestor public directory)

Scraper + dashboard for the **public** pages of the Massinvestor investment-firm
directory (https://massinvestordatabase.com). Produces a clean SQLite database of
~5,300 investment firms and serves it through a small Flask dashboard.

> **Legal note:** The site's Terms of Use (`/terms.php`) states that copying,
> reproduction, or redistribution of site content is *strictly prohibited*
> without express prior written permission, and that content sharing/copying
> from Demo access is prohibited. This project crawls only **publicly served**
> profile data (names, types, offices, stages, industries, descriptions, public
> team names/titles, funding events, portfolio lists, news links). It does **not**
> scrape login-gated email addresses or full partner profiles. **Use this dataset
> for personal/private research only; do not redistribute.**

## What's scraped (per firm)

- name, investor-type key (VC/PE/A/I/MB/VD/FI/FOF/ED/TT/CVC/SEC/HF)
- website, office locations (+ phone), stages, industries, description
- investment team (public name + title rows)
- recent funding events (date, company, state, amount, stage)
- portfolio companies (name + website)
- recent news (title + URL)

## Structure

```
scraper.py   # crawler -> SQLite (checkpoint/resume via firms table)
app.py       # Flask dashboard
templates/   # dashboard templates
massinvestor.db  # output database (built by scraper.py)
```

## Scrape

```bash
python3 scraper.py --db massinvestor.db --delay 0.45 --workers 4
# resume (skips already-scraped firms):
python3 scraper.py --db massinvestor.db
# small test:
python3 scraper.py --db test.db --limit 5
```

The crawler:

1. Fetches the 8 letter-range index pages
   (`/investmentfirmlist.php?range=abc|def|ghi|jkl|mno|pqr|stu|vwxyz`) → 5,336 unique names.
2. Fetches each firm's detail page from the canonical endpoint
   `publicfirm.php?name=<urlencoded name>` (robust to `&`, `,`, `.`, `(`, `)`, `'`).
3. Stores profiles in SQLite `firms` table; missing firms (HTTP 200 stubs with no
   `id="sectionheader"`) are recorded in `raw` and skipped.

Politeness: 0.45s+ delay per request, 4 workers, retry/backoff on 5xx, browser UA,
shared session cookie. No robots.txt exists (404); rate limiting was not observed
during testing.

## Dashboard

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
DB_PATH=$PWD/massinvestor.db PORT=5000 ./venv/bin/python app.py
# or: gunicorn app:app --bind 0.0.0.0:$PORT
```

Deployed on Railway: dashboard at the service URL.

## Live deployment

- **Dashboard:** https://dashboard-production-58d7.up.railway.app
- **Health:** https://dashboard-production-58d7.up.railway.app/health

The deployed app reads the SQLite DB from a Railway volume mounted at `/app/data`
(`DB_PATH=/app/data/massinvestor.db`). The crawl result (5,304 firms) is uploaded
to that volume; the app seeds an empty DB if the file is missing so deploys never
fail on a fresh build.
