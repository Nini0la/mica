# Devpost submission draft — Mica

## Project name
Mica

## Tagline
A personal AI companion that turns intentions into context-aware research and persistent follow-through.

## Track
Personal AI

## Inspiration
Most assistants are helpful while the conversation is open. The harder problem is what happens
afterward: remembering what mattered, finding useful information in context, preserving the next
step, and following through without asking someone to reconstruct the whole conversation.

Mica grew from wanting a personal companion that could carry a small promise forward—without
turning every conversation into a task list or pretending an action happened when it did not.

## What it does
Mica combines natural conversation with persistent goals, ideas, commitments and history. A user
can ask it to save or revise an intention, request a short reminder, and return later to find the
current state and an actual record of what ran. Its worker can deliver an in-app reminder while
the browser is closed.

The Scout research capability connects an existing goal to Tavily Search and a bounded source
extraction. For example, a fictional user seeking AI engineering opportunities can ask Mica to
research using their saved location, technical interests and constraints. Mica returns source URLs,
explains relevance and unknowns, and can turn a selected lead into a commitment with a concrete
verification step and the source retained.

The judge demo uses synthetic data in an isolated account. The existing private version also
supports owner-linked Telegram conversation and delivery. Telegram is deliberately disconnected
from the judge environment.

## How we built it
NVIDIA **Nemotron 3.5 Super VL**, served through **Nebius Token Factory**, interprets messages and
selects native function calls. FastAPI validates those calls against owner scope, permissions,
schemas and current record versions. PostgreSQL on Neon holds authoritative conversation,
commitment, proposal, scheduling and execution state.

The Next.js interface exposes Chat, Today, Threads and History. A Modal deployment serves the
browser interface and API over HTTPS; a separate single scheduler runs the existing durable
execution loop. Tavily Search and Extract plug directly into the same tool system, rather than
creating a second agent framework. Existing response traces show model/provider identity,
token usage, tool inputs, execution receipts and resulting action evidence.

## Challenges we ran into
An API connection alone did not establish that a model could run the product. We tested real
conversation, native tool selection, persistence, corrections, scheduling and restart behavior.
Model outputs exposed nested-argument formatting problems and incomplete action proposals;
validation rejected those outputs rather than silently inventing state.

Another challenge was separating language from evidence: a proposed action is not a saved one,
and a saved commitment is not automatically a scheduled intervention. For research, a search
snippet is also not a guarantee that a role is current or that an applicant qualifies. The demo
keeps source evidence and uncertainty visible.

## Accomplishments we're proud of
- Real NVIDIA Nemotron inference through Nebius inside the application, including model-selected
  commitment creation and modification.
- A reminder that persisted, executed with the browser closed, and did not duplicate on a later sweep.
- Goal-aware Tavily research connected to an actionable, persistent next step.
- A public HTTPS judge demonstration isolated from the developer's private assistant and data.
- Inspection that distinguishes model output, validated tool effects and worker execution.

## What we learned
Reliable personal AI needs both flexible interpretation and firm execution boundaries. A good
conversation is valuable, but operational state, permissions, idempotency and provenance make
follow-through trustworthy. Testing complete application journeys revealed issues that a small
model benchmark or successful API probe would have missed.

## What's next
We want to improve the distinction between saving and active monitoring, make confirmations and
briefings more natural, preserve multi-step goals more clearly, and develop better guided reflection
and context-sensitive accountability. Runtime experiments such as Hermes are future work; they
are not part of the submitted implementation.

## Significant work during the eligibility period
Mica's recorded development began in its original working repository on September 30, 2026,
within the August 26–October 30 submission period. It was developed iteratively from conversation
and persistent context through commitments, autonomous follow-through and Telegram connectivity.
The final integration work added NVIDIA Nemotron through Nebius Token Factory, real application
validation, the isolated judge deployment and Tavily-powered research-to-action functionality.
The October 30 public repository is a clean submission snapshot of that same project, not the
start of development. The README links the original development history and dated evidence.

## Nebius / NVIDIA feedback
The OpenAI-compatible Token Factory endpoint made it practical to integrate an NVIDIA model into
an existing typed tool runtime. The useful validation was application-level: correct nested tool
arguments, proposal persistence, response handling and recoverable effects. Explicit model listing,
model-deprecation notices, returned token usage and pricing metadata helped us make a reproducible
configuration. Clearer per-model guidance on tool JSON Schema features and reasoning/output-budget
interactions would make this path easier for builders.

## Links and submission fields
- Demo: https://nini-adegboyega--mica-demo.us-east.modal.run
- Public source: https://github.com/Nini0la/mica
- Video: **owner must record and upload publicly to YouTube; not yet recorded/uploaded**.
- Testing credentials: copy the dedicated judge access code from the private handoff, not an API key.
- Bonus category: Best Use of Tavily, supported by real integrated Search/Extract evidence.

Do not claim an award, formal eligibility approval, video upload or Devpost submission until verified.
