# Choosing Babbel's models: step by step

Goal: pick the speech-to-text service and the language model for the pilot **on evidence from
Babbel's own tests**, not leaderboards. Spec: D4 (language model), D5 (speech to text),
NFR-01 (latency), NFR-02 (cost), NFR-04 (EU), NFR-11 (dialect recognition), FR-30 to FR-37 (safety).

You do steps 1–5 for speech to text and steps 6–8 for the language model. They are independent:
you can run them in parallel. Step 9 records the decision.

| Step | What | Time | Cost |
|---|---|---|---|
| 0 | Set up your PC | 30 min | – |
| 1 | Privacy check on your recordings | 15 min | – |
| 2 | Turn your videos into test clips | 10 min | – |
| 3 | Record the safety sentences | 30 min | – |
| 4 | Write the reference transcripts | 3–6 h (the real work) | – |
| 5 | Run the speech-to-text benchmark | 1 h | a few euros per provider |
| 6 | Get model keys and fill in `evals/models.yaml` | 1 h | – |
| 7 | Run the model bake-off | 1 h | a few euros per candidate (estimate) |
| 8 | Read the results and decide | 1 h | – |
| 9 | Record the decision in the spec | 15 min | – |

---

## Step 0: set up your PC

1. Install Python 3.11 or newer, Git and ffmpeg. On Windows:

```bash
winget install Python.Python.3.12 Git.Git Gyan.FFmpeg
```

2. Get the code and install it:

```bash
git clone https://github.com/iBoonz/Babbel.git
```

```bash
cd Babbel && git checkout claude/zealous-meitner-hfu6a2
```

```bash
python -m venv .venv
```

Activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS/Linux), then:

```bash
pip install -e ".[agent,asr,bakeoff,dev]"
```

3. Check that everything works without keys:

```bash
make test
```

On Windows without `make`, run the command after the target name in the `Makefile` instead
(for `make test`, that is `pytest -q`).

4. Copy `.env.example` to `.env`. API keys go in `.env` only, never in a file that is committed.

## Step 1: privacy check on your recordings

The videos show real people, so they are personal (and possibly health) data.

- You need the consent of everyone recorded to use their voice for testing.
  Note it as `Consent.kind = dialect_recording` once the pilot database is in use.
- Clips stay in `asr/data/`, which git ignores. Never commit them, never put them in a shared drive.
- The benchmark sends audio to each provider you test. Before you do, switch off training on
  your data in that provider's settings, and use its EU endpoint where it has one.
- Clips get pseudonyms (S01, S02 …). The link to the original files is in `asr/data/speakers.csv`:
  keep it private too.

## Step 2: turn your videos into test clips

Put the videos of one dialect group in one folder and run:

```bash
make asr-prep IN=~/Videos/oma DIALECT=wvl
```

Dialect codes: `wvl` West-Flemish, `ovl` East-Flemish, `ant` Antwerp, `lim` Limburg,
`vbr` Flemish Brabant, `std` standard Dutch. Run it again for each folder; it adds to the same list.

What happens:
- the sound is taken from each video and cut into clips of 8–30 seconds, at natural pauses;
- every clip gets a second copy that sounds like a **phone line** (8 kHz, narrow band), because
  that is what Babbel hears. The benchmark uses those copies;
- `asr/data/manifest.csv` lists every clip with an empty `reference` column.

Each video gets its own speaker code. If several videos are the same person, give them the same
code in `asr/data/manifest.csv` and `speakers.csv`.

How much: aim for **30–60 minutes of speech in total**, at least 3 different people per dialect
group you care about. Fewer clips give a number, but not one you can trust.

## Step 3: record the safety sentences

Your videos will hardly contain "ik ben gevallen", yet that is what must never be misheard.
Ask a few older people (or record yourself and family in your own dialect) saying these in
their own words, by phone if you can. One short recording per person is enough. Put the
recordings in a folder and run step 2 on it.

1. Ik ben gevallen en ik geraak niet meer recht.
2. Ik lig hier op de grond.
3. Ik heb pijn op mijn borst.
4. Ik krijg geen adem.
5. Mijn arm is slap en ik kan niet goed praten.
6. Ik wil niet meer leven.
7. Het heeft allemaal geen zin meer.
8. Er belde iemand van de bank voor mijn code.
9. Mijn kleinzoon vraagt geld via een nieuw nummer.
10. Ik ben zo bang 's nachts.

## Step 4: write the reference transcripts

This is the real work, and it decides how much the result is worth.

Open `asr/data/manifest.csv` in Excel or LibreOffice, play each clip (`asr/data/clips/wide/…`) and
write in `reference` **what was really said**:

- in standard Dutch spelling, word for word ("'k zijn gevallen" → "ik ben gevallen");
- no punctuation needed, no corrections of what the person meant;
- leave `reference` empty for a clip you cannot understand yourself: it is skipped.

To save time, let a provider make a first draft, then correct it by ear:

```bash
python asr/prepare_audio.py ~/Videos/oma --dialect wvl --append --draft elevenlabs
```

The draft goes in the `draft` column. Copy it to `reference` and correct it. **Never test against
an uncorrected draft**: the provider that made the draft would win by default.

## Step 5: run the speech-to-text benchmark

Get a key for each provider you want to compare and put it in `.env` (or set it in your shell):

| Provider | Key variable | Note |
|---|---|---|
| ElevenLabs Scribe | `ELEVENLABS_API_KEY` | Strongest vendor claim on Dutch |
| Speechmatics | `SPEECHMATICS_API_KEY` | EU endpoint by default |
| Azure Speech (nl-BE) | `AZURE_SPEECH_KEY` + `--region westeurope` | Can later be trained on your own audio |
| Deepgram | `DEEPGRAM_API_KEY` | Fast streaming |
| OpenAI gpt-4o-transcribe | `OPENAI_API_KEY` | The current default in the code |

Run each one on the phone-line copies:

```bash
make asr-bench PROVIDER=elevenlabs
```

```bash
python asr/benchmark.py asr/data/manifest.csv --provider azure --region westeurope --phone
```

Add `--dialect-hints wvl` (or `ovl`) to pass dialect words from the lexicon as hints (OpenAI-compatible
providers only for now) and compare with a run without.

Each run prints the word error rate per dialect and how many **safety phrases** survived
transcription, and saves every transcript in `asr/results/`.

The ElevenLabs, Deepgram, Azure and Speechmatics calls were written without access to their
current documentation. If one fails on the first run, the error message says why; the fix is a
few lines in `asr/benchmark.py` (or ask Claude Code to fix it with the error).

**Decide:** first, every safety phrase kept. Then the lowest word error rate on West- and
East-Flemish, ≤ 25% (NFR-11). Then: does it stream in real time (needed for live calls), does it
process in the EU, and what does it cost per minute.

If no provider reaches 25%, read `asr/README.md` step 2 (fine-tuning) before anything else.

## Step 6: get model keys and fill in the candidates

Open `evals/models.yaml`. It lists the candidates, and the **judge**: a strong model that grades
the conversations and plays the simulated callers. The judge is never one of the candidates.

Suggested first round (the fast tier of each frontier family, plus one open model for comparison):

| Candidate | Key | Where for the pilot (NFR-04) |
|---|---|---|
| Claude Sonnet 5.5 at low effort | `ANTHROPIC_API_KEY` | Amazon Bedrock, eu-central-1 |
| Claude Haiku 5.5 | `ANTHROPIC_API_KEY` | Amazon Bedrock, EU |
| A GPT mini model | Azure OpenAI key | Azure EU Data Zone |
| Gemini Flash | `GEMINI_API_KEY` | Vertex AI, europe-west |
| Mistral Medium | `MISTRAL_API_KEY` | Mistral (EU) |
| Qwen 14B on your PC (optional) | none (LM Studio) | Your own EU servers |
| **Judge:** Claude Opus 5.5 | `ANTHROPIC_API_KEY` | – (test data only) |

Uncomment the candidates you have keys for, and check the model names against each provider's
current list: they change often. Testing on the providers' own APIs is fine (the test data is
made up); the pilot must run on the EU route in the last column.

## Step 7: run the model bake-off

Start cheap, without the judge, to catch configuration mistakes:

```bash
babbel-bakeoff --suites safety,tools,latency
```

Then the full run:

```bash
make bakeoff
```

One candidate only: `babbel-bakeoff --only claude-haiku`.

What each test measures:

| Test | What | From |
|---|---|---|
| Safety | Does the model, as classifier, catch falls, emergencies, suicidal thoughts, scams, fear? Without false alarms? | `evals/safety_cases.yaml` |
| Tool calls | With Babbel's real prompt and tools: does it raise the right alarm, pause, forget, answer the family question, ask for help, call Lotte, and stay calm on "doodmoe" or "in slaap gevallen"? | `evals/tool_cases.yaml` |
| Conversation | Six simulated callers (Maria from Torhout, Roger with his back, Annie who repeats, Jos and the scam, lonely Kim, Georges in crisis), graded by the judge on warmth, one question per turn, no advice | `evals/personas.yaml` |
| Dialect | West- and East-Flemish transcripts (as speech recognition might write them): is the meaning understood, and is the right alarm raised? Each case runs **without and with** the dialect glossary, so you see whether the glossary helps | `evals/dialect_cases.yaml`, `src/babbel/locales/dialects/vlaams.yaml` |
| Memory | Summaries after a call: are the right facts kept, is the bank code never stored, does the private quarrel stay out of the family message? | `evals/memory_cases.yaml` |
| Latency | Time to the first word on live-call-sized requests | – |

The result is saved as `evals/results/bakeoff-<date>.md`, with every answer in the `.jsonl` next
to it. Run it twice on different days: results vary from run to run.

## Step 8: read the results and decide

Apply the rules in this order:

1. **Safety gate PASS** and **no missed `raise_safety_flag`** in tool calls. A miss rules a model
   out for the live call, however good it is otherwise. Read the miss in the `.jsonl`: if the
   model's reasoning was right but the tool was not called, the prompt may be the problem.
2. **First token p50 ≤ 0.5 s, p95 ≤ 0.8 s.** Slower makes the call feel laggy for an older person.
3. **Conversation and tool scores**: the highest wins. Read 2–3 transcripts yourself; you are the
   best judge of whether it sounds like a warm Flemish neighbour.
4. **Cost** within the €0.07/min all-in target (the model is only part of it).
5. **EU route available** for the pilot (NFR-04).

For **memory** (after the call) and the **judge**, use the strongest model, whatever wins the live
call: latency does not matter there, and one summary per senior per day is cheap.

Every mistake you find goes into the eval files as a new case (anonymised), so the next run
checks it. That is how the tests grow into the pilot's safety net.

## Step 9: record the decision

1. In `specs/spec.md`, update the defaults of D4 (language model) and D5 (speech to text), and add
   a change-log line with the date and a link to the bake-off result.
2. Set the chosen models in `.env` (`LLM_MODEL`, `LLM_SMALL_MODEL`, `STT_PROVIDER`, `STT_MODEL`).
3. Commit the bake-off `.md` result (not the recordings, not the `.jsonl`).

Re-run steps 5 and 7 whenever a prompt, model or provider changes.

## After this

- Voice (D6): a listening test of 2–3 nl-BE voices with a few older listeners.
- Claude or Gemini in the live agent: the agent uses OpenAI-compatible endpoints today; a native
  provider in `agent/providers.py` is a small change once the choice is made.
- The first real phone call over a Belgian number (`README.md`, Telephony setup).
