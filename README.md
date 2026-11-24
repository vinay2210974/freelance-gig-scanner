# Freelancer.in gig scanner

Scans [freelancer.in](https://www.freelancer.in/) for new gigs matching Vinay's filters and sends alerts (WhatsApp + ntfy) when a gig is worth a look. **Never submits bids.**

A single GitHub Actions job stays running for about 5 hours 40 minutes and scans **every 2 minutes** between **08:00 and 23:30 IST** (`Asia/Kolkata`). Outside that window it sleeps 10 minutes so the job stays alive. When the job is about to hit the Actions timeout, it dispatches the next run of itself. An hourly cron (`7 * * * *`) is only a backup restarter, because GitHub scheduled workflows are unreliable (especially on new repos). If a backup trigger starts while another run is already in progress, the new job exits immediately.

This repository is public so Actions minutes stay free. Secrets (WhatsApp token, ntfy topic, phone id, destination number) live in GitHub Actions secrets, not in this repo.

## Filters

A gig is considered only if all of these hold:

- Client is payment-verified (read from the public project page)
- Title/skills/description contain at least one of: ai, llm, chatbot, automation, agent, voice, speech, data, intelligence, workflow, python, n8n, chatgpt, api, whatsapp, email, website, webapp, app
- Fewer than 16 bids
- Hourly budget at least $5/hr (fixed-price minimum is $0)
- Title is not IT / technical / helpdesk / customer support, trading / forex / stock-market, or similar non-dev work (wordpress, logo, video edit, data entry, etc.)

Fits are scored with keyword rules. A gig at or above the worth threshold (currently 0, so any hard-filter pass alerts) is appended to `shortlist.jsonl` and a notification is sent.

## WhatsApp alerts

Plain-text alerts are sent with the Cloud API. Every send logs the HTTP status and response body (token redacted). If Meta returns error **131047** (the 24-hour session window is closed), the scanner automatically sends the approved `hello` template (`language: en`) instead, and the gig details are not delivered until the window is reopened by a reply to the WhatsApp thread.

## Manual run

On GitHub: **Actions → Scan freelancer.in → Run workflow**. Optional input `wa_test` sends one short connectivity test message.

Locally (after setting the same env vars / secrets):

```bash
pip install -r requirements.txt
python pipeline.py --once
python pipeline.py --wa-test
```

## Secrets

Set these on the repo (Settings → Secrets and variables → Actions):

- `WHATSAPP_TOKEN`
- `WA_PHONE_ID`
- `WA_TO`
- `NTFY_TOPIC`

## State files committed back after each scan

`seen_ids.txt`, `state.json`, `shortlist.jsonl`, `new_matches.csv`, `scanner.log` (trimmed to the last 500 lines). Bid drafts are written on the runner only and are not committed.

Commits use `[skip ci]` so the state push does not trigger another scan.
