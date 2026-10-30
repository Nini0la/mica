# Submission architecture

```text
Judge browser (Next.js static export)
               │ same-origin HTTPS + session/CSRF
               ▼
Modal ASGI web function → existing FastAPI / AgentRuntime
               │                  │
               │                  ├─ Context Builder: current owned state + selected history
               │                  ├─ NVIDIA Nemotron / Nebius Token Factory
               │                  ├─ validated native domain tools
               │                  └─ research_goal → Tavily Search + bounded Extract
               ▼
Dedicated Neon PostgreSQL database + restricted demo role
  conversations / threads / commitments / proposals / reminder plans
  action leases / outcomes / provenance / existing response traces
               ▲
               │ every 30 seconds; max one scheduler container
Modal personal scheduler → due-state validation → atomic in-app reminder
```

The web function scales to zero when unused; at most two web containers are permitted. The
scheduler is independent of the browser and shares the established action/lease/idempotency
implementation. Reminder execution is deterministic. There is no second reminder scheduler,
message broker, agent framework, vector database or duplicate observability system.

Research is available only through a user conversational turn. It binds to an owned goal in
selected context, accepts one bounded query, returns at most three sources and extracts at most
one public source. The whole external operation is bounded by a killable 12-second subprocess.
API credentials enter the subprocess through stdin, not command arguments or traces. There is
no unattended search loop, contact action or automatic application submission.

Source evidence is retained in the existing response trace. A selected finding's URL is retained
in a commitment's existing next-action/constraints fields; no new research schema was required.
Remote content is treated as untrusted data. The model synthesizes relevance and unknowns;
backend controls enforce ownership, permissions, bounded operations and persisted effects.

The private Telegram assistant is a separate deployment with its own database credentials and
owner linkage. The judge runtime contains no Telegram/WhatsApp credentials. Production remains
single-owner by design; this submission does not claim general multi-user isolation.
