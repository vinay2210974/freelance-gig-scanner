#!/usr/bin/env python3
"""Vinay's freelance gig pipeline (GitHub Actions / local --once).

fetch (Freelancer India public API) -> hard filters (payment verified,
<=15 bids, keywords, budget, skip-title list) -> dedupe -> fit score
0-10 from scoring.json (title matches weigh more than description) ->
gigs at or above fit_alert go to shortlist.jsonl + drafts/<id>.md
(bid draft, not committed). Each scan POSTs qualifying gigs immediately
for human review (max 10, highest fit first), with a 2-minute minimum
gap so back-to-back scans merge. webhook_min_interval_min in scoring.json
is the configurable interval (0 = every scan, still floored at 2 minutes).
Direct WhatsApp / ntfy alerts are off unless WA_DIRECT=1.
Every hard-filter pass is logged to new_matches.csv.
NEVER submits bids. NEVER sends WhatsApp unless WA_DIRECT=1 or --wa-test.

Usage: python pipeline.py --once | --loop | --wa-test | --calibrate
  --once (default): one scan, skip if outside 08:00-23:30 Asia/Kolkata.
  --loop: box loop every SCAN_EVERY_MIN minutes inside the same window.
  --wa-test: send one short WhatsApp connectivity test (manual only).
  --calibrate: score built-in fixtures and shortlist.jsonl, print a table.
"""
import csv, json, os, re, sys, time, datetime, random
from zoneinfo import ZoneInfo
import requests

IST = ZoneInfo("Asia/Kolkata")
BASE = os.path.dirname(os.path.abspath(__file__))
SEEN = os.path.join(BASE, "seen_ids.txt")
STATE = os.path.join(BASE, "state.json")
LOG = os.path.join(BASE, "scanner.log")
CSV_OUT = os.path.join(BASE, "new_matches.csv")
SHORTLIST = os.path.join(BASE, "shortlist.jsonl")
PENDING = os.path.join(BASE, "pending_review.jsonl")
WEBHOOK_BATCH = 10
WEBHOOK_MIN_GAP_MIN = 2  # merge back-to-back scans even when interval is 0
DRAFTS = os.path.join(BASE, "drafts")
PROFILE = os.path.join(BASE, "fit_profile.md")
FEEDBACK = os.path.join(BASE, "feedback.md")
SCORING_PATH = os.path.join(BASE, "scoring.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
API = "https://www.freelancer.in/api/projects/0.1/projects/active/"
SITE = "https://www.freelancer.in/projects/"
QUERIES = ['ai', 'llm', 'chatbot', 'automation', 'agent', 'voice', 'speech', 'data', 'intelligence', 'workflow', 'python', 'n8n', 'chatgpt', 'api', 'whatsapp', 'email', 'website', 'webapp', 'app', "lead generation", "ai calling", "voice agent"]
MAX_BIDS = int(os.environ.get("MAX_BIDS", "16"))
MIN_HOURLY = float(os.environ.get("MIN_HOURLY", "5"))
MIN_FIXED = float(os.environ.get("MIN_FIXED", "0"))
WORTH = float(os.environ.get("WORTH", "0"))
LOOKBACK_FIRST = 3 * 3600
SCAN_TIMEOUT_SEC = int(os.environ.get("SCAN_TIMEOUT_SEC", "420"))

CORE_RE = re.compile(r"\b(ai|llms?|chat ?bots?|automations?|agents?|voice|speech|data|intelligence|workflows?|python|n8n|chat ?gpt|apis?|whatsapp|e-?mails?|websites?|web ?apps?|apps?)\b", re.I)
SKIP_TITLE = re.compile(r"\b(wordpress|woocommerce|elementor|php|laravel|codeigniter|joomla|drupal|magento|wix|squarespace|webflow|flutter|react native|swift|swiftui|kotlin|xamarin|unity|unreal|game dev\w*|c#|\.net|asp\.net|vb\.net|ruby on rails|solidity|smart contracts?|nft|blockchain|logo|tiktok|followers|essay|assignment|homework|thesis|dissertation|trading|trader|forex|crypto trading|stock market|options trading|arbitrage|algorithmic trading|autocad|solidworks|3d model\w*|native (?!app|mobile|ios|android)\w+|native speakers?|translat\w*|transcri\w*|copywrit\w*|ghostwrit\w*|content writ\w*|proofread\w*|voice ?over\w*|tutorial videos?|video editor|video editing|storytellers?|roleplay chats?|business development partners?|sales partners?|data entry|email collection|lead list\w*|document sourcing|data collection|tally|cinematic|kling|animated series)\b", re.I)
RED_FLAG = re.compile(r"telegram|contact (me )?on whatsapp|unpaid|free (trial|test)|test task without pay|commission only|"
    r"pay (a )?(fee|deposit)|equity only", re.I)
OFF_STACK = re.compile(r"\b(flutter|react native|swift|kotlin|unity|php|laravel|wordpress|shopify|\.net|c#)\b", re.I)


def now_ist():
    return datetime.datetime.now(IST)


def topic():
    t = (os.environ.get("NTFY_TOPIC") or "").strip()
    path = os.path.join(BASE, "ntfy_topic.txt")
    if not t and os.path.exists(path):
        t = open(path).read().strip()
    return t


def log(msg):
    line = f"{now_ist():%Y-%m-%d %H:%M:%S} IST {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def trim_log(keep=500):
    if not os.path.exists(LOG):
        return
    lines = open(LOG).read().splitlines()
    if len(lines) > keep:
        with open(LOG, "w") as f:
            f.write("\n".join(lines[-keep:]) + "\n")



def wa_creds():
    tok = (os.environ.get("WHATSAPP_TOKEN") or "").strip()
    phone = (os.environ.get("WA_PHONE_ID") or "").strip()
    to = (os.environ.get("WA_TO") or "").strip()
    return tok, phone, to


def _wa_error_code(text):
    try:
        return (json.loads(text) or {}).get("error", {}).get("code")
    except Exception:
        return None


def _wa_post(kind, payload, tok, phone):
    r = requests.post(
        f"https://graph.facebook.com/v21.0/{phone}/messages",
        headers={"Authorization": f"Bearer {tok}"},
        timeout=20,
        json=payload,
    )
    body = r.text or ""
    if tok:
        body = body.replace(tok, "[redacted]")
    log(f"whatsapp {kind} HTTP {r.status_code}: {body[:800]}")
    return r, body


def send_whatsapp(text):
    """Send a plain-text WhatsApp message.

    On error 131047 (24h session window closed), automatically fall back to the
    approved 'hello' template (language 'en'). Logs HTTP status and response
    body (token redacted) on every attempt.

    Returns dict with keys: ok, status, body, kind ('text'|'template'|'skipped'|'error'),
    and gig_delivered (True only if the plain-text message itself was accepted).
    """
    tok, phone, to = wa_creds()
    missing = [n for n, v in (("WHATSAPP_TOKEN", tok), ("WA_PHONE_ID", phone), ("WA_TO", to)) if not v]
    if missing:
        log(f"whatsapp skipped: missing {', '.join(missing)}")
        return {"ok": False, "status": 0, "body": "missing " + ", ".join(missing),
                "kind": "skipped", "gig_delivered": False}
    try:
        r, body = _wa_post("text", {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": (text or "")[:4000]},
        }, tok, phone)
    except Exception as e:
        log(f"whatsapp text failed: {e}")
        return {"ok": False, "status": 0, "body": str(e), "kind": "error", "gig_delivered": False}
    if r.status_code < 300:
        return {"ok": True, "status": r.status_code, "body": body, "kind": "text", "gig_delivered": True}
    if _wa_error_code(r.text) == 131047 or "131047" in (r.text or ""):
        log("whatsapp 24h window closed (131047); falling back to template hello/en")
        try:
            r2, body2 = _wa_post("template hello/en", {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {"name": "hello", "language": {"code": "en"}},
            }, tok, phone)
        except Exception as e:
            log(f"whatsapp template failed: {e}")
            return {"ok": False, "status": 0, "body": str(e), "kind": "error", "gig_delivered": False}
        return {"ok": r2.status_code < 300, "status": r2.status_code, "body": body2,
                "kind": "template", "gig_delivered": False}
    return {"ok": False, "status": r.status_code, "body": body, "kind": "text", "gig_delivered": False}


def wa_test():
    res = send_whatsapp("Gig scanner test from GitHub Actions \u2705")
    if res["ok"] and res["kind"] == "text":
        log("whatsapp test delivered as plain text")
        return
    if res["ok"] and res["kind"] == "template":
        log("whatsapp test: 24h window closed; hello template sent. Vinay must reply 'hi' to reopen alerts.")
        return
    log(f"whatsapp test did not succeed (kind={res['kind']} HTTP {res['status']})")
    raise SystemExit(1)


def jload(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


# ---------- fetch ----------
def fetch_projects(q, since):
    params = {"query": q, "sort_field": "time_submitted", "limit": 50, "full_description": "true",
              "job_details": "true", "from_time": int(since)}
    r = requests.get(API, params=params, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]["projects"]


def client_info(seo_url):
    """Public project page SSR JSON carries the client's verification block."""
    r = requests.get(SITE + seo_url, headers={"User-Agent": UA}, timeout=30)
    if r.status_code != 200:
        return None
    m = re.search(r'"client":\{"registrationTime":\d+,"address":\{([^}]*)\},"rating":\{"average":([\d.]+),'
                  r'"reviewCount":(\d+)\},"verification":\{([^}]*)\}', r.text)
    if not m:
        return None
    v = m.group(4)
    country = re.search(r'"country":"([^"]*)"', m.group(1))
    return {"pv": '"paymentVerified":true' in v, "deposit": '"depositMade":true' in v,
            "rating": float(m.group(2)), "reviews": int(m.group(3)),
            "country": country.group(1) if country else ""}


def usd(p):
    rate = (p.get("currency") or {}).get("exchange_rate") or 1.0
    b = p.get("budget") or {}
    return (b.get("minimum") or 0) * rate, (b.get("maximum") or b.get("minimum") or 0) * rate


# ---------- scoring (driven by scoring.json) ----------
def wa_direct_on():
    return (os.environ.get("WA_DIRECT") or "0").strip().lower() in ("1", "true", "yes", "on")


def default_term_pattern(name):
    parts = re.split(r"\s+", name.strip())
    return r"\b" + r"\s+".join(re.escape(p) for p in parts) + r"\b"


def load_scoring():
    cfg = json.load(open(SCORING_PATH))
    compiled = {}
    for group in ("strong", "medium", "negative"):
        terms = []
        for name, weight in (cfg.get(group) or {}).items():
            pat = (cfg.get("patterns") or {}).get(name) or default_term_pattern(name)
            req = (cfg.get("require") or {}).get(name)
            terms.append({
                "name": name,
                "weight": float(weight),
                "re": re.compile(pat, re.I),
                "require": re.compile(req, re.I) if req else None,
                "unless": re.compile((cfg.get("unless") or {})[name], re.I) if (cfg.get("unless") or {}).get(name) else None,
            })
        compiled[group] = terms
    cfg["_compiled"] = compiled
    return cfg


def _usd_pair(g):
    if g.get("_usd"):
        return g["_usd"]
    m = re.search(r"\$(\d+(?:\.\d+)?)\s*-\s*\$(\d+(?:\.\d+)?)", g.get("budget_usd") or "")
    if m:
        return float(m.group(1)), float(m.group(2))
    return 0.0, 0.0


def _field_mult(term, title, skills, desc, cfg, whole):
    if term["require"] and not term["require"].search(whole):
        return 0.0
    if term.get("unless") and term["unless"].search(whole):
        return 0.0
    if term["re"].search(title):
        return float(cfg.get("title_multiplier", 1.6))
    if term["re"].search(skills):
        return float(cfg.get("skills_multiplier", 1.2))
    if term["re"].search(desc):
        return float(cfg.get("description_multiplier", 1.0))
    return 0.0


def keyword_score(g, cfg=None):
    """Fit score 0-10 from scoring.json. Title hits weigh more than description."""
    cfg = cfg or load_scoring()
    title = g.get("title") or ""
    skills = g.get("skills") or ""
    desc = g.get("description") or ""
    whole = f"{title} {skills} {desc}"
    pos = 0.0
    neg = 0.0
    matched, flags = [], []
    for term in cfg["_compiled"]["strong"] + cfg["_compiled"]["medium"]:
        m = _field_mult(term, title, skills, desc, cfg, whole)
        if m:
            pos += term["weight"] * m
            matched.append(term["name"])
    pos = min(float(cfg.get("positive_cap", 7.5)), pos)
    for term in cfg["_compiled"]["negative"]:
        m = _field_mult(term, title, skills, desc, cfg, whole)
        if m:
            neg += term["weight"] * m
            flags.append(term["name"])
    s = pos + neg
    bonuses = cfg.get("bonuses") or {}
    bids = int(g.get("bids") or 0)
    if 0 <= bids <= 5:
        s += float(bonuses.get("bids_0_5", 0))
        flags.append("low-bids")
    if g.get("payment_verified") and g.get("deposit_made"):
        s += float(bonuses.get("verified_deposit", 0))
        flags.append("verified+deposit")
    lo, hi = _usd_pair(g)
    typ = g.get("type") or ""
    if typ == "hourly" and hi >= float(bonuses.get("hourly_min_usd", 5)):
        s += float(bonuses.get("budget_ok", 0))
        flags.append("budget-ok")
    elif typ == "fixed" and hi >= float(bonuses.get("fixed_min_usd", 30)):
        s += float(bonuses.get("budget_ok", 0))
        flags.append("budget-ok")
    penalties = cfg.get("penalties") or {}
    if OFF_STACK.search(title):
        s -= float(penalties.get("off_stack_title", 3))
        flags.append("off-stack title")
    if RED_FLAG.search(whole):
        s -= float(penalties.get("red_flag", 4))
        flags.append("red flag wording")
    s = max(0.0, min(10.0, round(s, 1)))
    alert = float(cfg.get("fit_alert", 4))
    reason_bits = ["matched: " + ", ".join(matched[:10])]
    if flags:
        reason_bits.append(", ".join(flags))
    return {
        "fit_score": s,
        "worth_trying": s >= alert,
        "reason": "; ".join(reason_bits),
        "scorer": "keywords",
        "matched": matched,
        "fit_alert": alert,
    }


def _norm_url(url):
    return (url or "").strip().rstrip("/")


def pending_record(g):
    desc = g.get("description") or g.get("snippet") or ""
    return {
        "title": g.get("title") or "",
        "url": g.get("url") or "",
        "budget": g.get("budget_usd") or g.get("budget") or "",
        "bids": g.get("bids"),
        "payment_verified": bool(g.get("payment_verified")),
        "fit_score": g.get("fit_score"),
        "matched": g.get("matched") or [],
        "snippet": desc[:600],
        "found_at": g.get("found_at"),
    }


def load_pending():
    rows = []
    if not os.path.exists(PENDING):
        return rows
    with open(PENDING) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def save_pending(rows):
    with open(PENDING, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_pending(gigs):
    """Append fit-alert gigs to pending_review.jsonl, deduped by url."""
    pending = load_pending()
    seen = {_norm_url(r.get("url")) for r in pending}
    added = 0
    for g in gigs:
        url = _norm_url(g.get("url"))
        if not url or url in seen:
            continue
        pending.append(pending_record(g))
        seen.add(url)
        added += 1
    save_pending(pending)
    if added or pending:
        log(f"pending: added {added}, queue {len(pending)}")
    return pending, added


def webhook_gap_sec(cfg):
    """Interval from scoring.json, floored at 2 minutes to merge back-to-back scans."""
    interval = float(cfg.get("webhook_min_interval_min", 0) or 0)
    return max(interval, WEBHOOK_MIN_GAP_MIN) * 60


def _webhook_payload(gigs):
    body = {"gigs": []}
    for g in gigs:
        body["gigs"].append({
            "title": g.get("title") or "",
            "url": g.get("url") or "",
            "budget": g.get("budget") or "",
            "bids": g.get("bids"),
            "payment_verified": bool(g.get("payment_verified")),
            "fit_score": g.get("fit_score"),
            "matched": g.get("matched") or [],
            "snippet": (g.get("snippet") or g.get("description") or "")[:600],
        })
    return body


def _post_webhook(gigs):
    """POST gigs. Returns True on HTTP 2xx. Never logs the secret."""
    url = (os.environ.get("ROUTINE_WEBHOOK_URL") or "").strip()
    secret = (os.environ.get("ROUTINE_WEBHOOK_SECRET") or "").strip()
    if not url:
        log("webhook skipped: ROUTINE_WEBHOOK_URL unset")
        return False
    if not secret:
        log("webhook secret missing")
        return False
    body = _webhook_payload(gigs)
    try:
        r = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            timeout=20,
        )
        log(f"webhook HTTP {r.status_code} ({len(body['gigs'])} gig(s))")
        return 200 <= r.status_code < 300
    except Exception as e:
        err = str(e)
        if secret:
            err = err.replace(secret, "[redacted]")
        log(f"webhook failed: {err}")
        return False


def maybe_flush_webhook(cfg, st, now=None):
    """POST pending gigs immediately, with a small gap to merge back-to-back scans.

    Batch is max 10, highest fit first. Leftovers stay in pending_review.jsonl.
    On HTTP 2xx, sent rows are cleared and state last_webhook_at is set.
    """
    now = time.time() if now is None else now
    pending = load_pending()
    alert = float(cfg.get("fit_alert", 4))
    gap = webhook_gap_sec(cfg)
    last = float(st.get("last_webhook_at") or 0)
    if not pending:
        log(f"webhook: pending empty (fit_alert {alert})")
        return st
    elapsed = now - last if last else float("inf")
    if elapsed < gap:
        remain_min = max(0, int((gap - elapsed + 59) // 60))
        log(f"webhook: holding {len(pending)} pending, {int(gap // 60)}m gap, next in ~{remain_min} min")
        return st
    pending_sorted = sorted(
        pending,
        key=lambda g: (-float(g.get("fit_score") or 0), _norm_url(g.get("url"))),
    )
    batch, leftovers = pending_sorted[:WEBHOOK_BATCH], pending_sorted[WEBHOOK_BATCH:]
    log(f"webhook: flushing {len(batch)} (queue had {len(pending)})")
    if _post_webhook(batch):
        save_pending(leftovers)
        st = dict(st)
        st["last_webhook_at"] = now
        log(f"webhook flushed {len(batch)}, {len(leftovers)} still pending")
    else:
        log("webhook flush failed, pending kept")
    return st


def openai_score(g):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    prof = open(PROFILE).read() if os.path.exists(PROFILE) else ""
    fb = open(FEEDBACK).read() if os.path.exists(FEEDBACK) else ""
    prompt = (f"Freelancer profile:\n{prof}\n\nFeedback/preferences:\n{fb}\n\nGig:\nTitle: {g['title']}\n"
              f"Budget: {g['budget']} ({g['budget_usd']})\nBids: {g['bids']}\nSkills: {g['skills']}\n"
              f"Client: {g['client_country']}, rating {g['client_rating']} ({g['client_reviews']} reviews), "
              f"deposit made {g['deposit_made']}\nDescription:\n{g['description'][:3000]}\n\n"
              'Reply with JSON only: {"fit_score": 0-10, "worth_trying": true/false, "reason": "<one sentence>"}')
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions", timeout=60,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), "temperature": 0,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        d = json.loads(r.json()["choices"][0]["message"]["content"])
        return {"fit_score": float(d.get("fit_score", 0)), "worth_trying": bool(d.get("worth_trying")),
                "reason": str(d.get("reason", ""))[:300], "scorer": "openai"}
    except Exception as e:
        log(f"openai scoring failed, using keywords: {e}")
        return None


# ---------- bid draft (never submitted) ----------
OPENERS = ["Hi, I read your brief on \"{t}\" and it lines up closely with work I ship regularly.",
           "Hello, \"{t}\" is right in my lane, and I can start quickly.",
           "Hi there, I've built very similar systems to what \"{t}\" describes."]
PROOF = {"rag": "built production RAG pipelines (LangChain/LangGraph with FAISS, Weaviate and pgvector)",
         "agent": "built LLM agents with tool calling on Azure OpenAI and OpenAI",
         "fastapi": "shipped FastAPI backends serving LLM features",
         "ocr": "built OCR/document-extraction pipelines",
         "voice": "built voice assistants on top of LLMs",
         "data": "worked as a data scientist on PySpark/Databricks (100M+ rows), pricing ML and A/B tests",
         "dash": "built Python dashboards (Streamlit/Plotly) for business users"}


def draft_bid(g, sc):
    text = (g["title"] + " " + g["description"]).lower()
    picks = []
    if re.search(r"rag|retriev|vector|embedding|langchain|langgraph|pdf|knowledge", text):
        picks.append("rag")
    if re.search(r"agent|tool|automation|n8n|workflow|gpt|openai|llm|chatbot", text):
        picks.append("agent")
    if "fastapi" in text or "api" in text:
        picks.append("fastapi")
    if re.search(r"\bocr\b|extract|invoice|document", text):
        picks.append("ocr")
    if "voice" in text:
        picks.append("voice")
    if re.search(r"data scien|machine learning|model|pyspark|databricks|forecast", text):
        picks.append("data")
    if re.search(r"dashboard|streamlit|plotly|power bi", text):
        picks.append("dash")
    picks = (picks or ["agent", "rag"])[:3]
    bullets = "\n".join(f"- I've {PROOF[p]}." for p in picks)
    first = random.choice(OPENERS).format(t=g["title"])
    return (f"{first}\n\nI'm Vinay, an Applied AI engineer with 5+ years in Python (MSc, Politecnico di Milano; "
            f"remote work for Italian companies).\n{bullets}\n\nHow I'd approach it: confirm the exact scope and "
            f"success criteria, share a small working first milestone within a few days, then iterate with you. "
            f"Quick question: what does a successful first version look like for you?\n\nHappy to jump on a "
            f"short call.\nVinay\n")


def write_draft(g, sc, bid):
    os.makedirs(DRAFTS, exist_ok=True)
    path = os.path.join(DRAFTS, f"freelancer_{g['id']}.md")
    with open(path, "w") as f:
        f.write(f"# {g['title']}\n\n- URL: {g['url']}\n- Budget: {g['budget']} ({g['budget_usd']})\n"
                f"- Bids at scan: {g['bids']} | Posted: {g['posted_ist']} IST\n- Client: {g['client_country']}, "
                f"{g['client_rating']}★ ({g['client_reviews']} reviews), payment verified, deposit {g['deposit_made']}\n"
                f"- Fit: {sc['fit_score']}/10 ({sc['scorer']}): {sc['reason']}\n\n## Draft bid (NOT submitted, review first)\n\n{bid}")
    return path


# ---------- pipeline ----------
def scan():
    st = jload(STATE, {})
    now = time.time()
    since = st.get("last_run", now - LOOKBACK_FIRST) - 300
    # freelancer search indexes projects late (often 10-60+ min after time_submitted),
    # so query a wide window; seen_ids + MAX_BIDS keep it from re-alerting.
    since = min(since, now - int(os.environ.get("INDEX_LAG_WINDOW", "10800")))
    seen = set(open(SEEN).read().split()) if os.path.exists(SEEN) else set()
    cands = {}
    ok_queries = 0
    for q in QUERIES:
        try:
            for p in fetch_projects(q, since):
                cands[p["id"]] = p
            ok_queries += 1
        except Exception as e:
            log(f"query {q!r} failed: {e}")
        time.sleep(1)
    log(f"freelancer.in API: {ok_queries}/{len(QUERIES)} queries ok, {len(cands)} unique projects since {int(since)}")
    passed, shortlisted, checked = [], [], 0
    cfg = load_scoring()
    alert = float(cfg.get("fit_alert", 4))
    ntfy = topic()
    direct = wa_direct_on()
    wa_tok, wa_phone, wa_to = wa_creds()
    if direct:
        if wa_tok and wa_phone and wa_to:
            log("whatsapp direct alerts: on (WA_DIRECT=1)")
        else:
            missing = [n for n, v in (("WHATSAPP_TOKEN", wa_tok), ("WA_PHONE_ID", wa_phone), ("WA_TO", wa_to)) if not v]
            log(f"whatsapp direct alerts: on but not configured (missing {', '.join(missing) or 'unknown'})")
    else:
        log("whatsapp direct alerts: off (WA_DIRECT=0)")
    for pid, p in sorted(cands.items(), key=lambda kv: -kv[1]["time_submitted"]):
        if str(pid) in seen or p.get("time_submitted", 0) < since or p.get("status") != "active":
            continue
        bids = (p.get("bid_stats") or {}).get("bid_count") or 0
        if bids >= MAX_BIDS:
            continue
        title = p.get("title", "")
        desc = p.get("description") or p.get("preview_description") or ""
        skills = ", ".join(j.get("name", "") for j in (p.get("jobs") or []))
        if SKIP_TITLE.search(title):
            continue
        if not CORE_RE.search(title + " " + skills + " " + desc):
            continue
        lo, hi = usd(p)
        if p.get("type") == "hourly" and hi < MIN_HOURLY:
            continue
        if p.get("type") == "fixed" and hi < MIN_FIXED:
            continue
        checked += 1
        ci = client_info(p["seo_url"])
        time.sleep(1.5)
        if not ci or not ci["pv"]:
            # clients often verify payment shortly after posting; retry for 3h
            if now - p.get("time_submitted", 0) > 10800:
                seen.add(str(pid))
            continue
        seen.add(str(pid))
        cur = p["currency"]["code"]
        b = p.get("budget") or {}
        unit = "/hr" if p.get("type") == "hourly" else " fixed"
        g = {"id": pid, "platform": "Freelancer.in", "title": title, "url": SITE + p["seo_url"], "bids": bids,
             "type": p.get("type"), "budget": f"{b.get('minimum') or ''}-{b.get('maximum') or ''} {cur}{unit}",
             "budget_usd": f"${lo:.0f}-${hi:.0f}{unit}", "_usd": (lo, hi),
             "posted_ist": datetime.datetime.fromtimestamp(p["time_submitted"], IST).strftime("%Y-%m-%d %H:%M"),
             "found_at": now_ist().strftime("%Y-%m-%d %H:%M"), "payment_verified": True,
             "deposit_made": ci["deposit"], "client_rating": ci["rating"], "client_reviews": ci["reviews"],
             "client_country": ci["country"], "skills": skills, "description": desc}
        sc = keyword_score(g, cfg)
        g.update({k: sc[k] for k in ("fit_score", "worth_trying", "reason", "scorer", "matched")})
        log(f"fit {g['fit_score']}/10 {g['title']!r} matched={g['matched']}")
        passed.append(g)
        if sc["worth_trying"]:
            bid = draft_bid(g, sc)
            g["draft_path"] = write_draft(g, sc, bid)
            shortlisted.append(g)
    with open(SEEN, "w") as f:
        f.write("\n".join(sorted(seen)) + ("\n" if seen else ""))
    st["last_run"] = now
    json.dump(st, open(STATE, "w"))
    if passed:
        cols = ["found_at", "posted_ist", "platform", "title", "url", "bids", "budget", "budget_usd",
                "payment_verified", "deposit_made", "client_rating", "client_reviews", "client_country",
                "skills", "fit_score", "worth_trying", "scorer", "reason"]
        new = not os.path.exists(CSV_OUT)
        with open(CSV_OUT, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(passed)
    for g in shortlisted:
        rec = {k: v for k, v in g.items() if k not in ("_usd", "description")}
        rec["description"] = g["description"][:1500]
        rec["status"] = "new"
        if rec.get("draft_path"):
            rec["draft_path"] = os.path.relpath(rec["draft_path"], BASE)
        with open(SHORTLIST, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        body = (f"Fit {g['fit_score']}/10 | {g['budget_usd']} ({g['budget']}) | {g['bids']} bids | payment verified"
                f"{' + deposit' if g['deposit_made'] else ''} | {g['client_country']} {g['client_rating']}★ "
                f"({g['client_reviews']} reviews) | posted {g['posted_ist']} IST\n{g['reason']}")
        if direct and ntfy:
            try:
                requests.post(f"https://ntfy.sh/{ntfy}", data=body.encode("utf-8"), timeout=20,
                    headers={"Title": ("Freelancer: " + g["title"])[:200].encode("latin-1", "ignore").decode("latin-1"),
                             "Click": g["url"], "Tags": "briefcase",
                             "Priority": "high" if g["fit_score"] >= 8 else "default"})
            except Exception as e:
                log(f"ntfy failed: {e}")
        if direct and wa_tok and wa_phone and wa_to:
            txt = f"\U0001F514 Freelancer gig\n{g['title']}\n{g['url']}\n{body}\n\nReply in Grok Bot chat: like / skip + why."
            res = send_whatsapp(txt)
            if not res["gig_delivered"]:
                log(f"whatsapp did not deliver gig {g['id']} {g['title']!r} (kind={res['kind']} HTTP {res['status']})")
        elif direct and wa_tok:
            log("whatsapp skipped: WA_PHONE_ID or WA_TO not set")
        if direct:
            time.sleep(0.5)
    append_pending(shortlisted)
    st = maybe_flush_webhook(cfg, st, now)
    json.dump(st, open(STATE, "w"))
    scores = ", ".join(f"{g['title'][:40]}={g['fit_score']}" for g in passed) or "none"
    log(f"scan done: {len(cands)} fetched, {checked} page-checked, {len(passed)} passed hard filters, "
        f"{len(shortlisted)} shortlisted (>= fit_alert {alert}), scores: {scores}")
    return passed, shortlisted


def in_window(dt):
    return dt.hour >= 8 and (dt.hour < 23 or (dt.hour == 23 and dt.minute <= 30))


def run_once():
    now = now_ist()
    if not in_window(now):
        log(f"outside scan window (now {now:%Y-%m-%d %H:%M} IST), skipping")
        trim_log()
        return
    import signal
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(TimeoutError("scan timeout")))
    signal.alarm(SCAN_TIMEOUT_SEC)
    try:
        scan()
    finally:
        signal.alarm(0)
        trim_log()


def loop():
    with open(os.path.join(BASE, "scanner.pid"), "w") as f:
        f.write(str(os.getpid()))
    log("loop started")
    last_slot = None
    while True:
        now = now_ist()
        every = max(5, int(os.environ.get("SCAN_EVERY_MIN", "15")))
        slot = (now.strftime("%Y-%m-%d %H"), now.minute // every) if every < 60 else now.strftime("%Y-%m-%d %H")
        if in_window(now) and slot != last_slot:
            try:
                import signal
                signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(TimeoutError("scan timeout")))
                signal.alarm(SCAN_TIMEOUT_SEC)
                scan()
                signal.alarm(0)
            except Exception as e:
                log(f"scan error: {e}")
            finally:
                trim_log()
            last_slot = slot
        time.sleep(60)



def calibrate():
    """Score fixtures + shortlist.jsonl and print a table. Does not write state or send alerts."""
    cfg = load_scoring()
    alert = float(cfg.get("fit_alert", 4))
    rows = []
    rag = {
        "title": "Build a RAG chatbot with LangChain in Python",
        "description": "Need a production RAG chatbot using LangChain, Python, and a vector database. "
                       "Should use embeddings and OpenAI or Azure OpenAI.",
        "skills": "Python, LangChain, LLM, Chatbot, RAG",
        "type": "fixed", "_usd": (200, 500), "bids": 2,
        "payment_verified": True, "deposit_made": True,
        "client_reviews": 0, "client_rating": 0,
    }
    rows.append(("FIXTURE", rag))
    if os.path.exists(SHORTLIST):
        for line in open(SHORTLIST):
            line = line.strip()
            if line:
                g = json.loads(line)
                rows.append(("shortlist", g))
    print(f"fit_alert={alert}")
    print(f"{'score':>5}  {'alert':<5}  {'src':<10}  title  reason")
    for src, g in rows:
        sc = keyword_score(g, cfg)
        flag = "YES" if sc["worth_trying"] else "no"
        print(f"{sc['fit_score']:5}  {flag:<5}  {src:<10}  {g.get('title','')[:70]}  | {sc['reason']}")


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--wa-test" in args or "wa-test" in args:
        wa_test()
    elif "--calibrate" in args or "calibrate" in args:
        calibrate()
    elif "--loop" in args or "loop" in args:
        loop()
    else:
        try:
            run_once()
        except Exception as e:
            log(f"scan error: {e}")
            trim_log()
            raise
