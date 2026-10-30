# Submission verification evidence

## Official timing

The [official rules](https://nebiusglobalaihackathon.devpost.com/rules) were checked October 30,
2026: submission closes **18:00 WAT / 17:00 UTC October 30**; judging ends **20:00 UTC December 23**.
The dedicated demo is configured through **December 31, 2026 at 09:54:34 UTC**.

## Gate A — qualifying NVIDIA/Nebius application path: passed

Exact model: **`nvidia/Nemotron-3.5-Super-VL`**.
Endpoint: **`https://api.tokenfactory.nebius.com/v1/chat/completions`**.

At 08:22 UTC October 30, an actual Mica HTTP/PostgreSQL run verified contextual conversation,
native commitment creation, a persisted 30-second reminder, API shutdown, separate worker delivery
and a duplicate sweep with no second effect. A subprocess network audit recorded four Nebius
requests, **zero Azure requests**, and **zero worker inference calls**.

The four calls used 49,854 input / 1,282 output tokens. Measured model latency ranged from 2.199
to 3.031 seconds. At authenticated Nebius list rates of $0.30/M input and $0.90/M output, the
uncached estimate was **$0.01611**; this is not an invoice and excludes cache discounts/credits.
The reminder executed 3.877 seconds after its due time while the API was stopped.

The deployed judge browser test additionally confirmed native commitment creation **and amendment**
through the same model, rather than relying on the standalone provider preflight.

## Gate B — public HTTPS access: passed

URL: **https://nini-adegboyega--mica-demo.us-east.modal.run**

A fresh Chromium browser session signed in, loaded realistic synthetic context, made real model
calls, created and amended a commitment, and scheduled a 30-second reminder. The entire browser
was closed for 65 seconds. A new browser session then verified a completed plan and exactly one
successful scheduled action, and inspected its delivered in-app message.

Initial deployed verification IDs:
- Commitment: `1b8f75bc-3a3b-4126-8e98-31fe5c826ba7` (amended to version 2).
- Reminder plan: `ee17f42d-afb3-48cc-9de4-f3f304cd62ea`.
- Successful action: `8978df19-2621-4ea5-ad82-5117e6017e36`.

Isolation verification: separate PostgreSQL database and restricted `mica_judge` role; attempted
reads of private-owner tables failed with PostgreSQL insufficient-privilege SQLSTATE `42501`.
No Telegram links or messaging credentials are present in the judge environment.

## Gate C — Tavily research to action: passed

An actual deployed browser turn asked Mica to research its saved AI engineering opportunities
goal. Nemotron selected `research_goal` using the owned goal and a query influenced by Nigeria,
remote work, junior AI engineering and Python/FastAPI constraints. The tool made genuine Tavily
Search and Extract calls and retained their request IDs, usage and source excerpts in the existing
Context Inspector. A source URL was then saved in a model-created commitment's next action and
constraints, with no invented deadline or unsolicited check-ins.

Verified runtime receipts from the October 30 test:
- Tavily Search request: `9a2f2fed-b5b1-43a9-b129-3944f1c7b60d` (reported usage: 1 credit).
- Tavily Extract request: `df11657c-aef1-4b18-810e-d4c0604d3e4b` (successful result; reported usage:
  0 credits on that call under Tavily's successful-URL batching).
- Source: `https://www.remoterocketship.com/jobs/junior-ai-engineer`.
- Created commitment: `183cf630-79f0-4fcd-8f35-609c6c6082e0`, source retained in next action/constraints.

The response explicitly said the sources did **not** confirm a Nigeria-accepted opening and
identified location eligibility/current availability as unknown. This is discovery plus a
verification next step, not a claim of a guaranteed job match. Results may change between runs.

## Fixes and limitations discovered during deployment

- The first research deployment lacked `httpx` in runtime dependencies. It had previously been
  a development-only dependency. Promoting it to production dependencies and regenerating the
  locked Modal requirements restored the deployed application; the private assistant was unaffected.
- An initial research synthesis exceeded the small output budget. Research synthesis now uses
  the same NVIDIA model with reasoning disabled for the final evidence-only summary, within the
  same NVIDIA provider; interpretation/tool selection retains low reasoning. Final clean-seed
  testing also exposed insufficient room for some tool-selection responses at the earlier
  1,200-token setting. The judge profile now has a bounded 2,400-token ceiling; the 15-second
  per-call deadline, two-call limit and no-Azure-fallback rule remain unchanged.
- A transcript review found mixed aggregator snippets caused a wrong salary attribution. Fresh
  research synthesis now receives only the authoritative goal and fresh evidence, not old research
  replies; extracted text supersedes search snippets. Findings are presented as source-page leads,
  with unverified eligibility/current availability. The model can still make mistakes; source
  evidence remains inspectable and individual opportunities require verification.
- Ordinary chat still contains some internal server-confirmation formatting from the preserved
  baseline. Broader conversational polish was explicitly deferred, not represented as complete.
- The shared demo is not a multi-user privacy product; reminders are in-app. No public email,
  WhatsApp or personal Telegram sends are enabled.

## Regression checks

Before final packaging: **604 backend tests passed**, Ruff and mypy passed (41 app modules).
**69 frontend tests passed**, along with ESLint, TypeScript checks, dependency audit and the
static production build. Provider/permission/recovery unit tests use explicit fakes where noted;
the browser/provider/research/worker results above use the deployed services.

Source revision and final health verification are recorded in the release handoff. The owner
must still record/upload the public YouTube video and submit the Devpost form; neither is claimed
complete by these engineering checks.
