# Mica — personal AI with persistent follow-through

Mica connects an intention expressed in conversation to saved context, evidence-backed research,
an actionable next step, and a durable reminder that runs even after the browser closes.

**Live judge demo:** https://nini-adegboyega--mica-demo.us-east.modal.run

Use the access code supplied in the Devpost testing instructions. The demo is a separate,
shared synthetic account: no personal Telegram connection, private owner database or external
messaging authority is attached. Please use fictional information.

## What works

- Natural conversation through **NVIDIA `nvidia/Nemotron-3.5-Super-VL` on Nebius Token Factory**.
- Context drawn from current threads, commitments, preferences and recent conversation.
- Model-selected, server-validated tools for saving and revising goals, ideas and commitments.
- One-time reminders and finite recurring check-ins, durable PostgreSQL schedules, retries and
  duplicate-effect protection. Judge-demo delivery is in the shared in-app chat.
- **Goal-aware Tavily research:** search uses the selected goal's constraints; a public result
  is extracted for deeper evidence; original URLs and uncertainty accompany the findings.
  A selected lead can become a commitment with its source URL retained.
- An existing Context Inspector shows actual model/provider inputs, outputs, token/latency data,
  tool arguments/results, persisted changes and action/delivery evidence.
- Existing daily briefings, reflection, vocabulary and optional private Telegram support are
  preserved. The judge instance uses in-app delivery only.

## Try it

1. Sign in with the supplied judge access code.
2. Ask: **“What are my current goals?”**
3. Enable the per-message saving checkbox and ask:
   **“Save a commitment called Prepare my demo, with next action Draft the opening. Don't schedule check-ins yet.”**
4. Ask: **“Update Prepare my demo: the next action is Rehearse the opening.”**
5. Ask: **“Remind me once in 30 seconds to rehearse my demo.”** Close the browser, wait about a
   minute, then sign in again. The reminder should be in Chat; Today shows the plan/action state.
6. Ask: **“Help me find AI engineering opportunities that fit what I'm looking for. Use my saved AI engineering opportunities project and check a source.”**
7. Select a returned URL and ask Mica to save an eligibility-verification next action, retaining
   that URL. Use the response's **Context** control to inspect the research/tool evidence.

[Detailed judge guide](JUDGE_GUIDE.md) · [Three-minute video sequence](VIDEO.md) ·
[Deployment and recovery](DEPLOYMENT.md) · [Architecture](ARCHITECTURE.md) ·
[Verification evidence](EVIDENCE.md)

## Run locally

Requirements: Python 3.11+, `uv`, Node.js 20.9+ (24 recommended), npm and PostgreSQL.

1. Copy `.env.example` to `.env` and configure a local PostgreSQL URL, access code and session
   secret. Set `MODEL_MODE=live`, `PROVIDER=nebius`, `NEBIUS_API_KEY`,
   `NEBIUS_MODEL=nvidia/Nemotron-3.5-Super-VL`, `NEBIUS_REASONING_EFFORT=low`, and
   `MODEL_MAX_OUTPUT_TOKENS=2400`. Optionally set `TAVILY_API_KEY`.
2. In `backend/`, run `uv sync --extra dev`, then
   `uv run --env-file ../.env alembic upgrade head`.
3. In `backend/`, start the API:
   `uv run --env-file ../.env uvicorn app.main:app --host 127.0.0.1 --port 8000`.
4. In `frontend/`, run `npm ci && npm run dev`; open http://localhost:3000.
5. For unattended in-app work, run a separate process from `backend/`:
   `uv run --env-file ../.env python -m app.scheduler --personal-only`.

Use a fresh disposable test database for the PostgreSQL integration suite:

```sh
# From backend/; this database is destructive test state, never a live deployment.
POSTGRES_TEST_URL=postgresql+psycopg:///mica_autonomy_test uv run pytest -q
uv run ruff check app tests
uv run mypy app
# From frontend/
npm test
npm run lint
npm run typecheck
npm run build
```

## How NVIDIA and Nebius are used

Every demonstrated chat/tool-selection call goes through the application gateway to
`https://api.tokenfactory.nebius.com/v1/chat/completions`, using the NVIDIA model above.
The model interprets requests and proposes native function calls; deterministic application code
checks ownership, permission, versions and tool schemas before committing effects. The worker
does not continuously reason or call the model to deliver a reminder.

Azure remains an optional manually selected alternative in source configuration. The judge
deployment contains no Azure credentials and does not silently fall back to Azure.

### Where Token Factory accelerated development

**Token Factory let us use hosted NVIDIA Nemotron inference without provisioning GPUs,
downloading model weights or operating an inference server.** Its OpenAI-compatible API fitted
Mica's existing gateway and native-tool interface, so we could focus on persistent context,
validated actions, research and reliable follow-through.

Authenticated model discovery helped us select an available Nemotron variant; deprecation notices
helped us avoid a model scheduled for retirement before judging. Returned model identity and token
usage, together with pricing metadata, made real application runs inspectable and their inference
costs estimable. We tested native tool execution through Mica, not just standalone API connectivity.

Our Nebius usage is **Token Factory inference**. Modal hosts the web/API and scheduler, while
Neon hosts PostgreSQL. We do not claim to have deployed GPU instances, Serverless Jobs or
Serverless Endpoints on Nebius AI Cloud.

## Development history and hackathon-period work

**This repository is a clean submission snapshot of Mica, not the start of its development.**
The original working repository was created for this same project on **September 30, 2026**;
its earliest recorded specification and implementation commits are also dated September 30.
This submission repository was created on **October 30, 2026** to present the application,
tests and reproduction instructions without private operational artifacts or unrelated materials.

Both repository dates fall within the hackathon's **August 26–October 30, 2026** submission
period. The recorded progression includes:

- **September 30–October 1:** persistent conversation/context, daily continuity, commitments,
  vocabulary and the application interface.
- **October 20–29:** contextual model assistance, companion refinements, durable autonomy,
  Telegram delivery, conversational proposals and separate reminder plans.
- **October 30:** NVIDIA Nemotron integration through Nebius Token Factory, real application
  acceptance runs, isolated HTTPS judge deployment and Tavily research-to-action integration.

See the [original development history](https://github.com/Nini0la/fpc-pet/commits/feat/functional-autonomy/),
the [initial specification commit](https://github.com/Nini0la/fpc-pet/commit/dec838d), and
[verification evidence](EVIDENCE.md). `SOURCE_SNAPSHOT.json` records the source revision and
runtime-file hashes used for the submission snapshot.

## Current boundaries

This is a bounded personal assistant, not an unrestricted computer agent. The public demo is one
shared synthetic account, not a multi-user privacy product. Model decisions can be imperfect;
the inspector distinguishes model claims from committed effects. Research findings are leads,
not guarantees that a job is open or accepts a particular applicant. Search results change.
Reminders are delivered on worker sweeps, not with hard real-time guarantees. Broader adaptive
accountability, guided reflection and Hermes runtime work remain future work.

Source code is MIT-licensed. Third-party libraries, NVIDIA model services, Nebius and Tavily are
subject to their respective licenses and terms. No model weights or private datasets are included.
