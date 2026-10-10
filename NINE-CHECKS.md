# Nine Checks: Aangan phone enquiry agent

**Verdict: build it, smaller than asked, with humans at four named points.**
All three kill switches pass. One sizing check (ROI) passes only if we measure it, so the dashboard is part of the build. Every boundary check needed a human placed at a specific point.

## ⚡ Kill switches (any No: don't build it)

| # | Check | Answer | Evidence from the case |
|---|---|---|---|
| 01 | Problem real? | **Yes** | ~48% of enquiries get no reply in 48 hrs. ~33% arrive outside 10am–7pm. Answering within 1 hr converts 4× better than next day. Real losses: T08 (10:47pm missed call, callback failed), T16 ("someone said they'd get back… two days"), W05 (Saturday enquiry lost), W08 (4 days, "not sure I want to work with a studio that doesn't reply"). |
| 02 | Workflow repeated? | **Yes** | ~200 enquiries/month, about half by phone. Every one of the 20 phone transcripts asks the same questions in the same order: what, where, size, timeline, who decides. Same five-criteria decision every time (`qualified.md`). |
| 03 | Input available? | **Yes, with gaps** | Calls are the input; the rules are written down (`services.md`, `qualified.md`, `pricing.md`). Gaps to close with Nikhil: lead time (6 weeks in `services.md` vs 8–10 in `qualified.md`), a 500 sq ft commercial minimum used in T18 but written nowhere, edge localities (Kharadi T10, Nanded City T08), and no shared designer calendar yet. |

## 📊 Sizing (any No: build something smaller)

| # | Check | Answer | Evidence |
|---|---|---|---|
| 04 | Output valuable? | **Yes** | Two outputs: a booked consult, and a handoff note so "the designer's first call is spent asking questions the front desk already asked" stops happening. Each lead is worth ₹8–14 lakh on average. |
| 05 | Impact measurable? | **Yes** | Before: Nikhil's manual pull (48% unanswered, 4× gap). After: every call is a row. Answer time, qualified rate, consults booked, designer pickup time, won/lost and ₹ come from the designers' Telegram taps. The dashboard is built from those rows, so he never pulls numbers by hand again. |
| 08 | ROI worth it? | **Yes, by a wide margin** | Running cost ≈ ₹7–13k/month (Vaani minutes + Claude + fixed). One extra won project (₹8L+) covers years of it. The dashboard tracks cost per call, per qualified lead, per booked consult and per won project, plus return (won value ÷ cost), so the ROI is checked every month, not assumed. |

**What "smaller" means here:** phone only (WhatsApp and web form come later, on the same engine). No pricing (see The Cut). Booking uses a slot list, not full two-way calendar sync.

## 🚧 Boundary (any No: put a human exactly here)

| # | Check | Answer | Where the human sits |
|---|---|---|---|
| 06 | Failure risk OK? | **Not on its own** | Three failure modes, each with a human catch: **(a)** the AI says a price → a guardrail scans every transcript and alerts Nikhil, and the dashboard counter must stay at 0. **(b)** It turns away a good lead → every "not a fit" is logged with a reason; edge localities go to the front desk instead of being declined. **(c)** An upset existing client (T09) → the AI never handles it; it escalates to a senior person within 15 minutes. |
| 07 | Judgment protected? | **Only if we keep it out** | Budget fit, aesthetic fit (F06 "honest conversation about whether your aesthetic matches ours") and the actual quote stay with the designer at the consultation. The AI never asks about budget and never quotes. Edge-area and scope calls go to Nikhil and the front desk. |
| 09 | Owner clear? | **Yes, named** | **Founder's office (me):** the system, the prompt, the weekly transcript review. **Nikhil:** the rubric and edge cases. **Each designer:** their lead's outcome (Telegram taps); 60-min SLA, then it goes to Nikhil. **Front desk:** missed-call callbacks, follow-up list, area checks. |

## ✂️ The Cut

**Quoting standard per-square-foot pricing on the call.** Nikhil asked for it directly ("the system should be able to quote our standard per-square-foot pricing").

Checks **06** and **07** stop it:
- His own `pricing.md` says no number from the guide may be quoted, and lists the exact sentences the agent must never say.
- The spread is huge: standard ₹1,800/sq ft vs luxury ₹3,500+, and T13: "a premium kitchen alone can be 3× the cost". Any number given on a call becomes an anchor the designer then has to argue against.
- Pricing is the designer's judgment after seeing the site.

**What we do instead:**
- Use the approved deflection line and book the free consultation in the same breath.
- Count how often callers ask, so the decision can be revisited with data.
- Two pricing-only enquiries went nowhere after the deflection (W02, F07), so the cost of the cut is real and we're tracking it.

## Humans in the loop (summary)

1. **Pricing:** never automated; the designer gives the number at the consultation.
2. **Existing-client complaints:** a senior person calls back within 15 minutes.
3. **Unclear areas / unusual scope:** the front desk or Nikhil confirms.
4. **Every qualified lead:** a designer accepts within 60 minutes, or Nikhil is alerted.
