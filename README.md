# ANVISA Regulatory Monitor

Monthly GitHub Actions agent that scrapes ANVISA and DOU for supplement
ingredient regulation changes, classifies them with Claude, updates Supabase,
and emails you a summary.

---

## Setup (one-time)

### 1. Create Supabase tables

Open your Brazil project's SQL editor and run `supabase_schema.sql`.

### 2. Add GitHub Secrets

Go to your repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|--------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `SUPABASE_URL` | Your Brazil Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Service role key (bypasses RLS) |
| `RESEND_API_KEY` | Your Resend API key |
| `ALERT_EMAIL` | Where to send alerts (e.g. sharab.ahmadnv@gmail.com) |

### 3. Push to GitHub

```bash
git add .
git commit -m "Add ANVISA monitor"
git push
```

The workflow runs automatically on the 1st of every month at 6am UTC.

---

## Manual Trigger

Go to Actions → ANVISA Regulatory Monitor → Run workflow.

Options:
- **dry_run**: `true` → logs everything but writes nothing to Supabase and still sends email
- **lookback_days**: `35` default, increase to `90` for initial backfill run

**Recommended first run:**
Set `dry_run=true`, `lookback_days=90` to see what gets classified without touching the DB.

---

## File Structure

```
.github/workflows/
  anvisa-monitor.yml          # Cron: 1st of month, 6am UTC

scripts/
  requirements.txt
  anvisa_monitor/
    main.py                   # Orchestrator
    scraper.py                # ANVISA portal scraper
    dou_scraper.py            # DOU search + RSS fallback
    classifier.py             # Claude API classification
    supabase_updater.py       # DB writes
    notifier.py               # Resend email

supabase_schema.sql           # Run once in Supabase SQL editor
```

---

## What Gets Scraped

**ANVISA portal (gov.br/anvisa)**
- News feed — catches new RDC/IN announcements
- Suplementos alimentares page — static legislation links

**DOU (in.gov.br)**
- Search queries: `ANVISA suplemento alimentar`, `lista positiva ingredientes`, etc.
- RSS fallback if search portal is down
- Seção 1 only (normative/legislative acts)

---

## What Claude Classifies

For each publication, Claude extracts:
- Is it relevant to supplement ingredient positive lists?
- Publication type (RDC / IN / notice)
- Change type (addition / removal / dose_modification / reclassification)
- Ingredients affected (name in PT + EN, category, dose limits)
- Which existing regulation it amends
- Urgency (high / medium / low)

---

## Email Alert Logic

- Runs every month regardless of findings (so you know it ran)
- Subject: `🚨 ACTION REQUIRED` if any HIGH urgency items found
- Subject: `📋 Monthly Update` otherwise
- Includes per-publication breakdown with ingredient changes listed

---

## Supabase Tables

| Table | Purpose |
|-------|---------|
| `anvisa_publications` | Classified publications, one row per item |
| `anvisa_ingredient_changes` | Individual ingredient add/remove/modify events |
| `anvisa_scrape_runs` | Run log (status, counts, errors) |

---

## Integration with Brazil RegCheck360

When you build the Brazil app, query `anvisa_ingredient_changes` to:
- Show recent changes on the ingredient check result page
- Flag ingredients with recent modifications
- Power a "what changed this month" feed

---

## Known Limitations

- ANVISA and DOU websites change their HTML structure occasionally.
  If scraping returns 0 results for 2+ consecutive months, check the
  selectors in `scraper.py` and `dou_scraper.py`.
- The DOU search portal has no official API — it's scraped from HTML.
  The RSS fallback handles outages.
- Full PDF text of RDCs/INs is not parsed (only the web page summary).
  For full text analysis, add PDF fetching to `scraper.py`.
