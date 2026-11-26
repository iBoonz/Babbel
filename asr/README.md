# Dialect speech recognition (spec NFR-11, D5)

Goal for the pilot: Babbel understands West- and East-Flemish speakers of 75+ well enough to
hold a conversation (WER ≤ 25%) and **never misses a safety phrase** (FR-30 to FR-37).

## Step 1: measure before you train (week 1–2)

1. Record 20–30 people (75+, WVL and OVL) with written consent (`Consent.kind = dialect_recording`).
   Use the phone line itself (8 kHz, real handsets), not a studio mic: that is what Babbel hears.
2. Per person ~10 minutes: free talk (their village, their work), plus a fixed list of
   safety sentences in their own words ("ik ben gevallen", "pijn op mijn borst", ...).
3. Transcribe in standard Dutch spelling into `asr/data/manifest.csv` (see `benchmark.py`).
4. Run the benchmark on 2–3 off-the-shelf models:

```bash
pip install -e ".[asr]"
python asr/benchmark.py asr/data/manifest.csv --model gpt-4o-transcribe
python asr/benchmark.py asr/data/manifest.csv --base-url http://localhost:8001/v1 --model large-v3
```

If the off-the-shelf model meets the gate, **do not train**. Use keyword biasing (names of
family and places are already passed as a prompt by `providers.build_stt`) and move on.

## Step 2: fine-tune only if the gate fails

- Base: Whisper large-v3 (or large-v3-turbo for latency), Dutch.
- Data: your recordings + public Flemish speech (CGN Flemish part via the Instituut voor de
  Nederlandse Taal, licence required; Common Voice nl, filtered on Belgian accents).
- Method: LoRA/PEFT fine-tune with Hugging Face `transformers` on one rented GPU
  (A100/H100, a few hours for ~50–100 h of audio). Keep a held-out set of speakers you never train on.
  `asr/finetune_whisper.py` does this from a manifest with a `recording` column: `--eval-only` measures
  the base model on the held-out recordings, a normal run trains a LoRA adapter and measures again.
  It also runs on a 16 GB AMD card under WSL (ROCm PyTorch) for small experiments.
- Compare models first: `asr/compare_models.py` runs Whisper, Qwen3-ASR, Voxtral and NVIDIA NeMo models
  locally on the same sample of clips (same audio, same normalisation, numbers spelled out) and prints
  word/character error rates per recording. `asr/finetune_qwen3.py` and `asr/finetune_nemo.py` train on the
  same split as `finetune_whisper.py`. On AMD, NeMo needs `MIOPEN_FIND_MODE=FAST`, and NeMo transducer
  (TDT) training is only practical on NVIDIA.
- Serve with faster-whisper behind an OpenAI-compatible `/v1/audio/transcriptions` endpoint,
  EU-hosted, then point `STT_PROVIDER`/`LLM_BASE_URL`-style settings at it.
- Re-run the benchmark and the safety eval (`babbel-evals --classifier`) before every switch.

## Privacy

Recordings are personal data. Store them encrypted, EU only, separate from call data, delete
on request, and never use pilot call audio (Babbel stores none: FR-11).
