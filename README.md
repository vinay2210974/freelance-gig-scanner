# Freelancer.in gig scanner

Scans [freelancer.in](https://www.freelancer.in/) for new gigs matching Vinay's filters and sends alerts (WhatsApp + ntfy) when a gig is worth a look. **Never submits bids.**

Runs on GitHub Actions every 5 minutes (GitHub's shortest schedule; jobs can start a few minutes late). Scans only between **08:00 and 23:30 IST** (`Asia/Kolkata`). Outside that window the job exits cleanly.

This repository is public so Actions minutes stay free. Secrets (WhatsApp token, ntfy topic, phone id, destination number) live in GitHub Actions secrets, not in this repo.

## Filters

A gig is considered only if all of these hold:

- Client is payment-verified (read from the public project page)
- Title/skills/description contain at least one of: ai, llm, chatbot, automation, agent, voice, speech, data, intelligence, workflow, python, n8n, chatgpt, api, whatsapp, email, website, webapp, app
- Fewer than 16 bids
- Hourly budget at least $5/hr (fixed-price minimum is $0)
- Title is not trading / forex / stock-market (also skips wordpress, logo, video edit, data entry, and similar)

Fits are scored with keyword rules. A gig at or above the worth threshold (currently 0, so any hard-filter pass alerts) is appended to `shortlist.jsonl` and a notification is sent.

## Manual run

On GitHub: **Actions → Scan freelancer.in → Run workflow**.

Locally (after setting the same env vars / secrets):

```bash
pip install -r requirements.txt
python pipeline.py --once
```

## Secrets

Set these on the repo (Settings → Secrets and variables → Actions):

- `WHATSAPP_TOKEN`
- `WA_PHONE_ID`
- `WA_TO`
- `NTFY_TOPIC`

## State files committed back after each run

`seen_ids.txt`, `state.json`, `shortlist.jsonl`, `new_matches.csv`, `scanner.log` (trimmed to the last 500 lines). Bid drafts are written on the runner only and are not committed.

Commits use `[skip ci]` so the state push does not trigger another scan.
