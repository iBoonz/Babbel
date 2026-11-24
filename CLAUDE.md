# Babbel: notes for Claude Code

Phone-first AI companion that calls older and lonely people in Flemish (nl-BE), remembers them,
keeps family in the loop and escalates when something is wrong. First version (MVP) for a
family pilot in February 2027. Solo founder: Bert (product manager, healthcare AI background).

## Source of truth
- Spec: `specs/spec.md` — requirement IDs FR-xx / NFR-xx, open decisions D1–D16. Edit it here, in the
  same change as the code; never renumber or reuse an ID; add a change-log line. Rows tagged **New** are
  not built yet. `specs/traceability.md` maps each ID to code and tests. Keep both current.
  (The old claude.ai doc https://claude.ai/artifact/4vc64Mxc6SLH1zu31Kykbp is a read-only snapshot.)
- Build plan: https://claude.ai/artifact/5DZ5aWjh1ptNZZV9KLeTBX

## Rules
- Spec first: a new behaviour gets an FR/NFR ID in the spec before code. Reference the ID in a
  docstring or comment where it is implemented, and in the test.
- Every word Babbel says, every prompt and every safety regex lives in `src/babbel/locales/nl-BE.yaml`.
  Never hardcode Dutch text in Python.
- `src/babbel/core/` stays pure (no I/O, no DB, no network) and fully unit-tested.
- Safety: a false alarm is acceptable, a miss is not. Before changing safety patterns, prompts or
  models, add the case to `evals/safety_cases.yaml` first, then run `make evals`.
  FR-30/31/32 must stay at 100% recall.
- Privacy: no audio is ever stored (FR-11). Never store bank details or codes (`core/memory.FORBIDDEN`).
  Private topics never reach family messages. EU hosting only.
- The scheduler must never place a double call (idempotency key + unique constraint, NFR-03).
- Babbel never pretends to be human, never gives medical, legal or financial advice, never calls 112 itself (D12).

## Commands
- `make install` — install with agent, asr and dev extras
- `make test` — pytest (no keys needed); `make lint` — ruff
- `make evals` — offline safety gate (CI); `make evals-llm` — classifier + simulated callers (needs LLM keys)
- `make api` / `make scheduler` / `make agent` / `make topics`; `docker compose up --build` for everything

## Layout
- `src/babbel/agent/` LiveKit voice agent (one job per call), provider builders, backend client
- `src/babbel/api/` FastAPI backend, SQLModel tables, scheduler runner, topic fetcher
- `src/babbel/core/` safety, scheduler, escalation, memory, prompts, cost, llm, notify, locale
- `evals/` safety cases and personas; `asr/` dialect WER benchmark

## Next up (not built yet)
Family portal UI + magic-link logins; WhatsApp channel; private bridge announcement with contact
consent (FR-42); missed-connection note (FR-43); partner API (D14); Langfuse tracing (NFR-08);
transcript retention job (NFR-05); first real call over a Belgian SIP number.

## Working in squads
This repo uses the [squads](https://github.com/j0hanz/squads) Claude Code plugin, enabled in
`.claude/settings.json` (marketplace `j0hanz/squads`, plugin `squads@squads`). Its router hook picks
the skill for each task; follow it. Needs `jq` and a POSIX shell for the hook gates.
- Vague idea → `/squads:brainstorm` → `/squads:plan` (draft, then validate).
- Clear feature → `/squads:plan` → `/squads:tdd` (one task) or `/squads:dispatch-agents` (several).
- Failure → `/squads:debug` first, then `/squads:tdd` for the fix.
- Before commit or PR → `/squads:review` (request, then resolve).
- Recurring bulk work → `/squads:forge-workflow` (writes `.claude/workflows/` and `docs/workflows/CATALOG.md`).

Squads works alongside the rules above. A plan names the FR/NFR IDs it touches, and a new
behaviour needs a spec ID before the plan is validated. `tdd` tests reference those IDs. Any
change to safety starts with a case in `evals/safety_cases.yaml`, and `make evals` must pass
before `review`. Finish with `make lint test evals`.
