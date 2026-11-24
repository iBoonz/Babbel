"""Dialect ASR benchmark (spec NFR-08, D5).

Measures word error rate (WER) per dialect on your own recordings, for any OpenAI-compatible
transcription endpoint (OpenAI, Azure OpenAI, a self-hosted faster-whisper server, ...).

manifest.csv columns:  audio,reference,dialect,speaker
  audio      path to a wav/mp3/m4a file, relative to the manifest
  reference  what was really said, written in standard Dutch spelling
  dialect    wvl | ovl | ant | lim | vbr | std
  speaker    pseudonym (never a real name)

Usage:
  pip install -e ".[asr]"
  python asr/benchmark.py asr/data/manifest.csv --model gpt-4o-transcribe
  python asr/benchmark.py asr/data/manifest.csv --base-url http://localhost:8001/v1 --model whisper-large-v3-nl-flemish

Pilot gate (spec): WER <= 25% on West- and East-Flemish speakers 75+, and no safety phrase missed.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import httpx

try:
    import jiwer
except ImportError:  # pragma: no cover
    sys.exit("pip install jiwer (or: pip install -e '.[asr]')")

PROMPT = "Vlaams gesprek met een oudere persoon. Namen: {names}."


def normalise(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(text.split())


def transcribe(client: httpx.Client, base_url: str, model: str, path: Path, names: str) -> str:
    with path.open("rb") as fh:
        resp = client.post(
            f"{base_url.rstrip('/')}/audio/transcriptions",
            data={"model": model, "language": "nl", "prompt": PROMPT.format(names=names or "-")},
            files={"file": (path.name, fh)},
        )
    resp.raise_for_status()
    return resp.json().get("text", "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--base-url", default=os.environ.get("STT_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--model", default=os.environ.get("STT_MODEL", "gpt-4o-transcribe"))
    ap.add_argument("--names", default="", help="comma-separated names to bias recognition")
    ap.add_argument("--out", default="asr/results.jsonl")
    args = ap.parse_args()

    manifest = Path(args.manifest)
    key = os.environ.get("STT_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    client = httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=120)
    refs: dict[str, list[str]] = defaultdict(list)
    hyps: dict[str, list[str]] = defaultdict(list)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open(encoding="utf-8") as fh, out.open("w", encoding="utf-8") as log:
        for row in csv.DictReader(fh):
            hyp = transcribe(client, args.base_url, args.model, manifest.parent / row["audio"], args.names)
            ref_n, hyp_n = normalise(row["reference"]), normalise(hyp)
            refs[row["dialect"]].append(ref_n)
            hyps[row["dialect"]].append(hyp_n)
            log.write(json.dumps({**row, "hypothesis": hyp, "wer": jiwer.wer(ref_n, hyp_n)},
                                 ensure_ascii=False) + "\n")

    print(f"model: {args.model}")
    all_r, all_h = [], []
    for dialect in sorted(refs):
        wer = jiwer.wer(refs[dialect], hyps[dialect])
        all_r += refs[dialect]
        all_h += hyps[dialect]
        print(f"  {dialect:>4}: WER {wer:.1%} over {len(refs[dialect])} clips")
    if all_r:
        print(f"   all: WER {jiwer.wer(all_r, all_h):.1%}")
    print(f"details: {out}")


if __name__ == "__main__":
    main()
