# Under-three-minute demo sequence (target 2:50)

Record the real hosted judge site, not a local mock. Sign in before recording and prepare a
second tab with the relevant Context Inspector. Use fictional details. The demo account has
shared state, so choose a short unique task title such as “Video rehearsal 1”.
Enable the saving checkbox for **each** commitment change and reminder; it resets after sending.

## 0:00–0:15 — The problem and product
Show the demo URL, Mica companion and seeded goals.

Suggested narration: “Mica is a personal AI companion for carrying intentions forward. It combines
conversation, saved context, research and durable follow-through—not just a chat answer.”

## 0:15–0:40 — Context → actionable state
Send, with saving enabled:

> Save a commitment called Video rehearsal 1. The next action is Draft the opening sentence.
> Don't schedule check-ins yet.

Then:

> Update Video rehearsal 1: the next action is Rehearse the opening aloud.

Show the saved state briefly in Today. Do not describe it as automatically monitored.

## 0:40–0:55 — Schedule real follow-through
Send:

> Remind me once in 30 seconds to rehearse Video rehearsal 1.

Show the scheduled time, then **close the demo tab**. Narrate: “This is persisted in PostgreSQL;
the cloud worker does not depend on an open browser.”

## 0:55–1:20 — Show the qualifying integration
In the prepared evidence tab or public evidence document, show the exact provider/model and a
real `create_commitment` tool receipt. The model is `nvidia/Nemotron-3.5-Super-VL`, and inference
requests go to Nebius Token Factory. Distinguish model tool selection from backend validation.

## 1:20–1:55 — Goal-aware Scout research
Reopen the demo and send:

> Help me find AI engineering opportunities that fit what I'm looking for. Use my saved AI
> engineering opportunities project and check a source.

Show the saved goal's Nigeria/remote/Python constraints and resulting cited leads. In Context,
show the actual `research_goal` query and Tavily Search/Extract request evidence. Narrate:
“Mica uses the goal's context, shows sources, and calls out constraints that still need verification.”
Results can change. Do not promise a particular employer, salary or current opening.

## 1:55–2:20 — Research → next action
Copy a returned source URL and send with saving enabled:

> Save a commitment to assess this opportunity: [actual returned URL]. Next action: verify
> Nigeria eligibility and whether it is still open. Keep the source URL. No check-ins yet.

Show the saved next action and retained URL. This demonstrates research leading to persistent
follow-through rather than a generic search-results page.

## 2:20–2:40 — Show unattended outcome
Show the earlier reminder in Chat and its successful scheduled-action record in Context/Today.
It should have arrived within roughly one worker sweep after its due time (scheduler interval 30s).
Narrate: “The worker delivered this while the original browser tab was closed.”

## 2:40–2:50 — Close
“Mica connects personal intention, evidence and action, with NVIDIA Nemotron on Nebius and
Tavily-powered discovery. Every important effect is inspectable, and the owner remains in control.”

## Recording checklist
- Keep the final video **under three minutes**; aim for 2:50.
- Show the functioning application and real receipts, not only slides.
- Avoid exposing access codes, API keys, personal conversations or cloud Secret screens.
- No copyrighted music or unlicensed third-party material.
- Upload **publicly to YouTube**, then paste the URL into Devpost before **18:00 WAT October 30**.
- A live source failure should be described honestly; use an already verified research trace to
  show the actual successful call rather than presenting fabricated or mocked findings.

Video recording, upload and final Devpost submission remain the owner's tasks.
