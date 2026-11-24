# Freelancer.in gig scanner

Scans [freelancer.in](https://www.freelancer.in/) for new gigs matching Vinay's filters, scores each one 0-10 from [`scoring.json`](scoring.json), and notifies the review webhook when the fit score is at or above `fit_alert` (currently 7). **Never submits bids.** Direct WhatsApp and ntfy alerts are off by default (`WA_DIRECT=0`).

A single GitHub Actions job stays running for about 5 hours 40 minutes and scans **every 2 minutes** between **08:00 and 23:30 IST** (`Asia/Kolkata`). Outside that window it sleeps 10 minutes so the job stays alive. When the job is about to hit the Actions timeout, it dispatches the next run of itself. An hourly cron (`7 * * * *`) is only a backup restarter, because GitHub scheduled workflows are unreliable (especially on new repos). If a backup trigger starts while another run is already in progress, the new job exits immediately.

This repository is public so Actions minutes stay free. Secrets (WhatsApp token, webhook URL and secret, ntfy topic, phone id, destination number) live in GitHub Actions secrets, not in this repo.

## Filters

A gig is considered only if all of these hold:

- Client is payment-verified (read from the public project page)
- Title/skills/description contain at least one of: ai, llm, chatbot, automation, agent, voice, speech, data, intelligence, workflow, python, n8n, chatgpt, api, whatsapp, email, website, webapp, app
- Fewer than 16 bids
- Hourly budget at least $5/hr (fixed-price minimum is $0)
- Title is not IT / technical / helpdesk / customer support, trading / forex / stock-market / arbitrage, sales / calling / telecalling, video / design-course, or similar non-dev work (wordpress, logo, video edit, data entry, etc.)

## Fit scoring

Scores come from [`scoring.json`](scoring.json) (edit that file to tune; no code change needed):

- Strong and medium positive terms (RAG, LLM, LangChain, agents, chatbots, Python, n8n, etc.), minus negative terms (support, calling, sales, design, video, trading, …)
- A title match weighs more than a skills or description match
- Small bonuses for 0-5 bids, payment-verified + deposit, and a sane budget (hourly ≥ $5, fixed ≥ ~$30)
- Final score is clamped to 0-10

Gigs at or above `fit_alert` (7) are appended to `shortlist.jsonl` (with `fit_score` and `matched` terms) and POSTed in one batch to `ROUTINE_WEBHOOK_URL` with `Authorization: Bearer $ROUTINE_WEBHOOK_SECRET`. If the webhook secret is unset, the scan logs `webhook secret missing` and continues. Every hard-filter pass, including low-score gigs, is logged to `new_matches.csv`.

Record feedback on the box clone with:

```bash
python tune.py like <url> "reason"
python tune.py skip <url> "reason"
```

This appends a line to `feedback.jsonl`. Adjust `scoring.json` weights by hand from that feedback.

## WhatsApp alerts

Direct WhatsApp sends from the scanner are disabled unless `WA_DIRECT=1`. The `--wa-test` path still exists for a manual connectivity check. High-fit gigs are reviewed first, then a message can be sent if the gig is worth sending on.

## Manual run

On GitHub: **Actions → Scan freelancer.in → Run workflow**. Optional input `wa_test` sends one short connectivity test message (only if you turn that input on).

Locally (after setting the same env vars / secrets):

```bash
pip install -r requirements.txt
python pipeline.py --once
python pipeline.py --calibrate
python pipeline.py --wa-test
```

## Secrets

Set these on the repo (Settings → Secrets and variables → Actions):

- `ROUTINE_WEBHOOK_URL`
- `ROUTINE_WEBHOOK_SECRET`
- `WHATSAPP_TOKEN` (only used if `WA_DIRECT=1` or `--wa-test`)
- `WA_PHONE_ID`
- `WA_TO`
- `NTFY_TOPIC`

## State files committed back after each scan

`seen_ids.txt`, `state.json`, `shortlist.jsonl`, `new_matches.csv`, `scanner.log` (trimmed to the last 500 lines). Bid drafts are written on the runner only and are not committed.

Commits use `[skip ci]` so the state push does not trigger another scan.
