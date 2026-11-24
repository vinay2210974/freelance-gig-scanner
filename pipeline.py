#!/usr/bin/env python3
"""Vinay's freelance gig pipeline (GitHub Actions / local --once).

fetch (Freelancer India public API) -> hard filters (payment verified,
<=15 bids, keywords, budget, skip trading) -> dedupe -> fit score
(keyword rules; OpenAI only if OPENAI_API_KEY is set) -> worth-trying
gigs go to shortlist.jsonl + drafts/<id>.md (bid draft, not committed)
+ ntfy + WhatsApp. Every hard-filter pass is logged to new_matches.csv.
NEVER submits bids.

Usage: python pipeline.py --once | --loop
  --once (default): one scan, skip if outside 08:00-23:30 Asia/Kolkata.
  --loop: box loop every SCAN_EVERY_MIN minutes inside the same window.
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
DRAFTS = os.path.join(BASE, "drafts")
PROFILE = os.path.join(BASE, "fit_profile.md")
FEEDBACK = os.path.join(BASE, "feedback.md")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
API = "https://www.freelancer.in/api/projects/0.1/projects/active/"
SITE = "https://www.freelancer.in/projects/"
QUERIES = ['ai', 'llm', 'chatbot', 'automation', 'agent', 'voice', 'speech', 'data', 'intelligence', 'workflow', 'python', 'n8n', 'chatgpt', 'api', 'whatsapp', 'email', 'website', 'webapp', 'app']
MAX_BIDS = int(os.environ.get("MAX_BIDS", "16"))
MIN_HOURLY = float(os.environ.get("MIN_HOURLY", "5"))
MIN_FIXED = float(os.environ.get("MIN_FIXED", "0"))
WORTH = float(os.environ.get("WORTH", "0"))
LOOKBACK_FIRST = 3 * 3600
SCAN_TIMEOUT_SEC = int(os.environ.get("SCAN_TIMEOUT_SEC", "420"))

# keyword -> weight for fit scoring
WEIGHTS = {r"\brag\b|retrieval[- ]augmented": 2.5, r"langchain|langgraph|llama ?index": 2.5,
    r"\bllms?\b|large language model": 2, r"gen ?ai|generative ai": 2, r"\bagents?\b|agentic|tool calling": 2,
    r"openai|azure openai|gpt-?\d|chat ?gpt|claude|gemini": 1.5, r"vector|embedding|faiss|pinecone|weaviate|pgvector|chroma": 1.5,
    r"\bpython\b": 1.5, r"fastapi": 1.5, r"chat ?bots?": 1, r"ai automation|n8n|automation": 0.5,
    r"\bocr\b|document (extraction|parsing)|pdf": 1, r"voice (assistant|agent|bot)": 1,
    r"pyspark|databricks": 1, r"dashboards?|streamlit|plotly": 0.5, r"data scien|machine learning|\bnlp\b": 1}
SKILL_RE = re.compile("|".join(f"(?:{k})" for k in WEIGHTS), re.I)
CORE_RE = re.compile(r"\b(ai|llms?|chat ?bots?|automations?|agents?|voice|speech|data|intelligence|workflows?|python|n8n|chat ?gpt|apis?|whatsapp|e-?mails?|websites?|web ?apps?|apps?)\b", re.I)
SKIP_TITLE = re.compile(r"\b(trading|trader|forex|crypto trading|stock market|options trading|wordpress|logo|video edit|data entry|tiktok|followers|essay|assignment|homework|"
    r"thesis|dissertation|virtual assistant)\b", re.I)
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


# ---------- scoring ----------
def keyword_score(g):
    text = g["title"] + " " + g["description"] + " " + g["skills"]
    hits = [k for k in WEIGHTS if re.search(k, text, re.I)]
    s = min(7.0, sum(WEIGHTS[k] for k in hits))
    _, hi = g["_usd"]
    s += 1 if (g["type"] == "hourly" and hi >= 25) or (g["type"] == "fixed" and hi >= 300) else 0
    s += 1 if g["deposit_made"] else 0
    s += 0.5 if g["client_reviews"] > 0 and g["client_rating"] >= 4 else 0
    s += 0.5 if g["bids"] <= 3 else 0
    reasons = []
    if OFF_STACK.search(g["title"]):
        s -= 3
        reasons.append("off-stack title")
    if RED_FLAG.search(text):
        s -= 4
        reasons.append("red flag wording")
    s = max(0.0, min(10.0, round(s, 1)))
    matched = sorted({re.search(k, text, re.I).group(0).strip().lower() for k in hits})
    reasons.insert(0, "matched: " + ", ".join(matched[:8]))
    return {"fit_score": s, "worth_trying": s >= WORTH, "reason": "; ".join(reasons), "scorer": "keywords",
            "matched": matched}


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
    ntfy = topic()
    wa_tok = (os.environ.get("WHATSAPP_TOKEN") or "").strip()
    wa_phone = (os.environ.get("WA_PHONE_ID") or "").strip()
    wa_to = (os.environ.get("WA_TO") or "").strip()
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
        seen.add(str(pid))
        if not ci or not ci["pv"]:
            continue
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
        sc = openai_score(g) or keyword_score(g)
        g.update({k: sc[k] for k in ("fit_score", "worth_trying", "reason", "scorer")})
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
        if ntfy:
            try:
                requests.post(f"https://ntfy.sh/{ntfy}", data=body.encode("utf-8"), timeout=20,
                    headers={"Title": ("Freelancer: " + g["title"])[:200].encode("latin-1", "ignore").decode("latin-1"),
                             "Click": g["url"], "Tags": "briefcase",
                             "Priority": "high" if g["fit_score"] >= 8 else "default"})
            except Exception as e:
                log(f"ntfy failed: {e}")
        if wa_tok and wa_phone and wa_to:
            try:
                txt = f"\U0001F514 Freelancer gig\n{g['title']}\n{g['url']}\n{body}\n\nReply in Grok Bot chat: like / skip + why."
                r = requests.post(f"https://graph.facebook.com/v21.0/{wa_phone}/messages",
                    headers={"Authorization": f"Bearer {wa_tok}"}, timeout=20,
                    json={"messaging_product": "whatsapp", "to": wa_to,
                          "type": "text", "text": {"body": txt[:4000]}})
                if r.status_code >= 300:
                    log(f"whatsapp failed {r.status_code}: {r.text[:200]}")
            except Exception as e:
                log(f"whatsapp failed: {e}")
        elif wa_tok:
            log("whatsapp skipped: WA_PHONE_ID or WA_TO not set")
        time.sleep(0.5)
    log(f"scan done: {len(cands)} fetched, {checked} page-checked, {len(passed)} passed hard filters, "
        f"{len(shortlisted)} shortlisted (scorer: {'openai' if os.environ.get('OPENAI_API_KEY') else 'keywords'})")
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


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--loop" in args or "loop" in args:
        loop()
    else:
        try:
            run_once()
        except Exception as e:
            log(f"scan error: {e}")
            trim_log()
            raise
