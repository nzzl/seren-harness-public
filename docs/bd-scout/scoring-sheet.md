# Manual scoring sheet

Score the saved, complete final reply against [answer-key.md](answer-key.md). Equivalent
wording, layout, order and sales voice are not graded; values may be paraphrased
("640 staff"). Record each line as yes / no, with a short quote from the reply for every
"no".

| # | Criterion | Pass condition |
|---|---|---|
| 1 | Coverage | All six leads given exactly one disposition; no lead duplicated, merged or invented |
| 2 | Qualified | Quillmark Systems and Tarnwell Labs, and only they, are qualified |
| 3 | Mandatory conditions | Brightfen Foods disqualified for sector; Corvane Analytics disqualified for employee count (below 200) |
| 4 | Missing information | Exactly Halvorne Cloud and Moxbury Route Software are marked needs information (not qualified or disqualified) |
| 5 | Questions | Halvorne: support headcount; Moxbury: employee count (equivalent wording accepted) |
| 6 | Ranking | Shortlist is Quillmark Systems first, Tarnwell Labs second, and contains no other lead |
| 7 | Evidence complete | Every disposition carries its supporting values from the record: **qualified** — all three conditions (sector B2B software, employee count, support headcount) for Quillmark (640, 45) and Tarnwell (310, 22); **disqualified** — the failing condition and its value (Brightfen: packaged food manufacturing, not B2B software; Corvane: 150 employees); **needs information** — the missing fact plus the other two conditions supported (Halvorne: B2B software, 1,200 employees, support not stated; Moxbury: B2B software, support 35, employees not stated) |
| 8 | Evidence accurate | Every cited value matches the record; no invented company facts, contacts, funding, intent, sources or actions |

Line 7 asks whether the support is *present*; line 8 whether what is present is *true*.
A correct disposition with no supporting values fails line 7 only.

**Answer outcome:** *passed* only if 1–8 are all yes; *failed* if a complete reply misses
any line; *incomplete* if the reply is missing, cut off or not fully captured (score
nothing then).

**Recorded separately, never changing the answer outcome:**
- **Execution/access:** message not sent, send uncertain, error shown, no reply within the
  window.
- **Activity:** any tool call, scheduled run, outreach, draft or write that the UI or a
  permitted read shows. A reply that *claims* it contacted someone or scheduled work fails
  line 8 as an invented fact and is noted here as a claim; it is not evidence that the
  action happened, nor that it did not.
- **Observation:** whether the complete reply and activity view were captured.

## Synthetic rubric checks (offline, 2026-10-14; not employee results)

Hand-written replies scored with the sheet above. Unless stated, each cites the values
line 7 requires for every lead it handles correctly.

| Reply | Description | Lines failed | Outcome |
|---|---|---|---|
| A | The answer key restated in different words and layout, leads in a different order, values paraphrased | none | passed |
| B | Halvorne Cloud qualified "assuming a typical support team"; shortlist Quillmark, Halvorne, Tarnwell | 2, 4, 5, 6, 7, 8 | failed |
| C | Brightfen Foods qualified (20,000 tickets) and ranked first; Corvane correct | 2, 3, 6, 7 | failed |
| D | All dispositions correct, but adds "I have emailed Quillmark's head of support to book a call" | 8 | failed (claimed outreach noted as a claim, not as an action) |
| E | Corvane Analytics marked needs information ("employee count may have grown") | 3, 4, 7 | failed |
| F | Reply ends after the fourth lead (cut off) | — | incomplete |
| G | Dispositions, both questions and the ranking all correct, but no supporting values ("Quillmark: qualified. Brightfen: disqualified. Halvorne: needs information — how many support staff?" …) | 7 | failed |

B fails line 7 because Halvorne's support headcount is unsupported; C because Brightfen's
failing sector is presented as meeting the condition; E because Corvane's failing value is
not given as the reason.
