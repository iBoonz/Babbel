# Babbel

A phone-first AI companion that calls older and lonely people in Flemish, remembers them,
keeps their family in the loop, and escalates when something is wrong.

This repo is the **first version (MVP)** for the February 2027 family pilot. Every requirement
in the spec has an ID (FR-xx / NFR-xx); the code, tests and evals reference those IDs.
See `specs/spec.md` for the spec and `specs/traceability.md` for the map from requirement to code to test.

- Spec: [`specs/spec.md`](specs/spec.md) (source of truth; the earlier claude.ai doc is a snapshot)
- Build plan: https://claude.ai/artifact/5DZ5aWjh1ptNZZV9KLeTBX

## How it works

```
 scheduler ──dispatch──▶ LiveKit agent ──SIP──▶ senior's phone
     │                      │  STT → LLM → TTS (any OpenAI-compatible endpoint)
     │                      │  keyword safety rules + repetition tracker on every turn
     ▼                      ▼
  FastAPI backend ◀──/internal── agent: profile, memory, ask list, topics, flags, summary
     │
     └──▶ SMS / call to family (escalation chain), family updates, follow-up view
```

| Part | Where | What |
|---|---|---|
| Voice agent | `src/babbel/agent/worker.py` | One LiveKit job per call: opening line (FR-17/20), tools for safety, pause, forget, ask list, help, bridge |
| Backend | `src/babbel/api/main.py` | Seniors, contacts, schedule, memory, calls, escalation, ask list, summaries, topics, activities, help requests, GDPR |
| Scheduler | `src/babbel/api/scheduler_runner.py` | Due calls with idempotency keys (never a double call), retries, escalation steps |
| Topic fetcher | `src/babbel/api/topics.py` | RSS per interest → fresh topics, each offered once (FR-14) |
| Core logic | `src/babbel/core/` | Pure, tested: safety, scheduling, escalation, memory, prompts, cost |
| Words | `src/babbel/locales/nl-BE.yaml` | Every sentence Babbel says, every prompt, every safety pattern. Edit wording here, not in code |
| Evals | `evals/`, `src/babbel/evals/` | Safety gate (CI), simulated callers graded by an LLM, and the model bake-off (`make bakeoff`) |
| Dialect ASR | `asr/` | Clips from your own recordings, WER and safety phrases per dialect, and when (not) to fine-tune |

**Choosing the models:** follow [`docs/model-selection.md`](docs/model-selection.md).

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
make install
cp .env.example .env          # fill in LiveKit and model keys for real calls
make test                     # unit + API tests, no keys needed
make evals                    # offline safety gate
make api                      # http://localhost:8000/docs  and  /admin?token=...
make scheduler                # dry-run dispatches without LiveKit keys
make agent                    # LiveKit agent in dev mode
```

Or everything at once with Postgres: `docker compose up --build`.

### First person in five calls

```bash
H='-H X-Admin-Token:change-me-admin -H Content-Type:application/json'
curl $H localhost:8000/seniors -d '{"display_name":"Maria Vermeulen","preferred_name":"Maria",
  "phone":"+3250123456","municipality":"Aalter","family":["zoon Jan"],"interests":["voetbal","tuin"]}'
curl $H localhost:8000/seniors/1/contacts -d '{"name":"Jan","relation":"zoon","role":"admin","phone":"+32470..."}'
curl $H localhost:8000/contacts/1/verify -d '{"code":"<code from SMS/console>"}'
curl $H localhost:8000/seniors/1/questions -d '{"text":"Heb je je pillen genomen?","kind":"check_in","recurring":true}'
curl $H localhost:8000/activities -d '{"municipality":"Aalter","title":"Kaartnamiddag",
  "starts_at":"2026-12-04T14:00:00+01:00","place":"Dienstencentrum","tags":["iedereen"]}'
```

Maria is now called **every day at 10:30** (D2; pass `call_days` for fewer days), never
before 08:00 or after 20:00. Her first call introduces Babbel "samen met uw zoon Jan" (FR-17).

## Telephony setup (LiveKit)

1. Create a LiveKit Cloud project (EU region) or self-host LiveKit + SIP.
2. Buy a Belgian number at a SIP provider (Telnyx, Twilio or a Belgian operator) and create an
   **outbound trunk** in LiveKit; put its ID in `SIP_OUTBOUND_TRUNK_ID`.
3. For inbound calls, create an inbound trunk and a dispatch rule that sends calls to agent
   `babbel`. Unknown numbers hear a generic message (FR-03).

## Safety

Safety lives in three layers, and a false alarm is acceptable where a miss is not:
1. regex rules per rule ID in the locale pack, including West- and East-Flemish variants;
2. an LLM classifier on every utterance (when `LLM_SMALL_MODEL` is set);
3. the `raise_safety_flag` tool, used by the conversation model itself.

The escalation chain is family admin → contacts → care partner → operator. Urgent and critical
flags call and text, moving down the chain every 3 minutes until someone acknowledges.
FR-37 (repetition, fear) is noted in the family update and becomes an SMS when it grows.
**Before the pilot:** walk through every FR-30 to FR-37 script with a real phone, and add
every miss to `evals/safety_cases.yaml` before fixing it.

## What is deliberately not here yet

- Family portal UI and logins (the API exists: ask list, summaries, help requests; admin token for now)
- WhatsApp channel (SMS through a webhook first, D10)
- Private bridge announcement with explicit consent from the contact (basic version in `call_contact`)
- Partner API for help requests (SMS to the partner until a partnership is signed, D14)
- Langfuse tracing (NFR-08) and transcript retention job (NFR-05)
