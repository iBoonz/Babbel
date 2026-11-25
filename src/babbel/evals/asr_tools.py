"""Helpers for the dialect speech-recognition benchmark (spec NFR-11, D5, FR-30 to FR-37).

Pure functions, used by asr/prepare_audio.py and asr/benchmark.py and covered by tests.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from babbel.core.safety import KeywordDetector

SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END = re.compile(r"silence_end:\s*([\d.]+)")


def parse_silencedetect(stderr: str) -> list[tuple[float, float]]:
    """Silences (start, end) in seconds from ffmpeg's silencedetect output."""
    starts = [max(0.0, float(x)) for x in SILENCE_START.findall(stderr)]
    ends = [float(x) for x in SILENCE_END.findall(stderr)]
    return list(zip(starts, ends))


def choose_cuts(silences: list[tuple[float, float]], duration: float,
                min_len: float = 8.0, max_len: float = 30.0) -> list[tuple[float, float]]:
    """Cut a recording into clips of min_len..max_len seconds, cutting in the middle of pauses
    so no word is split. Falls back to a hard cut at max_len when a stretch has no pause."""
    points = sorted((s + e) / 2 for s, e in silences if 0 < (s + e) / 2 < duration)
    clips, start = [], 0.0
    while duration - start > max_len:
        window = [p for p in points if start + min_len <= p <= start + max_len]
        cut = window[-1] if window else start + max_len
        clips.append((round(start, 2), round(cut, 2)))
        start = cut
    if duration - start >= min_len or not clips:
        clips.append((round(start, 2), round(duration, 2)))
    else:  # a short tail joins the previous clip
        clips[-1] = (clips[-1][0], round(duration, 2))
    return clips


def safety_misses(reference: str, hypothesis: str, detector: KeywordDetector) -> tuple[set, set]:
    """Safety rules the reference triggers, and those of them lost in the hypothesis.

    A clip where the senior says "ik ben gevallen" but the transcript reads "ik ben gebeld"
    is a miss: Babbel would never raise FR-30, however good the language model is."""
    expected = {f.rule_id for f in detector.detect(reference)}
    found = {f.rule_id for f in detector.detect(hypothesis)}
    return expected, expected - found


def read_csv_rows(path: str | Path) -> list[dict]:
    """Read a CSV saved by Excel, LibreOffice or a script: UTF-8 with or without BOM, comma- or
    semicolon-separated (Excel on a Belgian or Dutch Windows saves with semicolons)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    first = text.splitlines()[0] if text else ""
    delimiter = ";" if first.count(";") > first.count(",") else ","
    return [{(k or "").strip(): (v or "") for k, v in row.items()}
            for row in csv.DictReader(text.splitlines(), delimiter=delimiter)]


def write_csv_rows(path: str | Path, fields: list[str], rows: list[dict]) -> None:
    """Write a CSV that Excel opens correctly on a Belgian or Dutch Windows (semicolons, UTF-8
    with BOM). read_csv_rows reads it back, as do LibreOffice and Google Sheets."""
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)
