"""
Runs the whole backend locally with a stand-in for Claude (no API key, no network).
    python3 tests/test_local.py
"""
import io
import json
import os
import sys

os.environ.update({"WEBHOOK_SECRET": "test-secret", "TELEGRAM_BOT_TOKEN": "", "KV_REST_API_URL": ""})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import index  # noqa: E402


def fake_extract(transcript, phone, started_at):
    t = transcript.lower()
    verdict, reasons, flags = "qualified", [], []
    if "my designer" in t or "project has been going" in t:
        verdict = "escalation"
    elif "nashik" in t:
        verdict, reasons = "not_fit", ["outside_area"]
    elif "diwali" in t:
        verdict, reasons = "nurture", ["timeline_too_short"]
    if "how much" in t or "cost" in t:
        flags.append("asked_price")
    return ({"caller_name": "Test Caller", "call_type": "existing_client" if verdict == "escalation" else "new_enquiry",
             "source": "referral", "referrer": "", "locality": "Kothrud", "in_service_area": "yes",
             "project_type": "residential_full", "carpet_sqft": 1400, "rooms_scope": "kitchen, living, 2 bedrooms",
             "current_state": "builder finish", "timeline": "by March 2027", "target_date": "2027-03",
             "decision_maker": "yes", "budget_volunteered": "",
             "criteria": {k: "pass" for k in ["c1_real_project", "c2_service_area", "c3_timeline", "c4_budget", "c5_decision_maker"]},
             "verdict": verdict, "fail_reasons": reasons, "flags": flags, "nurture_date": "2026-11-10" if verdict == "nurture" else "",
             "preferred_times": "", "summary": "Test summary", "handoff_note": "Test note.", "dont_reask": ["size", "rooms"],
             "still_unknown": ["style"]}, 3.2)


index.EXTRACTOR["fn"] = fake_extract


def call(method, path, body=None, headers=None):
    path, _, qs = path.partition("?")
    raw = json.dumps(body or {}).encode()
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": qs, "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw), "HTTP_HOST": "test.local"}
    environ.update(headers or {})
    got = {}
    out = index.app(environ, lambda status, h: got.update(status=int(status.split()[0])))
    return got["status"], json.loads(b"".join(out))


failures = 0


def check(name, ok, detail=""):
    global failures
    failures += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + str(detail)[:300]))


K = "?key=test-secret"

s, r = call("GET", "/api/health")
check("health", s == 200 and r["ok"], r)

s, r = call("POST", "/api/vaani/check_caller", {"phone": "1"})
check("vaani needs secret", s == 401, s)

s, r = call("GET", "/api/test-calls")
check("20 test calls load", s == 200 and len(r["calls"]) == 20, r)

s, r = call("POST", "/api/simulate", {"test_id": "T01"})
check("T01 simulated -> qualified with designer + handoff card", s == 200 and r["verdict"] == "qualified" and r["designer"] and "Kothrud" in r["handoff_text"], r)
check("phone masked in output", "98000" not in json.dumps(r), r.get("phone"))
lead_t01 = r["lead_id"]

s, r = call("POST", "/api/simulate", {"test_id": "T08"})
check("T08 missed call -> incomplete without AI", r["verdict"] == "incomplete" and r["ai_cost_inr"] == 0, r)

s, r = call("POST", "/api/vaani/check_caller" + K, {"phone": "+91 98000 00009"})
check("T09 number recognised as existing client", r["status"] == "existing_client", r)

s, r = call("POST", "/api/vaani/check_caller" + K, {"arguments": json.dumps({"phone": "+91 98000 00001"})})
check("repeat caller recognised (string-wrapped args)", r["status"] == "repeat_enquiry", r)

s, r = call("POST", "/api/vaani/get_slots" + K, {"locality": "Kothrud", "project_type": "residential"})
check("get_slots returns 3", len(r["slots"]) == 3, r)
slot = r["slots"][0]["slot_id"]

s, r = call("POST", "/api/vaani/book_consult" + K, {"slot_id": slot, "caller_name": "Priya", "phone": "+91 99999 11111", "mode": "site_visit"})
check("book_consult books", r["booked"], r)
s, r2 = call("POST", "/api/vaani/book_consult" + K, {"slot_id": slot, "caller_name": "X", "phone": "1", "mode": "studio"})
check("double booking refused", not r2["booked"], r2)
s, r3 = call("POST", "/api/vaani/get_slots" + K, {"locality": "Kothrud", "project_type": "residential"})
check("booked slot no longer offered", slot not in [x["slot_id"] for x in r3["slots"]], r3)

s, r = call("POST", "/api/vaani/call_ended" + K, {
    "call": {"id": "vaani-1", "from": "+91 99999 11111", "duration": 240},
    "messages": [{"role": "assistant", "content": "Namaste, Aangan Studio."},
                 {"role": "user", "content": "Hi, I want my 3BHK in Kothrud redone, kitchen and bedrooms, by March."},
                 {"role": "assistant", "content": "Lovely. I have booked you for a site visit."}]})
check("end-of-call webhook -> lead", s == 200 and r["verdict"] == "qualified", r)
lead = index.get_lead(r["lead_id"])
check("booking made mid-call is attached to the lead", lead["consult_booked"] and "site visit" in lead["consult_slot"], lead)
check("voice cost from minutes", lead["call_cost_inr"] == 32.0, lead["call_cost_inr"])

s, r = call("POST", "/api/simulate", {"test_id": "T09"})
check("T09 -> escalation", r["verdict"] == "escalation", r)
s, r = call("POST", "/api/simulate", {"test_id": "T07"})
check("T07 -> nurture with follow-up date", r["verdict"] == "nurture" and r["nurture_date"], r)
s, r = call("POST", "/api/simulate", {"test_id": "T03"})
check("T03 -> not_fit outside_area", r["verdict"] == "not_fit" and "outside_area" in r["fail_reasons"], r)

for act in ["acc", "cal", "cdn", "won", "val:11.5"]:
    s, r = call("POST", "/api/leads/%s/action" % lead_t01, {"action": act})
check("outcome taps walk the funnel", r["lead"]["outcome"] == "won" and r["lead"]["project_value_inr"] == 1150000, r)

index.handle_telegram({"update_id": 7, "callback_query": {"id": "q", "data": "acc|" + lead["lead_id"], "from": {"first_name": "Aryan"},
                                                           "message": {"chat": {"id": 1}, "message_id": 2}}})
index.handle_telegram({"update_id": 7, "callback_query": {"id": "q", "data": "rea|" + lead["lead_id"], "from": {"first_name": "Aryan"},
                                                           "message": {"chat": {"id": 1}, "message_id": 2}}})
l2 = index.get_lead(lead["lead_id"])
check("Telegram tap applied once (retry ignored)", l2["accepted_at"] and len(l2["events"]) == 1, l2.get("events"))

s, r = call("GET", "/api/stats")
check("stats funnel", r["funnel"]["won"] == 1 and r["funnel"]["won_value"] == 1150000 and r["funnel"]["booked"] == 1, r["funnel"])
check("stats cost", r["cost"]["total"] > 1500 and r["cost"]["per_qualified"] > 0 and r["cost"]["return_multiple"], r["cost"])
check("stats signals", r["signals"]["price_spoken_by_agent"] == 0, r["signals"])

check("price guard catches a spoken figure", index.agent_spoke_price("Assistant: it's about 2 lakh for that"))
check("price guard ignores the deflection", not index.agent_spoke_price("Assistant: Pricing depends on the site, the materials you choose, and the scope."))
check("price guard ignores the caller's own budget", not index.agent_spoke_price("User: my budget is 5 lakh"))

d = index.parse_dt("2026-10-07T22:47:00+05:30")
check("SLA clock: 10:47pm Wed starts Thu 10:00", index.next_office_time(d).isoformat() == "2026-10-08T10:00:00+05:30", index.next_office_time(d))
d = index.parse_dt("2026-10-10T20:00:00+05:30")
check("SLA clock: Sat 8pm starts Mon 10:00", index.next_office_time(d).isoformat() == "2026-10-12T10:00:00+05:30", index.next_office_time(d))

check("schema: every property required", sorted(index.LEAD_SCHEMA["required"]) == sorted(index.LEAD_SCHEMA["properties"]))

s, r = call("GET", "/api/cron/daily" + K)
check("daily job runs", s == 200 and "sla_escalated" in r, r)

s, r = call("GET", "/api/nope")
check("unknown route 404", s == 404, s)

print("\n%d failure(s)" % failures)
sys.exit(1 if failures else 0)
