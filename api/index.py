"""
Aangan Voice Desk: the backend behind Aangan Studio's AI phone assistant.

One WSGI app, deployed as a single Vercel Python function. Routes:

  Voice agent (Vaani), protected by WEBHOOK_SECRET (?key= or X-Webhook-Secret):
    POST /api/vaani/check_caller   new / repeat enquiry / existing client
    POST /api/vaani/get_slots      open consultation slots for the area
    POST /api/vaani/book_consult   book a slot during the call
    POST /api/vaani/escalate       existing client with a problem -> Nikhil now
    POST /api/vaani/call_ended     end-of-call transcript -> lead

  Dashboard (public/index.html calls these):
    GET  /api/stats                funnel, speed, decline reasons, cost
    GET  /api/leads                recent leads (phone numbers masked)
    GET  /api/test-calls           the 20 September phone calls
    POST /api/simulate             replay a test call through the full pipeline
    POST /api/leads/<id>/action    the same outcome taps a designer makes in Telegram

  Telegram + timers:
    POST /api/telegram             button taps from designers
    POST /api/setup-telegram       points the bot's webhook at this deployment
    GET  /api/cron/daily           10am IST: SLA sweep, follow-ups, digest to Nikhil
    GET  /api/health
"""

import json
import os
import random
import re
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

MODEL = "claude-opus-5-5"
IST = timezone(timedelta(hours=5, minutes=30))
OPEN_HOUR, CLOSE_HOUR = 10, 19
SIMULATIONS_PER_DAY = int(os.environ.get("SIMULATIONS_PER_DAY", "40"))


def env(name, default=""):
    return os.environ.get(name, default)


# --------------------------------------------------------------------- store
# Upstash Redis over REST when configured (Vercel Marketplace sets KV_REST_API_*),
# otherwise an in-memory dict so the app still runs (data resets on cold start).

_MEM = {}


class Store:
    def __init__(self):
        self.url = env("KV_REST_API_URL") or env("UPSTASH_REDIS_REST_URL")
        self.token = env("KV_REST_API_TOKEN") or env("UPSTASH_REDIS_REST_TOKEN")
        self.persistent = bool(self.url and self.token)

    def _cmd(self, *args):
        req = urllib.request.Request(
            self.url, data=json.dumps([str(a) for a in args]).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())["result"]

    def get(self, k):
        return self._cmd("GET", k) if self.persistent else _MEM.get(k)

    def set(self, k, v, ex=None):
        if self.persistent:
            return self._cmd("SET", k, v, "EX", ex) if ex else self._cmd("SET", k, v)
        _MEM[k] = v

    def setnx(self, k, v, ex):
        if self.persistent:
            return self._cmd("SET", k, v, "NX", "EX", ex) == "OK"
        if k in _MEM:
            return False
        _MEM[k] = v
        return True

    def incr(self, k, ex=None):
        if self.persistent:
            n = self._cmd("INCR", k)
            if ex and n == 1:
                self._cmd("EXPIRE", k, ex)
            return n
        _MEM[k] = int(_MEM.get(k, 0)) + 1
        return _MEM[k]

    def lpush(self, k, v):
        if self.persistent:
            return self._cmd("LPUSH", k, v)
        _MEM.setdefault(k, []).insert(0, v)

    def ltrim(self, k, a, b):
        if self.persistent:
            return self._cmd("LTRIM", k, a, b)
        _MEM[k] = _MEM.get(k, [])[a:b + 1]

    def lrange(self, k, a, b):
        if self.persistent:
            return self._cmd("LRANGE", k, a, b) or []
        items = _MEM.get(k, [])
        return items[a:] if b == -1 else items[a:b + 1]

    def mget(self, keys):
        if not keys:
            return []
        if self.persistent:
            return self._cmd("MGET", *keys)
        return [_MEM.get(k) for k in keys]


store = Store()


def save_lead(lead):
    store.set("lead:" + lead["lead_id"], json.dumps(lead, default=str))


def get_lead(lead_id):
    raw = store.get("lead:" + lead_id)
    return json.loads(raw) if raw else None


def update_lead(lead_id, **fields):
    lead = get_lead(lead_id)
    if not lead:
        return None
    lead.update(fields)
    save_lead(lead)
    return lead


def all_leads(limit=500):
    ids = store.lrange("leads", 0, limit - 1)
    return [json.loads(x) for x in store.mget(["lead:" + i for i in ids]) if x]


# ----------------------------------------------------------------- reference

SERVICE_AREAS = [
    "kothrud", "baner", "aundh", "wakad", "koregaon park", "kalyani nagar", "viman nagar",
    "hadapsar", "magarpatta", "nibm", "kondhwa", "undri", "shivane", "warje", "erandwane",
    "deccan", "pimpri", "chinchwad", "pimple saudagar", "pimple nilakh", "ravet", "hinjewadi"]


def designers():
    """DESIGNERS_JSON env overrides the demo roster. Every chat ID falls back to TELEGRAM_CHAT_ID."""
    default = [
        {"name": "Aryan", "types": ["residential", "commercial"],
         "areas": ["kothrud", "aundh", "baner", "wakad", "hinjewadi", "pimple saudagar", "pimple nilakh",
                   "ravet", "pimpri", "chinchwad", "warje", "shivane", "erandwane", "deccan"]},
        {"name": "Meera", "types": ["residential"],
         "areas": ["koregaon park", "kalyani nagar", "viman nagar", "hadapsar", "magarpatta", "nibm",
                   "kondhwa", "undri"]},
        {"name": "Kabir", "types": ["commercial", "residential"], "areas": ["baner", "hinjewadi", "chinchwad", "kalyani nagar"]},
    ]
    try:
        roster = json.loads(env("DESIGNERS_JSON")) if env("DESIGNERS_JSON") else default
    except ValueError:
        roster = default
    for d in roster:
        d.setdefault("chat_id", env("TELEGRAM_CHAT_ID"))
    return roster


def existing_clients():
    """Live projects. Demo: Sheetal (T09) so the escalation path can be tested."""
    try:
        return json.loads(env("CLIENTS_JSON")) if env("CLIENTS_JSON") else [
            {"phone": "9800000009", "name": "Sheetal Deshpande", "designer": "Aryan",
             "project": "2BHK, Viman Nagar, execution month 3"}]
    except ValueError:
        return []


def load_test_calls():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", "data", "test_calls.json"), os.path.join(os.getcwd(), "data", "test_calls.json")):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return []


# ----------------------------------------------------------------- utilities

def now_ist():
    return datetime.now(IST)


def parse_dt(v):
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, IST)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=IST)).astimezone(IST)
        except ValueError:
            pass
    return now_ist()


def phone_key(p):
    d = re.sub(r"\D", "", str(p or ""))
    return d[-10:] if len(d) >= 10 else ""


def mask_phone(p):
    k = phone_key(p)
    return ("•••• " + k[-4:]) if k else ""


def new_id(prefix):
    return prefix + now_ist().strftime("%y%m%d") + "-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


def pick(obj, *paths):
    for path in paths:
        cur = obj
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur not in (None, "", []):
            return cur
    return None


def is_after_hours(d):
    return d.weekday() == 6 or d.hour < OPEN_HOUR or d.hour >= CLOSE_HOUR


def next_office_time(d):
    """After-hours leads start the designer's 60-minute clock when the studio opens."""
    if not is_after_hours(d):
        return d
    day = d if d.hour < OPEN_HOUR and d.weekday() != 6 else d + timedelta(days=1)
    if day.weekday() == 6:
        day += timedelta(days=1)
    return day.replace(hour=OPEN_HOUR, minute=0, second=0, microsecond=0)


def fmt_when(d):
    return d.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


# ------------------------------------------------------------ mid-call tools

def check_caller(phone):
    key = phone_key(phone)
    if not key:
        return {"status": "new", "result": "New caller."}
    for c in existing_clients():
        if phone_key(c["phone"]) == key:
            return {"status": "existing_client", "name": c["name"], "designer": c["designer"],
                    "result": "Existing client %s (designer %s, %s). Do not qualify. Listen, apologise if needed, then call escalate."
                              % (c["name"], c["designer"], c["project"])}
    last_id = store.get("phone:" + key)
    last = get_lead(last_id) if last_id else None
    if last:
        return {"status": "repeat_enquiry", "name": last.get("caller_name", ""),
                "result": "Called before on %s: %s. Greet by name; do not re-ask what is known."
                          % (parse_dt(last["received_at"]).strftime("%d %b"), last.get("summary", ""))}
    return {"status": "new", "result": "New caller."}


def match_designers(locality, project_type):
    loc = (locality or "").lower()
    kind = "commercial" if "commercial" in (project_type or "").lower() else "residential"
    roster = designers()
    by_type = [d for d in roster if kind in d.get("types", [])] or roster
    by_area = [d for d in by_type if loc and any(a in loc or loc in a for a in d.get("areas", []))]
    pool = by_area or by_type
    load = {}
    for l in all_leads(200):
        if not l.get("outcome") and l.get("designer"):
            load[l["designer"]] = load.get(l["designer"], 0) + 1
    return sorted(pool, key=lambda d: load.get(d["name"], 0))


def candidate_slots(designer_names, days=10):
    """Rolling slots: Mon–Sat, 11:00 and 16:00, per designer. Booked ones are stored in Redis."""
    out, start = [], now_ist()
    for i in range(1, days + 1):
        day = start + timedelta(days=i)
        if day.weekday() == 6:
            continue
        for hour in (11, 16):
            for n in designer_names:
                when = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                out.append({"slot_id": "%s-%s" % (n.lower(), when.strftime("%m%d%H")), "designer": n, "start": when})
    return out


def get_slots(locality, project_type):
    names = [d["name"] for d in match_designers(locality, project_type)][:2]
    free = [s for s in candidate_slots(names) if not store.get("slot:" + s["slot_id"])][:3]
    return {"slots": [{"slot_id": s["slot_id"], "designer": s["designer"], "when": fmt_when(s["start"])} for s in free],
            "result": "Offer two of these: " + "; ".join("%s = %s" % (s["slot_id"], fmt_when(s["start"])) for s in free)}


def book_consult(a):
    sid = str(a.get("slot_id", ""))
    slot = next((s for s in candidate_slots([d["name"] for d in designers()], 14) if s["slot_id"] == sid), None)
    if not slot:
        return {"booked": False, "result": "That slot doesn't exist. Call get_slots again."}
    booking = {"slot_id": sid, "designer": slot["designer"], "start": slot["start"].isoformat(),
               "mode": a.get("mode", "either"), "phone": a.get("phone", ""), "caller_name": a.get("caller_name", "")}
    if not store.setnx("slot:" + sid, json.dumps(booking), 60 * 60 * 24 * 30):
        return {"booked": False, "result": "That slot was just taken. Call get_slots again."}
    if phone_key(booking["phone"]):
        store.set("booking:" + phone_key(booking["phone"]), json.dumps(booking), 60 * 60 * 24)
    where = "a site visit" if booking["mode"] == "site_visit" else "at the studio"
    return {"booked": True, "designer": slot["designer"], "when": fmt_when(slot["start"]),
            "result": "Booked %s with %s, %s. Confirm this to the caller." % (fmt_when(slot["start"]), slot["designer"], where)}


def escalate(a):
    text = ("🚨 <b>Existing client needs a callback within 15 min</b>\n<b>%s</b> · %s\n%sIssue: %s%s"
            % (esc(a.get("caller_name", "Unknown")), esc(a.get("phone", "")),
               ("Designer: " + esc(a["designer"]) + "\n") if a.get("designer") else "",
               esc(a.get("issue", "")), "\n<b>Asked for Nikhil personally.</b>" if a.get("wants_nikhil") else ""))
    tg_send(env("NIKHIL_CHAT_ID") or env("TELEGRAM_CHAT_ID"), text)
    return {"escalated": True, "result": "Escalated. Tell the caller a senior person will call back within 15 minutes."}


# ------------------------------------------------------ Claude: transcript -> lead

LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "caller_name": {"type": "string"},
        "call_type": {"type": "string", "enum": ["new_enquiry", "existing_client", "vendor_or_other", "missed_or_empty"]},
        "source": {"type": "string", "enum": ["referral", "instagram", "linkedin", "google", "builder", "repeat_caller", "press", "other", "unknown"]},
        "referrer": {"type": "string"},
        "locality": {"type": "string"},
        "in_service_area": {"type": "string", "enum": ["yes", "no", "unsure"]},
        "project_type": {"type": "string", "enum": ["residential_full", "residential_partial", "single_room", "commercial_office", "commercial_other", "out_of_scope_type", "unknown"]},
        "carpet_sqft": {"type": "integer", "description": "0 if unknown"},
        "rooms_scope": {"type": "string"},
        "current_state": {"type": "string"},
        "timeline": {"type": "string", "description": "Caller's words plus the resolved month"},
        "target_date": {"type": "string", "description": "YYYY-MM or empty"},
        "decision_maker": {"type": "string", "enum": ["yes", "represented", "unclear", "no"]},
        "budget_volunteered": {"type": "string", "description": "Only if the caller said a number"},
        "criteria": {
            "type": "object",
            "properties": {k: {"type": "string", "enum": ["pass", "fail", "unclear"]} for k in
                           ["c1_real_project", "c2_service_area", "c3_timeline", "c4_budget", "c5_decision_maker"]},
            "required": ["c1_real_project", "c2_service_area", "c3_timeline", "c4_budget", "c5_decision_maker"],
            "additionalProperties": False},
        "verdict": {"type": "string", "enum": ["qualified", "not_fit", "nurture", "escalation", "incomplete"]},
        "fail_reasons": {"type": "array", "items": {"type": "string", "enum": ["advice_only", "outside_area", "timeline_too_short", "budget_misaligned", "not_decision_maker", "out_of_scope_type", "below_minimum_size", "other"]}},
        "flags": {"type": "array", "items": {"type": "string", "enum": ["asked_price", "frustrated", "repeat_caller", "vip_referral", "proxy_caller", "site_visit_requested", "tenant", "hindi_or_marathi", "price_spoken_by_agent", "edge_locality"]}},
        "nurture_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
        "preferred_times": {"type": "string"},
        "summary": {"type": "string", "description": "One line, under 25 words"},
        "handoff_note": {"type": "string", "description": "2-4 short sentences for the designer"},
        "dont_reask": {"type": "array", "items": {"type": "string"}},
        "still_unknown": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
LEAD_SCHEMA["required"] = list(LEAD_SCHEMA["properties"].keys())

EXTRACT_SYSTEM = """You read phone call transcripts for Aangan Studio, an interior design studio in Pune, and fill in a lead record.
Use only what was said on the call. Never invent facts; use empty strings, 0 or "unknown" when something was not said.

Studio scope: residential (apartments, houses, villas: full home, a floor, 2+ rooms, or one room with full execution) and offices/clinics/studios up to ~3,000 sq ft.
Out of scope: restaurants, hotels, retail, gyms (out_of_scope_type); advice/styling/colour only, standalone Vastu, standalone furniture sourcing (advice_only); structural or architecture work; commercial spaces under ~500 sq ft (below_minimum_size).
Service area: Pune city and PCMC: Kothrud, Baner, Aundh, Wakad, Koregaon Park, Kalyani Nagar, Viman Nagar, Hadapsar, Magarpatta, NIBM, Kondhwa, Undri, Shivane, Warje, Erandwane, Deccan, Pimpri, Chinchwad, Pimple Saudagar, Pimple Nilakh, Ravet, Hinjewadi and adjoining areas.
Not served: Talegaon, Lonavala, Nashik, Mumbai, other cities. Places near Pune that are not listed (e.g. Kharadi, Nanded City, Wagholi): in_service_area "unsure" and flag edge_locality.
Timelines: design takes 3-4 weeks, execution 8-16 weeks. If the caller needs the project complete within ~8-10 weeks of the call date, criterion 3 fails.

The founder's five criteria:
1 real project with execution;
2 in service area;
3 realistic timeline;
4 budget: fail ONLY if the caller volunteered a number clearly below any project of that scope (e.g. 1-1.5 lakh for kitchen + bedroom, or for a full flat). If budget is not mentioned, pass;
5 decision-maker on the call or represented: a relative calling for owners who will attend the consultation counts as represented (pass), but flag proxy_caller.

Verdict:
- escalation: an existing client with a live project (complaint or update). Mark criteria unclear.
- incomplete: missed, dropped, or too little said to judge.
- not_fit: any of criteria 1-4 clearly fails, or the project type is out of scope or too small.
- nurture: the only problem is timing (or the caller is just exploring a real project) and they are open to a later start; set nurture_date.
- qualified: nothing fails. Unclear on 4 or 5 still counts as qualified.

Flags: asked_price if the caller asked about cost; price_spoken_by_agent if the studio side stated any rupee figure, range or per-sq-ft rate; frustrated if the caller was upset; repeat_caller if they said they called before; vip_referral if referred by a friend of the founder or a named past client; site_visit_requested; tenant; hindi_or_marathi if the caller used those languages.
Resolve relative dates ("by March", "in six weeks", "after Diwali") against the call date and write the month in timeline."""


def claude_extract(transcript, phone, started_at):
    """Returns (lead_fields_dict, cost_in_inr)."""
    import anthropic  # installed on Vercel from requirements.txt

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=EXTRACT_SYSTEM,
        output_config={"effort": env("CLAUDE_EFFORT", "medium"),
                       "format": {"type": "json_schema", "schema": LEAD_SCHEMA}},
        messages=[{"role": "user", "content":
                   "Call date and time (IST): %s\nCaller number: %s\n\nTranscript:\n%s"
                   % (started_at.strftime("%A %d %B %Y, %I:%M %p"), phone or "unknown", transcript)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this transcript")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
    text = next(b.text for b in response.content if b.type == "text")
    usd = (response.usage.input_tokens * 4 + response.usage.output_tokens * 20) / 1e6  # Opus 5.5: $4 / $20 per MTok
    return json.loads(text), round(usd * float(env("USD_INR", "84")), 2)


GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3-flash", "gemini-2.5-flash"]


def gemini_json(system, prompt):
    """One JSON-mode call to Gemini. Returns (parsed_json, cost_usd). Tries the next model name on 404."""
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }).encode()
    models = [env("GEMINI_MODEL")] if env("GEMINI_MODEL") else GEMINI_MODELS
    last_error = None
    for model in models:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": env("GEMINI_API_KEY")})
        try:
            with urllib.request.urlopen(req, timeout=55) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last_error = "Gemini %s: %s %s" % (model, e.code, e.read().decode()[:200])
            if e.code == 404:
                continue  # model name not available on this key; try the next one
            raise RuntimeError(last_error)
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        u = data.get("usageMetadata", {})
        usd = (u.get("promptTokenCount", 0) * float(env("GEMINI_IN_PER_MTOK_USD", "0.3")) +
               u.get("candidatesTokenCount", 0) * float(env("GEMINI_OUT_PER_MTOK_USD", "2.5"))) / 1e6
        return json.loads(text), usd
    raise RuntimeError(last_error or "No Gemini model available")


def gemini_extract(transcript, phone, started_at):
    """Same job on Google Gemini (used when GEMINI_API_KEY is set and ANTHROPIC_API_KEY is not)."""
    prompt = ("Call date and time (IST): %s\nCaller number: %s\n\nTranscript:\n%s\n\n"
              "Reply with ONE JSON object only, matching this JSON Schema exactly (all keys required, use the enum values given):\n%s"
              % (started_at.strftime("%A %d %B %Y, %I:%M %p"), phone or "unknown", transcript, json.dumps(LEAD_SCHEMA)))
    data, usd = gemini_json(EXTRACT_SYSTEM, prompt)
    return clean_lead(data), round(usd * float(env("USD_INR", "84")), 2)


def clean_lead(x):
    """Coerce a model's JSON to the schema: fill missing keys, drop unknown enum values."""
    out = {}
    for k, spec in LEAD_SCHEMA["properties"].items():
        v = x.get(k) if isinstance(x, dict) else None
        if spec["type"] == "string":
            v = "" if v is None else str(v)
            if "enum" in spec and v not in spec["enum"]:
                v = {"verdict": "incomplete", "decision_maker": "unclear", "in_service_area": "unsure"}.get(k, spec["enum"][-1])
        elif spec["type"] == "integer":
            try:
                v = int(float(v or 0))
            except (TypeError, ValueError):
                v = 0
        elif spec["type"] == "array":
            v = [str(i) for i in (v if isinstance(v, list) else [])]
            allowed = spec["items"].get("enum")
            if allowed:
                v = [i for i in v if i in allowed]
        elif spec["type"] == "object":
            v = v if isinstance(v, dict) else {}
            v = {ck: (v.get(ck) if v.get(ck) in ("pass", "fail", "unclear") else "unclear") for ck in spec["properties"]}
        out[k] = v
    return out


def ai_provider():
    if env("ANTHROPIC_API_KEY"):
        return "claude"
    if env("GEMINI_API_KEY"):
        return "gemini"
    return ""


def extract(transcript, phone, started_at):
    if ai_provider() == "gemini":
        return gemini_extract(transcript, phone, started_at)
    return claude_extract(transcript, phone, started_at)


# Tests swap this out so the pipeline can run without an API key.
EXTRACTOR = {"fn": extract}

PRICE_PATTERN = re.compile(r"(₹|\brs\.?\s*\d|\d+(\.\d+)?\s*(lakh|lac|k\b)|per\s*(sq|square))", re.I)
STUDIO_SIDE = re.compile(r"^\s*(assistant|agent|bot|ai|front desk|studio)\s*:", re.I)


def agent_spoke_price(transcript):
    return any(STUDIO_SIDE.match(l) and PRICE_PATTERN.search(l) for l in transcript.splitlines())


# ------------------------------------------------------------- the pipeline

def normalise_call(b):
    t = pick(b, "transcript", "call.transcript", "data.transcript", "call_transcript", "analysis.transcript",
             "conversation", "messages", "call.messages", "data.messages")
    if isinstance(t, list):
        t = "\n".join("%s: %s" % (m.get("role") or m.get("speaker") or "", m.get("content") or m.get("text") or m.get("message") or "")
                      for m in t if isinstance(m, dict))
    secs = pick(b, "duration", "duration_seconds", "call_duration", "call.duration", "data.duration") or 0
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        secs = 0
    cost = pick(b, "cost", "total_cost", "call_cost", "call.cost", "data.cost") or 0
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        cost = 0
    return {
        "call_id": str(pick(b, "call_id", "callId", "call.id", "data.call_id", "id") or new_id("C")),
        "phone": str(pick(b, "from", "from_number", "caller", "caller_number", "customer.number", "call.from", "data.from", "phone") or ""),
        "started_at": parse_dt(pick(b, "started_at", "start_time", "startedAt", "call.started_at", "data.started_at", "created_at", "timestamp")),
        "duration_min": round(secs / 60, 1),
        "reported_cost": cost,
        "recording_url": str(pick(b, "recording_url", "recordingUrl", "call.recording_url", "data.recording_url") or ""),
        "transcript": str(t or ""),
    }


def process_call(payload, notify=True):
    c = normalise_call(payload)
    rate = float(env("VOICE_RATE_PER_MIN_INR", "8"))
    lead = {
        "lead_id": new_id("L"), "received_at": c["started_at"].isoformat(), "call_id": c["call_id"],
        "phone": c["phone"], "after_hours": is_after_hours(c["started_at"]),
        "duration_min": c["duration_min"],
        "call_cost_inr": round(c["reported_cost"] * float(env("VOICE_COST_FX", "1")) if c["reported_cost"] else c["duration_min"] * rate, 2),
        "ai_cost_inr": 0, "recording_url": c["recording_url"], "transcript": c["transcript"][:30000],
        "simulated": bool(payload.get("simulated")), "events": [],
    }

    if len(re.sub(r"\s", "", c["transcript"])) < 60:
        lead.update(call_type="missed_or_empty", verdict="incomplete", summary="Missed or dropped call. Call back.",
                    fail_reasons=[], flags=[], handoff_note="")
        _store_new_lead(lead)
        if notify:
            tg_send(env("FRONTDESK_CHAT_ID") or env("TELEGRAM_CHAT_ID"),
                    "📞 Missed or dropped call from <b>%s</b> at %s. Please call back."
                    % (esc(c["phone"] or "unknown number"), fmt_when(c["started_at"])))
        return lead

    ext, ai_cost = EXTRACTOR["fn"](c["transcript"], c["phone"], c["started_at"])
    if agent_spoke_price(c["transcript"]) and "price_spoken_by_agent" not in ext["flags"]:
        ext["flags"].append("price_spoken_by_agent")
    lead.update({k: ext[k] for k in LEAD_SCHEMA["properties"] if k != "criteria"})
    lead["criteria"] = ext["criteria"]
    lead["ai_cost_inr"] = ai_cost

    booking_raw = store.get("booking:" + phone_key(c["phone"])) if phone_key(c["phone"]) else None
    if booking_raw:
        b = json.loads(booking_raw)
        lead.update(consult_booked=True, designer=b["designer"],
                    consult_slot="%s · %s" % (fmt_when(parse_dt(b["start"])),
                                              {"site_visit": "site visit", "studio": "studio"}.get(b["mode"], "mode TBC")))
    else:
        lead["consult_booked"] = False
    if lead["verdict"] == "qualified" and not lead.get("designer"):
        pool = match_designers(lead["locality"], lead["project_type"])
        lead["designer"] = pool[0]["name"] if pool else ""

    lead["handoff_text"] = handoff_text(lead) if lead["verdict"] == "qualified" else ""
    _store_new_lead(lead)
    if notify:
        route(lead)
    return lead


def _store_new_lead(lead):
    save_lead(lead)
    store.lpush("leads", lead["lead_id"])
    if phone_key(lead.get("phone")):
        store.set("phone:" + phone_key(lead["phone"]), lead["lead_id"])


def route(lead):
    nikhil = env("NIKHIL_CHAT_ID") or env("TELEGRAM_CHAT_ID")
    desk = env("FRONTDESK_CHAT_ID") or env("TELEGRAM_CHAT_ID")
    v = lead["verdict"]
    if v == "qualified":
        d = next((x for x in designers() if x["name"] == lead.get("designer")), None)
        tg_send((d or {}).get("chat_id") or desk, lead["handoff_text"], keyboard("new", lead["lead_id"]))
        update_lead(lead["lead_id"], handed_off_at=now_ist().isoformat())
    elif v == "escalation":
        tg_send(nikhil, "🚨 <b>Existing client call</b>: %s\n%s" % (esc(lead.get("caller_name") or lead["phone"]), esc(lead["summary"])))
    elif v == "nurture":
        tg_send(desk, "🗓 <b>Follow up later</b>: %s (%s)\n%s\nOn: <b>%s</b>"
                % (esc(lead.get("caller_name") or "—"), esc(lead["phone"]), esc(lead["summary"]), esc(lead.get("nurture_date") or "in 4 weeks")))
    elif lead.get("in_service_area") == "unsure":
        tg_send(desk, "❓ Area check needed: %s, %s\n%s" % (esc(lead.get("caller_name") or lead["phone"]), esc(lead.get("locality")), esc(lead["summary"])))
    if "price_spoken_by_agent" in lead.get("flags", []):
        tg_send(nikhil, "⚠️ The assistant may have said a price on call %s. Please check the transcript." % lead["lead_id"])


def handoff_text(l):
    tags = " ".join("#" + f for f in l.get("flags", []))
    lines = [
        "🏠 <b>New qualified lead</b> · %s%s" % (l["lead_id"], " · after hours" if l.get("after_hours") else ""),
        esc(tags), "",
        "<b>Who:</b> %s · %s (decides: %s)" % (esc(l.get("caller_name") or "—"), esc(l.get("phone")), esc(l.get("decision_maker"))),
        "<b>Where:</b> %s" % esc(l.get("locality")),
        "<b>What:</b> %s%s, %s" % (esc(l.get("project_type", "").replace("_", " ")),
                                    ", %s sq ft" % l["carpet_sqft"] if l.get("carpet_sqft") else "", esc(l.get("rooms_scope"))),
        "<b>State:</b> %s" % esc(l.get("current_state")),
        "<b>When:</b> %s" % esc(l.get("timeline")),
        "<b>Source:</b> %s%s" % (esc(l.get("source")), (", " + esc(l["referrer"])) if l.get("referrer") else ""),
    ]
    if l.get("budget_volunteered"):
        lines.append("<b>Budget said:</b> " + esc(l["budget_volunteered"]))
    lines.append("<b>Consult:</b> " + (esc(l["consult_slot"]) if l.get("consult_booked") else
                                       "NOT booked. Call to fix a time. Prefers: " + esc(l.get("preferred_times") or "not said")))
    lines += ["", esc(l.get("handoff_note")), "",
              "<b>Already asked, don't re-ask:</b> " + esc(", ".join(l.get("dont_reask", [])))]
    if l.get("still_unknown"):
        lines.append("<b>Still to find out:</b> " + esc(", ".join(l["still_unknown"])))
    return "\n".join(x for x in lines if x is not None)


# ------------------------------------------------------------------ Telegram

STAGES = {
    "new": [[("✅ Accept", "acc"), ("↪️ Reassign", "rea")]],
    "accepted": [[("📞 Called client", "cal")]],
    "called": [[("🤝 Consult done", "cdn"), ("🚫 No-show", "nsh")]],
    "consulted": [[("🏆 Won", "won"), ("❌ Lost", "lst")]],
    "lost": [[("Budget", "lr:budget"), ("Chose another studio", "lr:competitor")],
             [("Timing", "lr:timing"), ("Stopped responding", "lr:ghosted")], [("Other", "lr:other")]],
    "closed": [],
}


def keyboard(stage, lead_id):
    return {"inline_keyboard": [[{"text": t, "callback_data": "%s|%s" % (a, lead_id)} for t, a in row] for row in STAGES[stage]]}


def esc(s):
    return str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg(method, payload):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (token, method),
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.URLError as e:
        print("telegram", method, e)
        return None


def tg_send(chat_id, html, markup=None):
    if not chat_id:
        return None
    payload = {"chat_id": chat_id, "text": html[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    return tg("sendMessage", payload)


def apply_action(lead_id, act, who="designer"):
    """One outcome step. Shared by Telegram taps and the dashboard's buttons. Returns (note, next_stage)."""
    lead = get_lead(lead_id)
    if not lead:
        return "Lead not found", None
    t = now_ist().isoformat()
    events = lead.get("events", [])
    if act == "acc":
        fields, note, stage = {"accepted_at": t}, "Accepted by " + who, "accepted"
    elif act == "cal":
        fields, note, stage = {"called_at": t}, "Client called", "called"
    elif act == "cdn":
        fields, note, stage = {"consult_done_at": t}, "Consult done", "consulted"
    elif act == "nsh":
        fields, note, stage = {"outcome": "no_show"}, "Marked no-show", "closed"
    elif act == "won":
        fields, note, stage = {"outcome": "won", "referral_text": referral_text(lead)}, "Won", "closed"
    elif act == "ref":
        fields, note, stage = {"referral_asked_at": t}, "Referral ask sent", None
    elif act == "lst":
        return "Why was it lost?", "lost"
    elif act.startswith("lr:"):
        fields, note, stage = {"outcome": "lost", "lost_reason": act[3:]}, "Lost (%s)" % act[3:], "closed"
    elif act.startswith("val:"):
        try:
            value = int(float(act[4:]) * 100000)
        except ValueError:
            return "Not a number", None
        fields, note, stage = {"project_value_inr": value}, "Project value ₹%s lakh" % act[4:], None
    elif act == "rea":
        pool = [d for d in match_designers(lead.get("locality"), lead.get("project_type")) if d["name"] != lead.get("designer")]
        if not pool:
            return "No other designer for this area", None
        nxt = pool[0]
        fields = {"designer": nxt["name"], "accepted_at": "", "handed_off_at": t}
        note, stage = "Reassigned to " + nxt["name"], "closed"
        lead.update(fields)
        tg_send(nxt.get("chat_id"), "↪️ <b>Reassigned to you</b> from %s\n\n%s" % (esc(who), handoff_text(lead)), keyboard("new", lead_id))
    else:
        return "Unknown action", None
    events.append({"at": t, "what": note, "by": who})
    fields["events"] = events
    update_lead(lead_id, **fields)
    return note, stage


def referral_text(l):
    """The message that turns a won project into the next call."""
    first = (l.get("caller_name") or "").split(" ")[0] or "there"
    home = ("your %s home" % l["locality"]) if l.get("locality") else "your home"
    return ("Hi %s, it's %s from Aangan Studio. We're so glad to be designing %s with you! "
            "If a friend, neighbour or colleague is planning their interiors, we'd love an introduction. "
            "They can call the studio any time, even late at night, and someone will always pick up."
            % (first, l.get("designer") or "the team", home))


def handle_telegram(update):
    if not store.setnx("tg:%s" % update.get("update_id"), "1", 86400):
        return  # Telegram retried; act once
    q = update.get("callback_query")
    if q:
        act, _, lead_id = str(q.get("data", "")).partition("|")
        who = (q.get("from") or {}).get("first_name") or "designer"
        note, stage = apply_action(lead_id, act, who)
        chat, msg_id = q["message"]["chat"]["id"], q["message"]["message_id"]
        tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": note})
        if stage:
            tg("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg_id, "reply_markup": keyboard(stage, lead_id)})
        if act == "won":
            store.set("await_value:%s" % chat, lead_id, 3600)
            tg_send(chat, "🎉 Reply with the project value in ₹ lakh (e.g. 11.5) for %s." % lead_id)
            won = get_lead(lead_id) or {}
            tg_send(chat, "💬 <b>Referral ask, ready to send to the client:</b>\n\n" + esc(won.get("referral_text", "")),
                    {"inline_keyboard": [[{"text": "✅ Sent it", "callback_data": "ref|" + lead_id}]]})
        elif act != "lst":
            tg_send(chat, "✔︎ %s: %s" % (lead_id, esc(note)))
        return
    m = update.get("message") or {}
    text, chat = m.get("text", ""), (m.get("chat") or {}).get("id")
    if not chat:
        return
    if text.startswith("/start") or text.startswith("/id"):
        tg_send(chat, "Aangan Voice Desk is connected. Your chat ID is <code>%s</code>." % chat)
        return
    waiting = store.get("await_value:%s" % chat)
    num = re.sub(r"[^\d.]", "", text)
    if waiting and num:
        note, _ = apply_action(waiting, "val:" + num, (m.get("from") or {}).get("first_name") or "designer")
        store.set("await_value:%s" % chat, "", 1)
        tg_send(chat, "Saved. " + esc(note))


# ------------------------------------------------------------- timers + stats

def sla_sweep():
    """Qualified leads nobody accepted within SLA minutes of studio time -> Nikhil."""
    limit = int(env("SLA_MINUTES", "60"))
    hit = []
    for l in all_leads():
        if l.get("verdict") != "qualified" or not l.get("handed_off_at") or l.get("accepted_at") or l.get("sla_escalated"):
            continue
        start = next_office_time(parse_dt(l["handed_off_at"]))
        if (now_ist() - start).total_seconds() / 60 >= limit:
            tg_send(env("NIKHIL_CHAT_ID") or env("TELEGRAM_CHAT_ID"),
                    "⏰ <b>No designer has picked up</b> %s after %d min.\n%s · %s\nAssigned: %s"
                    % (l["lead_id"], limit, esc(l.get("caller_name")), esc(l.get("summary")), esc(l.get("designer") or "nobody")),
                    keyboard("new", l["lead_id"]))
            update_lead(l["lead_id"], sla_escalated=now_ist().isoformat())
            hit.append(l["lead_id"])
    return hit


def daily_job():
    swept = sla_sweep()
    today = now_ist().strftime("%Y-%m-%d")
    due = [l for l in all_leads() if l.get("verdict") == "nurture" and l.get("nurture_date")
           and l["nurture_date"] <= today and not l.get("nurture_done")]
    if due:
        tg_send(env("FRONTDESK_CHAT_ID") or env("TELEGRAM_CHAT_ID"), "🗓 <b>Follow-ups due today</b>\n" + "\n".join(
            "• %s · %s: %s" % (esc(l.get("caller_name") or "—"), esc(l.get("phone")), esc(l.get("summary"))) for l in due))
        for l in due:
            update_lead(l["lead_id"], nurture_done=today)
    s = stats()
    tg_send(env("NIKHIL_CHAT_ID") or env("TELEGRAM_CHAT_ID"),
            "📊 <b>Aangan calls so far</b>\nCalls: %d (%d after hours)\nQualified: %d · Booked on call: %d · Won: %d\n"
            "Asked about price: %d · Price spoken by assistant: %d\nTotal cost: ₹%d · per qualified lead: ₹%d"
            % (s["volume"]["calls"], s["volume"]["after_hours"], s["funnel"]["qualified"], s["funnel"]["booked"],
               s["funnel"]["won"], s["signals"]["asked_price"], s["signals"]["price_spoken_by_agent"],
               s["cost"]["total"], s["cost"]["per_qualified"]))
    return {"sla_escalated": swept, "nurture_reminders": len(due)}


def stats():
    leads = all_leads()
    count = lambda f: sum(1 for l in leads if f(l))
    has = lambda k: (lambda l: bool(l.get(k)))
    flag = lambda f: (lambda l: f in (l.get("flags") or []))
    reason = lambda r: (lambda l: r in (l.get("fail_reasons") or []))
    pickups = sorted((parse_dt(l["accepted_at"]) - parse_dt(l["handed_off_at"])).total_seconds() / 60
                     for l in leads if l.get("accepted_at") and l.get("handed_off_at"))
    voice = sum(float(l.get("call_cost_inr") or 0) for l in leads)
    ai = sum(float(l.get("ai_cost_inr") or 0) for l in leads)
    fixed = float(env("FIXED_MONTHLY_INR", "1500"))
    total = voice + ai + fixed
    q, booked, won = count(lambda l: l.get("verdict") == "qualified"), count(has("consult_booked")), count(lambda l: l.get("outcome") == "won")
    won_value = sum(int(l.get("project_value_inr") or 0) for l in leads)
    avg_value = int(env("AVG_PROJECT_VALUE_INR", "1100000"))
    close_rate = float(env("CLOSE_RATE", "0.25"))
    after_q = count(lambda l: l.get("after_hours") and l.get("verdict") == "qualified")
    per = lambda n: round(total / n) if n else 0
    return {
        "storage": "redis" if store.persistent else "memory (resets on restart — add Upstash Redis in Vercel)",
        "telegram": bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID")),
        "volume": {"calls": len(leads), "after_hours": count(has("after_hours")), "simulated": count(has("simulated"))},
        "verdicts": {v: count(lambda l, v=v: l.get("verdict") == v) for v in ["qualified", "not_fit", "nurture", "escalation", "incomplete"]},
        "funnel": {"qualified": q, "booked": booked, "accepted": count(has("accepted_at")), "called": count(has("called_at")),
                   "consult_done": count(has("consult_done_at")), "won": won, "lost": count(lambda l: l.get("outcome") == "lost"),
                   "won_value": won_value, "pipeline_value": booked * int(env("AVG_PROJECT_VALUE_INR", "1100000"))},
        "flywheel": {"calls": len(leads), "qualified": q, "booked": booked, "briefed": count(has("handed_off_at")) or q,
                     "accepted": count(has("accepted_at")), "consult_done": count(has("consult_done_at")), "won": won,
                     "referral_asked": count(has("referral_asked_at")),
                     "referral_calls": count(lambda l: l.get("source") == "referral")},
        "rescued": {"after_hours_calls": count(has("after_hours")), "after_hours_qualified": after_q,
                    "pipeline_value": after_q * avg_value, "expected_value": round(after_q * avg_value * close_rate),
                    "avg_project_value": avg_value, "close_rate": close_rate},
        "speed": {"median_pickup_min": round(pickups[len(pickups) // 2], 1) if pickups else None,
                  "sla_misses": count(has("sla_escalated"))},
        "not_fit_reasons": {r: count(reason(r)) for r in ["outside_area", "advice_only", "timeline_too_short", "budget_misaligned",
                                                          "out_of_scope_type", "below_minimum_size", "not_decision_maker"]},
        "signals": {"asked_price": count(flag("asked_price")), "price_spoken_by_agent": count(flag("price_spoken_by_agent")),
                    "repeat_caller": count(flag("repeat_caller")), "edge_locality": count(flag("edge_locality")),
                    "vip_referral": count(flag("vip_referral"))},
        "cost": {"voice": round(voice), "ai": round(ai, 2), "fixed_monthly": round(fixed), "total": round(total),
                 "per_call": per(len(leads)), "per_qualified": per(q), "per_booked": per(booked), "per_won": per(won),
                 "return_multiple": round(won_value / total, 1) if total and won_value else None},
    }


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence: the single most important thing this week"},
        "insights": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "evidence": {"type": "string"}, "action": {"type": "string"}},
            "required": ["title", "evidence", "action"], "additionalProperties": False}},
    },
    "required": ["headline", "insights"],
    "additionalProperties": False,
}

BRIEF_SYSTEM = """You are the founder's-office analyst for Aangan Studio, an interior design studio in Pune.
You write Nikhil's Monday brief about the AI phone desk. Read the numbers and the recent call summaries.
Give exactly 3 insights, each a specific change that would turn more calls into won projects or close the loop faster
(e.g. edge localities to add or exclude, a script change, designer pick-up speed, referral asks, pricing-question handling).
Rules: cite the actual numbers in evidence; never invent data that is not given; never suggest quoting prices on calls
(that is a deliberate rule). If there is too little data, say what to watch for. Plain, direct sentences."""


def monday_brief(refresh=False):
    cached = store.get("brief:latest")
    if cached and not refresh:
        return 200, json.loads(cached)
    if not ai_provider():
        return 400, {"error": "No AI key set. Add GEMINI_API_KEY in Vercel."}
    if store.incr("brief:" + now_ist().strftime("%Y%m%d"), 172800) > 20:
        return 429, {"error": "Brief limit for today reached."}
    s = stats()
    recent = [{"verdict": l.get("verdict"), "summary": l.get("summary"), "locality": l.get("locality"),
               "flags": l.get("flags"), "fail_reasons": l.get("fail_reasons"), "outcome": l.get("outcome"),
               "after_hours": l.get("after_hours")} for l in all_leads(40)]
    prompt = "Numbers:\n%s\n\nRecent calls (newest first):\n%s" % (json.dumps(s, default=str), json.dumps(recent, default=str))
    if ai_provider() == "gemini":
        data, usd = gemini_json(BRIEF_SYSTEM, prompt + "\n\nReply with ONE JSON object matching this JSON Schema:\n" + json.dumps(BRIEF_SCHEMA))
    else:
        import anthropic
        r = anthropic.Anthropic().beta.messages.create(
            model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            system=BRIEF_SYSTEM, messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": BRIEF_SCHEMA}})
        if r.stop_reason == "refusal":
            return 500, {"error": "The AI declined to write the brief."}
        data = json.loads(next(b.text for b in r.content if b.type == "text"))
        usd = (r.usage.input_tokens * 4 + r.usage.output_tokens * 20) / 1e6
    items = [i for i in (data.get("insights") or []) if isinstance(i, dict)][:3]
    out = {"headline": str(data.get("headline", "")),
           "insights": [{k: str(i.get(k, "")) for k in ("title", "evidence", "action")} for i in items],
           "based_on_calls": s["volume"]["calls"], "written_at": now_ist().isoformat(),
           "cost_inr": round(usd * float(env("USD_INR", "84")), 2)}
    store.set("brief:latest", json.dumps(out), 600)
    return 200, out


def public_lead(l):
    out = {k: v for k, v in l.items() if k not in ("transcript",)}
    out["phone"] = mask_phone(l.get("phone"))
    out["channel"] = "replay" if l.get("simulated") else "phone"
    if out.get("handoff_text"):
        out["handoff_text"] = out["handoff_text"].replace(esc(l.get("phone")), out["phone"])
    return out


def simulate(body):
    day = now_ist().strftime("%Y%m%d")
    if store.incr("sim:" + day, 172800) > SIMULATIONS_PER_DAY:
        return 429, {"error": "Daily demo limit reached. Try again tomorrow."}
    test_id = str(body.get("test_id", "")).upper()
    if test_id:
        tc = next((t for t in load_test_calls() if t["id"] == test_id), None)
        if not tc:
            return 404, {"error": "No test call " + test_id}
        transcript, phone = tc["transcript"], "+91 98000 000%s" % test_id[-2:]
    else:
        transcript, phone = str(body.get("transcript", ""))[:20000], str(body.get("phone") or "+91 98000 00099")
    missed = "MISSED CALL" in transcript  # a real missed call arrives with no transcript at all
    payload = {"call_id": "sim-" + new_id("C"), "from": phone, "started_at": now_ist().isoformat(),
               "duration": 0 if missed else 300, "transcript": "" if missed else transcript, "simulated": True}
    lead = process_call(payload, notify=True)
    if lead.get("verdict") == "qualified" and not lead.get("consult_booked"):
        # On a real call Vaani books mid-call via get_slots/book_consult; the replay does the same step here.
        kind = "commercial" if "commercial" in (lead.get("project_type") or "") else "residential"
        slots = get_slots(lead.get("locality", ""), kind)["slots"]
        if slots:
            b = book_consult({"slot_id": slots[0]["slot_id"], "caller_name": lead.get("caller_name", ""),
                              "phone": lead.get("phone", ""), "mode": "site_visit", "locality": lead.get("locality", "")})
            if b.get("booked"):
                lead = update_lead(lead["lead_id"], consult_booked=True, designer=b["designer"],
                                   consult_slot="%s · site visit" % b["when"]) or lead
                lead["handoff_text"] = handoff_text(lead)
                update_lead(lead["lead_id"], handoff_text=lead["handoff_text"])
    result = public_lead(lead)
    result["transcript"] = lead.get("transcript", "")
    if test_id:
        result["expected"] = tc["expected"]
        result["matches_expected"] = lead.get("verdict") == tc["expected"]
    return 200, result


# ---------------------------------------------------------------------- HTTP

def unwrap_args(b):
    for c in (b.get("args"), b.get("arguments"), b.get("parameters"), b.get("params"), b.get("input"),
              (b.get("function") or {}).get("arguments") if isinstance(b.get("function"), dict) else None):
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except ValueError:
                c = None
        if isinstance(c, dict):
            return c
    return b


def authorised(environ, query):
    """Open when WEBHOOK_SECRET isn't set (no-password setup); a daily cap on calls limits misuse instead."""
    secret = env("WEBHOOK_SECRET")
    if not secret:
        return True
    given = query.get("key") or environ.get("HTTP_X_WEBHOOK_SECRET", "")
    return given == secret


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    query = {k: v[0] for k, v in urllib.parse.parse_qs(environ.get("QUERY_STRING", "")).items()}
    path = "/api/" + query["route"] if query.get("route") else environ.get("PATH_INFO", "/")
    if path.rstrip("/") == "/api/index":
        # Vercel rewrites /api/* to the function file; recover the address the caller used if a header carries it.
        for h in ("HTTP_X_VERCEL_ORIGINAL_PATH", "HTTP_X_MATCHED_PATH", "HTTP_X_FORWARDED_URI", "HTTP_X_ORIGINAL_URL",
                  "REQUEST_URI", "RAW_URI"):
            v = environ.get(h, "").split("?")[0]
            if v and v.rstrip("/") != "/api/index":
                path = v
                break
    if path.startswith("/desk/"):  # public alias used by the dashboard and Vaani: /desk/x == /api/x
        path = "/api/" + path[len("/desk/"):]
    path = path.rstrip("/") or "/"
    body = {}
    if method == "POST":
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(size) if size else b""
            body = json.loads(raw.decode() or "{}")
        except (ValueError, KeyError):
            body = {}
        if not isinstance(body, dict):
            body = {}
    try:
        status, payload = dispatch(method, path, query, body, environ)
    except Exception as e:  # keep the voice agent talking even if something breaks
        print("error", path, repr(e))
        status, payload = 500, {"error": str(e)[:300],
                                "result": "Sorry, I couldn't do that just now. A designer will call to confirm."}
    if isinstance(payload, tuple):  # (bytes, content_type) for pages and images
        data, ctype = payload
    else:
        data, ctype = json.dumps(payload, default=str).encode(), "application/json"
    start_response("%d %s" % (status, {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                                       429: "Too Many Requests", 500: "Internal Server Error"}.get(status, "OK")),
                   [("Content-Type", ctype), ("Cache-Control", "no-store"), ("Content-Length", str(len(data)))])
    return [data]


PAGES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/components-map.html": ("components-map.html", "text/html; charset=utf-8"),
         "/components-map.png": ("components-map.png", "image/png")}


def static_page(path):
    """Serve the dashboard from the function too, wherever the files sit in the repo (root, public/ or docs/)."""
    name, ctype = PAGES[path]
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.path.join(here, ".."), os.getcwd()):
        for sub in ("", "public", "docs"):
            p = os.path.join(base, sub, name)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return 200, (f.read(), ctype)
    return 404, {"error": "page file not found", "file": name}


def dispatch(method, path, query, body, environ):
    if path in PAGES and method == "GET":
        return static_page(path)
    if path == "/api/health":
        return 200, {"ok": True, "ai": ai_provider() or "none",
                     "model": MODEL if ai_provider() == "claude" else (env("GEMINI_MODEL") or GEMINI_MODELS[0]),
                     "storage": "redis" if store.persistent else "memory",
                     "telegram": bool(env("TELEGRAM_BOT_TOKEN")), "claude_key": bool(ai_provider())}

    if path.startswith("/api/vaani/"):
        if not authorised(environ, query):
            return 401, {"error": "unauthorised"}
        fn = path.rsplit("/", 1)[1]
        store.lpush("raw", json.dumps({"at": now_ist().isoformat(), "fn": fn, "query": {k: v for k, v in query.items() if k != "key"},
                                       "body": body}, default=str)[:12000])
        store.ltrim("raw", 0, 19)
        a = unwrap_args(body)
        if fn == "check_caller":
            return 200, check_caller(a.get("phone") or pick(body, "from", "caller", "call.from") or "")
        if fn == "get_slots":
            return 200, get_slots(a.get("locality", ""), a.get("project_type", "residential"))
        if fn == "book_consult":
            return 200, book_consult(a)
        if fn == "escalate":
            return 200, escalate(a)
        if fn == "call_ended":
            if not env("WEBHOOK_SECRET") and store.incr("calls:" + now_ist().strftime("%Y%m%d"), 172800) > int(env("CALLS_PER_DAY", "150")):
                return 429, {"error": "Daily call limit reached"}
            lead = process_call(body)
            return 200, {"ok": True, "lead_id": lead["lead_id"], "verdict": lead.get("verdict")}
        return 404, {"error": "unknown function " + fn}

    if path == "/api/raw" and method == "GET":
        if not authorised(environ, query):
            return 401, {"error": "unauthorised"}
        def hide(txt):  # mask phone numbers (10+ digits), leave dates and times alone
            def m(x):
                d = re.sub(r"\D", "", x.group())
                return "••••" + d[-4:] if len(d) >= 10 and not re.match(r"\d{4}-\d{2}-\d{2}", x.group()) else x.group()
            return re.sub(r"\+?\d[\d\s-]{8,}\d", m, txt)
        return 200, {"recent_vaani_requests": [json.loads(hide(x)) for x in store.lrange("raw", 0, 19)]}
    if path == "/api/stats" and method == "GET":
        sla_sweep()
        return 200, stats()
    if path == "/api/leads" and method == "GET":
        return 200, {"leads": [public_lead(l) for l in all_leads(100)]}
    if path == "/api/test-calls" and method == "GET":
        return 200, {"calls": [{k: t[k] for k in ("id", "title", "call_date", "expected", "expected_reason", "transcript")} for t in load_test_calls()]}
    if path == "/api/brief" and method == "POST":
        return monday_brief(bool(body.get("refresh")))
    if path == "/api/simulate" and method == "POST":
        return simulate(body)
    m = re.match(r"^/api/leads/([A-Za-z0-9-]+)/action$", path)
    if m and method == "POST":
        note, stage = apply_action(m.group(1), str(body.get("action", "")), "dashboard")
        return 200, {"note": note, "next_stage": stage, "lead": public_lead(get_lead(m.group(1)) or {})}

    if path == "/api/telegram" and method == "POST":
        if not authorised(environ, query):
            return 401, {"error": "unauthorised"}
        handle_telegram(body)
        return 200, {"ok": True}
    if path == "/api/setup-telegram" and method == "POST":
        if not authorised(environ, query):
            return 401, {"error": "unauthorised"}
        host = environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST")
        url = "https://%s/api/telegram?key=%s" % (host, urllib.parse.quote(env("WEBHOOK_SECRET")))
        return 200, {"setWebhook": tg("setWebhook", {"url": url, "allowed_updates": ["message", "callback_query"]})}
    if path == "/api/cron/daily":
        cron = env("CRON_SECRET")
        if cron and environ.get("HTTP_AUTHORIZATION") != "Bearer " + cron and not authorised(environ, query):
            return 401, {"error": "unauthorised"}
        return 200, daily_job()

    return 404, {"error": "not found", "path": path}
