# Judge testing instructions

**URL:** https://nini-adegboyega--mica-demo.us-east.modal.run

**Login:** enter the access code provided in the Devpost testing instructions. The shared
synthetic account is `owner`; the web form only asks for the code. No registration, payment,
Telegram account or personal API key is needed.

The environment is configured to remain accessible until **December 31, 2026 at 09:54:34 UTC**,
beyond the official judging end of December 23 at 20:00 UTC. It is cloud-hosted and independent
of the developer's laptop. Cold starts may take a few seconds; a model response commonly takes
several seconds. Existing records may include earlier judge tests because this is a shared account.

## A five-minute product test

For **each** save, update or reminder message, enable the saving checkbox again; it resets after
every send. Research itself works with saving unchecked.

1. **Context:** ask “What are my current goals?” Look at Chat, Threads and Today. Alex is a
   fictional Lagos-based Python/FastAPI developer exploring junior AI engineering opportunities.
2. **Save:** check **Allow saving or updating this idea, project, or commitment**. Send
   “Save a commitment called My judge test, with next action Draft an outline. Don't schedule check-ins yet.”
3. **Revise:** “Update My judge test: the next action is Rehearse the outline.” Verify it in Today.
4. **Autonomy:** “Remind me once in 30 seconds to rehearse my judge test.” Close the browser tab.
   After about one minute, open and sign in again. The worker-created reminder appears in Chat.
5. **Evidence:** open **Context** on a model response. Inspect the requested/returned NVIDIA
   model, tool calls, successful tool receipts and usage. On the reminder message, inspect the
   current scheduled-action evidence. The deterministic worker itself needs no model call.
6. **Research:** with saving unchecked, ask “Help me find AI engineering opportunities that fit
   what I'm looking for. Use my saved AI engineering opportunities project and check a source.”
   Inspect `research_goal` for the actual query, owned goal context, Tavily request IDs, sources,
   extraction success/failure and reported credits.
7. **Research → action:** copy a returned source URL. With saving checked, ask “Save a commitment
   to assess this opportunity: [URL]. Next action: verify Nigeria eligibility and whether it's
   still open. Retain the source URL; don't schedule check-ins yet.” Verify the saved source and
   next action in the inspector or Today.

Use unique commitment titles if another judge has already used the example. Avoid deleting shared
seeded goals so other judges can reproduce the research scenario. No private information is needed.

## What to expect

- Model/provider: **`nvidia/Nemotron-3.5-Super-VL` via Nebius Token Factory**.
- A saved commitment without check-ins is not automatically monitored. Request a reminder or
  an explicit recurring cadence when you want follow-through.
- Judge reminders are **in-app**, not email or Telegram push notifications. Telegram is intentionally
  disconnected from this isolated environment.
- Check-ins are finite; they do not run indefinitely. Pause/cancel controls remain available.
- Research is real and results vary. A page fetched by Tavily is evidence, not proof that every
  listing attribute is accurate or that you qualify. Mica should identify important unknowns.
- Requests are bounded: up to two model/tool calls per turn, 2,400 output tokens per model call;
  shared chat protection permits 20 turns/minute and research permits 60 requests/day. Ordinary
  judge testing is free. A temporary 429 response includes a retry interval.
- `/api/demo/status` exposes nonsecret provider, revision, availability and worker-heartbeat status.

The demo uses a different database and restricted database role from the private assistant.
It has no personal messaging credentials and cannot read the private owner's tables.
