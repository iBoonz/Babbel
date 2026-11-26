"""Dialect speech-recognition benchmark (spec NFR-11, D5, FR-30 to FR-37).

Measures, on your own recordings:
  - word error rate (WER) per dialect;
  - safety phrases lost: clips where the reference triggers a safety rule (FR-30 to FR-37)
    and the transcript no longer does. One lost "ik ben gevallen" matters more than 5% WER.

manifest.csv (made by asr/prepare_audio.py, or by hand):
  audio        path to a wav/mp3/m4a file, relative to the manifest
  audio_phone  optional: the same clip as phone-line audio (used with --phone)
  reference    what was really said, in standard Dutch spelling (rows without one are skipped)
  dialect      wvl | ovl | ant | lim | vbr | std
  speaker      pseudonym (never a real name)

Providers (--provider), key from the environment variable in brackets:
  openai       any OpenAI-compatible /audio/transcriptions endpoint      [STT_API_KEY or OPENAI_API_KEY]
  elevenlabs   ElevenLabs Speech to Text (Scribe)                        [ELEVENLABS_API_KEY]
  deepgram     Deepgram pre-recorded                                     [DEEPGRAM_API_KEY]
  azure        Azure AI Speech fast transcription, locale nl-BE          [AZURE_SPEECH_KEY, --region]
  speechmatics Speechmatics batch                                         [SPEECHMATICS_API_KEY]
  local        Whisper on this PC via faster-whisper: no key, audio never leaves the PC
               (pip install -e ".[asr-local]"; --model large-v3-turbo | large-v3 | medium ...)

The request shapes for elevenlabs, deepgram, azure and speechmatics were written from memory and
not yet run against the live APIs: if a provider returns an error on the first run, check its
API reference and adjust the small function for it below.

Usage:
  pip install -e ".[asr]"
  python asr/benchmark.py asr/data/manifest.csv --provider elevenlabs --phone
  python asr/benchmark.py asr/data/manifest.csv --provider openai --model gpt-4o-transcribe --phone
  python asr/benchmark.py asr/data/manifest.csv --provider azure --region westeurope --phone

Pilot gate (spec NFR-11): WER <= 25% per dialect group on phone audio, and no safety phrase lost.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

try:
    import jiwer
except ImportError:  # pragma: no cover
    sys.exit("pip install jiwer (or: pip install -e '.[asr]')")

from babbel.config import load_env_file
from babbel.core.dialect import stt_hints
from babbel.core.safety import KeywordDetector
from babbel.evals.asr_tools import read_csv_rows, safety_misses, write_csv_rows

PROMPT = "Vlaams gesprek met een oudere persoon. Namen: {names}."
HINTS = " Woorden: {hints}."  # FR-19: dialect forms from locales/dialects/vlaams.yaml
DEFAULT_MODELS = {"openai": "gpt-4o-transcribe", "elevenlabs": "scribe_v1", "deepgram": "nova-3",
                  "azure": "fast", "speechmatics": "enhanced", "local": "large-v3-turbo"}


def normalise(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(text.split())


def _key(*names: str) -> str:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    sys.exit(f"set one of: {', '.join(names)}")


def make_transcriber(provider: str, model: str = "", *, base_url: str = "", region: str = "",
                     names: str = "", hints: list[str] | None = None):
    """Returns a function path -> transcript text for one provider."""
    model = model or DEFAULT_MODELS.get(provider, "")
    prompt = PROMPT.format(names=names or "-") + (HINTS.format(hints=", ".join(hints)) if hints else "")

    if provider == "local":
        # NFR-11 baseline that needs no account: the audio stays on this machine.
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            sys.exit('pip install -e ".[asr-local]"  (installs faster-whisper)')
        device = os.environ.get("WHISPER_DEVICE", "auto")
        compute = os.environ.get("WHISPER_COMPUTE", "int8" if device in ("auto", "cpu") else "default")
        print(f"loading Whisper {model} on {device} ({compute}); the first run downloads the model")
        whisper = WhisperModel(model, device=device, compute_type=compute)

        def run(path: Path) -> str:
            # Decode with ffmpeg (16 kHz mono float), not faster-whisper's own PyAV reader, which
            # breaks on newer PyAV releases.
            import subprocess

            import numpy as np
            pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
                                  "-f", "f32le", "-"], capture_output=True, check=True).stdout
            audio = np.frombuffer(pcm, dtype=np.float32)
            segments, _ = whisper.transcribe(audio, language="nl", beam_size=5,
                                             initial_prompt=prompt, condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments)
        return run

    http = httpx.Client(timeout=300)

    if provider == "openai":
        url = (base_url or os.environ.get("STT_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        key = _key("STT_API_KEY", "OPENAI_API_KEY")

        def run(path: Path) -> str:
            with path.open("rb") as fh:
                r = http.post(f"{url}/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                              data={"model": model, "language": "nl", "prompt": prompt},
                              files={"file": (path.name, fh)})
            r.raise_for_status()
            return r.json().get("text", "")
        return run

    if provider == "elevenlabs":
        url = (base_url or "https://api.elevenlabs.io").rstrip("/")
        key = _key("ELEVENLABS_API_KEY")

        def run(path: Path) -> str:
            with path.open("rb") as fh:
                r = http.post(f"{url}/v1/speech-to-text", headers={"xi-api-key": key},
                              data={"model_id": model, "language_code": "nl"},
                              files={"file": (path.name, fh)})
            r.raise_for_status()
            return r.json().get("text", "")
        return run

    if provider == "deepgram":
        url = (base_url or "https://api.deepgram.com").rstrip("/")  # EU: https://api.eu.deepgram.com
        key = _key("DEEPGRAM_API_KEY")

        def run(path: Path) -> str:
            r = http.post(f"{url}/v1/listen", params={"model": model, "language": "nl",
                                                      "smart_format": "true"},
                          headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
                          content=path.read_bytes())
            r.raise_for_status()
            return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        return run

    if provider == "azure":
        if not region and not base_url:
            sys.exit("azure needs --region (e.g. westeurope) or --base-url")
        url = (base_url or f"https://{region}.api.cognitive.microsoft.com").rstrip("/")
        key = _key("AZURE_SPEECH_KEY")

        def run(path: Path) -> str:
            with path.open("rb") as fh:
                r = http.post(f"{url}/speechtotext/transcriptions:transcribe",
                              params={"api-version": "2024-11-15"},
                              headers={"Ocp-Apim-Subscription-Key": key},
                              files={"audio": (path.name, fh),
                                     "definition": (None, json.dumps({"locales": ["nl-BE"]}))})
            r.raise_for_status()
            phrases = r.json().get("combinedPhrases") or [{}]
            return phrases[0].get("text", "")
        return run

    if provider == "speechmatics":
        url = (base_url or "https://eu1.asr.api.speechmatics.com").rstrip("/")
        key = _key("SPEECHMATICS_API_KEY")
        auth = {"Authorization": f"Bearer {key}"}
        config = {"type": "transcription",
                  "transcription_config": {"language": "nl", "operating_point": model}}

        def run(path: Path) -> str:
            with path.open("rb") as fh:
                r = http.post(f"{url}/v2/jobs", headers=auth,
                              files={"data_file": (path.name, fh),
                                     "config": (None, json.dumps(config))})
            r.raise_for_status()
            job = r.json()["id"]
            for _ in range(120):
                status = http.get(f"{url}/v2/jobs/{job}", headers=auth).json()["job"]["status"]
                if status == "done":
                    break
                if status in ("rejected", "deleted", "expired"):
                    raise RuntimeError(f"speechmatics job {job}: {status}")
                time.sleep(2)
            r = http.get(f"{url}/v2/jobs/{job}/transcript", params={"format": "txt"}, headers=auth)
            r.raise_for_status()
            return r.text
        return run

    sys.exit(f"unknown provider {provider!r}; choose from {', '.join(DEFAULT_MODELS)}")


def fill_drafts(manifest: Path, transcribe) -> None:
    """Pre-fill the `draft` column (wide-band audio), so writing references is correcting, not
    typing. Rows that already have a draft or a reference are left alone."""
    rows = read_csv_rows(manifest)
    fields = list(rows[0].keys()) if rows else []
    if "draft" not in fields:
        fields.append("draft")
    done = 0
    for row in rows:
        if row.get("draft", "").strip() or row.get("reference", "").strip():
            continue
        try:
            row["draft"] = transcribe(manifest.parent / row["audio"])
            done += 1
        except Exception as exc:
            print(f"  failed {row['audio']}: {exc}")
        write_csv_rows(manifest, fields, rows)  # save after every clip: safe to stop and resume
    print(f"{done} drafts added to {manifest}. Copy each draft to `reference` and correct it by ear.")


def main() -> None:
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--provider", default=os.environ.get("STT_PROVIDER", "openai"))
    ap.add_argument("--model", default=os.environ.get("STT_MODEL", ""))
    ap.add_argument("--base-url", default="")
    ap.add_argument("--region", default="", help="Azure region, e.g. westeurope")
    ap.add_argument("--phone", action="store_true", help="use the phone-line copies (audio_phone)")
    ap.add_argument("--names", default="", help="comma-separated names to bias recognition")
    ap.add_argument("--locale", default="nl-BE", help="locale pack for the safety rules")
    ap.add_argument("--fill-drafts", action="store_true",
                    help="only fill the empty `draft` column of the manifest with this provider, then stop")
    ap.add_argument("--dialect-hints", default="", metavar="REGION",
                    help="wvl or ovl: pass dialect words from the lexicon as hints (openai provider only)")
    args = ap.parse_args()

    manifest = Path(args.manifest)
    model = args.model or DEFAULT_MODELS.get(args.provider, "")
    hints = stt_hints(args.dialect_hints) if args.dialect_hints else None
    if args.fill_drafts:
        fill_drafts(manifest, make_transcriber(args.provider, model, base_url=args.base_url,
                                               region=args.region, names=args.names, hints=hints))
        return
    if hints and args.provider not in ("openai", "local"):
        print("note: --dialect-hints is only passed to the openai and local providers for now")
    transcribe = make_transcriber(args.provider, model, base_url=args.base_url,
                                  region=args.region, names=args.names, hints=hints)
    detector = KeywordDetector(args.locale)
    refs: dict[str, list[str]] = defaultdict(list)
    hyps: dict[str, list[str]] = defaultdict(list)
    safety_total, safety_lost, skipped, failed = 0, [], 0, 0
    label = (f"{args.provider}-{model}{'-phone' if args.phone else ''}"
             f"{'-hints' if hints else ''}").replace("/", "_")
    out = Path("asr/results") / f"{label}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8") as log:
        for row in read_csv_rows(manifest):
            if not row.get("reference", "").strip():
                skipped += 1
                continue
            rel = row.get("audio_phone") if args.phone and row.get("audio_phone") else row["audio"]
            try:
                hyp = transcribe(manifest.parent / rel)
            except Exception as exc:
                failed += 1
                print(f"  failed {rel}: {exc}")
                continue
            ref_n, hyp_n = normalise(row["reference"]), normalise(hyp)
            refs[row["dialect"]].append(ref_n)
            hyps[row["dialect"]].append(hyp_n)
            expected, lost = safety_misses(row["reference"], hyp, detector)
            safety_total += len(expected)
            if lost:
                safety_lost.append(f"{rel}: {sorted(lost)}")
            log.write(json.dumps({**row, "used_audio": rel, "hypothesis": hyp,
                                  "wer": jiwer.wer(ref_n, hyp_n) if ref_n else None,
                                  "safety_expected": sorted(expected), "safety_lost": sorted(lost)},
                                 ensure_ascii=False) + "\n")

    print(f"\n{args.provider} / {model} ({'phone audio' if args.phone else 'wide-band audio'})")
    all_r, all_h = [], []
    for dialect in sorted(refs):
        wer = jiwer.wer(refs[dialect], hyps[dialect])
        all_r += refs[dialect]
        all_h += hyps[dialect]
        print(f"  {dialect:>4}: WER {wer:.1%} over {len(refs[dialect])} clips {'ok' if wer <= 0.25 else 'FAIL'}")
    if all_r:
        print(f"   all: WER {jiwer.wer(all_r, all_h):.1%}")
    kept = safety_total - len(safety_lost)
    print(f"  safety phrases kept: {kept}/{safety_total} {'ok' if not safety_lost else 'FAIL'}")
    for item in safety_lost:
        print(f"   - lost {item}")
    if skipped:
        print(f"  skipped {skipped} clips without a reference transcript")
    if failed:
        print(f"  {failed} clips failed (see above)")
    print(f"details: {out}")


if __name__ == "__main__":
    main()
