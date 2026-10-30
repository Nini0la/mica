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
   `MODEL_MAX_OUTPUT_TOKENS=1200`. Optionally set `TAVILY_API_KEY`.
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

## Hackathon-period work

This submission builds on an existing personal-AI prototype. Significant work during the
August 26–October 30, 2026 eligibility period includes durable commitment/autonomy integration,
restart and duplicate protection, Telegram delivery, semantic proposals/reminder continuations,
Nebius/Nemotron integration and live validation, the isolated judge deployment, and Tavily
research-to-action integration. See [EVIDENCE.md](EVIDENCE.md) for dated verification.

## Current boundaries

This is a bounded personal assistant, not an unrestricted computer agent. The public demo is one
shared synthetic account, not a multi-user privacy product. Model decisions can be imperfect;
the inspector distinguishes model claims from committed effects. Research findings are leads,
not guarantees that a job is open or accepts a particular applicant. Search results change.
Reminders are delivered on worker sweeps, not with hard real-time guarantees. Broader adaptive
accountability, guided reflection and Hermes runtime work remain future work.

Source code is MIT-licensed. Third-party libraries, NVIDIA model services, Nebius and Tavily are
subject to their respective licenses and terms. No model weights or private datasets are included.
