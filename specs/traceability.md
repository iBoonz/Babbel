# Requirement traceability (spec v0.1, first version)

The spec is `specs/spec.md` (source of truth). This table maps each built requirement to code and tests.
Requirements tagged **New** in the spec are not built yet and are listed at the bottom.

| ID | Requirement (short) | Code | Tests / evals |
|---|---|---|---|
| FR-01 | Scheduled outbound calls | `core/scheduler.due_calls`, `api/scheduler_runner.tick`, `agent/worker.entrypoint` | `test_scheduler::test_first_attempt_in_window` |
| FR-02 | One retry after 15 min | `core/scheduler.due_calls` | `test_scheduler::test_one_retry_after_missed` |
| FR-03 | Inbound, known vs unknown caller | `api/main.senior_by_phone`, `agent/worker.entrypoint` | `test_api::test_unknown_caller_gets_404` |
| FR-04 | Quiet hours 08:00–20:00 | `core/scheduler.within_quiet_hours`, `config.default_quiet_start` | `test_scheduler::test_quiet_hours_from_eight`, `test_api::test_onboarding_defaults...` |
| FR-05 | Max call length | `agent/worker.enforce_max_length` | manual |
| FR-06 | Pause by voice | `BabbelAgent.pause_calls`, `api/main.pause_calls` | `test_api::test_pause_and_forget_by_voice` |
| FR-10 | Profile | `Senior` table, `core/memory.memory_context` | `test_prompts`, `test_api` |
| FR-18 | Opening says who is calling, picks up last time / family | `core/prompts.opening_line`, locale `system_prompt` | `test_prompts`, persona evals |
| FR-11 | Summary + facts, no audio | `agent/worker.finalize`, `core/llm.summarise_call`, `api/main.complete_call` | `test_api::test_call_complete_stores...`, `test_memory` |
| FR-12 | Use memory naturally | locale `system_prompt` | persona evals |
| FR-13 | Forget on request | `BabbelAgent.forget`, `api/main.forget_by_voice`, `DELETE /memory/{id}` | `test_api::test_pause_and_forget_by_voice` |
| FR-14 | Learn interests, proactive topics | `api/topics.py`, `api/main.call_context`, `core/prompts.topics_block` | `test_topics`, `test_api::test_topics_offered_once...` |
| FR-15 | Local activities | `LocalActivity`, `api/main.call_context`, `core/prompts.activities_block` | `test_api::test_topics_offered_once_and_activities_matched` |
| FR-16 | Help requests (family, Helpper) | `BabbelAgent.request_help`, `POST /internal/help-requests` | `test_api::test_help_request_goes_to_family_and_partner` |
| FR-17 | First-call introduction | `core/prompts.first_call_line`, `opening_line`, locale `first_call` | `test_prompts::test_first_call...`, `test_api::test_first_call_flag...` |
| FR-19 | West- and East-Flemish lexicon (partly built) | `locales/dialects/vlaams.yaml`, `core/dialect.py`, `asr/benchmark.py --dialect-hints`, bake-off `dialect` suite | `test_dialect`, `evals/dialect_cases.yaml` |
| FR-20 | AI disclosure every call | `core/prompts.disclosure_line`, `BabbelAgent.on_enter` | `test_prompts::test_disclosure...` |
| FR-21/22 | Truthful; no medical/legal/financial advice | locale `system_prompt` | persona evals (roger-ovl) |
| FR-30–36 | Safety triggers and escalation (keyword rules include dialect variants from the lexicon) | `core/safety`, `core/escalation`, `api/main.open_escalation`, `process_due_steps` | `test_safety`, `test_escalation`, `test_api::test_urgent...`, `test_api::test_missed_calls...`, `babbel-evals` |
| FR-37 | Wellbeing: repetition, fear | `core/safety.RepetitionTracker`, locale `FR-37` patterns, `api/main.raise_wellbeing` | `test_safety::test_repetition...`, `test_api::test_wellbeing...` |
| FR-40 | Onboarding, SMS-verified contacts | `POST /seniors`, `/contacts`, `/verify`, `/schedule`, `/consents` | `conftest.senior`, `test_api` |
| FR-41 | Family update after each call | `api/main.complete_call` | `test_api::test_call_complete...` |
| FR-42 | "Bel Lotte" bridge | `BabbelAgent.call_contact` | manual (needs SIP) |
| FR-43 | Missed-connection note | locale `missed_connection` | TODO |
| FR-44 | Contact quiet hours, opt-out | `BabbelAgent.call_contact`, `chain_for` | `test_escalation::test_chain_order` |
| FR-45 | Family ask list | `FamilyQuestion`, `/seniors/{id}/questions`, `BabbelAgent.answer_family_question` | `test_api::test_family_ask_list_flow` |
| FR-46 | Family follow-up view | `GET /seniors/{id}/summaries` | `test_api::test_family_ask_list_flow` |
| FR-50 | Admin view | `GET /admin`, `/admin/dashboard` | `test_api::test_dashboard` |
| FR-51 | Cost per call | `core/cost`, `api/main.complete_call` | `test_cost` |
| FR-52 | Feedback | `call_context.ask_feedback`, `prompts.feedback_note`, `BabbelAgent.record_feedback` | `test_api::test_feedback_every_third_call` |
| FR-53 | Export and delete | `/seniors/{id}/export`, `DELETE /seniors/{id}` | `test_api::test_export_and_delete` |
| NFR-02 | Cost ≤ €0.07/min, alert > €0.08 | `core/cost`, `dashboard_data` | `test_cost` |
| NFR-03 | Idempotent scheduler | `DueCall.idempotency_key`, unique constraint | `test_scheduler::test_no_double_call` |
| NFR-11 | Dialect speech recognition | `asr/prepare_audio.py`, `asr/benchmark.py`, `evals/asr_tools.py` | WER gate ≤ 25% per dialect, safety phrases kept; `test_bakeoff` |
| D4 | Language model choice | `evals/bakeoff.py`, `evals/providers.py`, `evals/models.yaml`, `tool_cases.yaml`, `memory_cases.yaml` | `test_bakeoff`; `make bakeoff` |
| NFR-09 | Swappable providers | `config.py`, `agent/providers.py` | — |
| NFR-10 | All words in locale files | `locales/nl-BE.yaml`, `core/locale.py` | all prompt tests |

## Not built yet

| ID | Requirement (short) | Note |
|---|---|---|
| FR-07 | Voicemail detection | Needs answering-machine detection in the SIP/agent path |
| FR-08 | Senior's own agreement on the first call | Stored via `POST /seniors/{id}/consents`; locale line and agent tool to add |
| FR-23 | Someone else answers | Agent prompt + `answered_other` outcome |
| FR-38 | Call back when the line drops during a flag | Scheduler/agent: one immediate call-back |
| FR-43 | Missed-connection note | Locale line exists; sending logic and test TODO |
| NFR-01 | Latency | Turn timing log TODO |
| NFR-05 | Transcript retention job | TODO |
| NFR-06 | Hashed, expiring SMS codes | `Contact.verification_code` is stored as plain text today |
| NFR-08 | Langfuse tracing | TODO |

