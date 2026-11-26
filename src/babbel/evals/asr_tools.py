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


TRANSCRIPT_NOISE = re.compile(r"\?\?\?|\bxxx\b|\bggg\b|\[[^\]]*\]")
FILLERS = {"uh", "uhm", "eh", "ehm", "hm", "mm", "mhm", "mm-hu"}


def clean_transcript(text: str) -> str:
    """Comparable text from a dialect transcript or a model's output (NFR-11): lowercase, no
    punctuation, no fillers, and the GCND corpus marks removed: `·` joins a clitic ("k·zeggen"),
    `???`/`xxx` is unintelligible, `ggg` is laughter or noise. Training targets and both sides
    of a word error rate go through the same function."""
    text = text.lower().replace("’", "'").replace("·", " ")
    text = TRANSCRIPT_NOISE.sub(" ", text)
    text = re.sub(r"[^\w\s'-]", " ", text)
    words = (w.strip("'-") for w in text.split())
    return " ".join(w for w in words if w and w not in FILLERS)


def training_clips(rows: list[dict], text_col: str, held_out: set[str],
                   max_seconds: float = 29.5) -> list[dict]:
    """Clips to fine-tune on (NFR-11): a usable transcript in `text_col`, at most `max_seconds` long,
    and never from a held-out recording, so no speaker ends up on both sides of the split. Each row
    gets `text`, the cleaned target. The same rule for every model keeps the comparison fair."""
    out = []
    for r in rows:
        if r.get("recording") in held_out:
            continue
        text = clean_transcript(r.get(text_col, ""))
        dur = float(r.get("source_end") or 0) - float(r.get("source_start") or 0)
        if text and 0 < dur <= max_seconds:
            out.append({**r, "text": text})
    return out


def tail_clips(rows: list[dict], fraction: float) -> set[str]:
    """Audio paths of the last `fraction` of every recording, in time order (NFR-11). Held out as one
    block per recording, so the model has heard every dialect but never these sentences, and
    neighbouring sentences (same topic, same names) cannot leak into training."""
    by_rec: dict[str, list[dict]] = {}
    for r in rows:
        by_rec.setdefault(r.get("recording", ""), []).append(r)
    out: set[str] = set()
    for clips in by_rec.values():
        clips = sorted(clips, key=lambda r: float(r.get("source_start") or 0))
        cut = len(clips) - round(len(clips) * fraction)
        out.update(r["audio"] for r in clips[cut:])
    return out


def spell_numbers(text: str, lang: str = "nl") -> str:
    """Write digits as words ("1910" -> "negentienhonderdtien") so a model that writes digits and a
    transcript that spells numbers out are compared fairly (NFR-11). Needs `num2words`; without it the
    text is returned unchanged."""
    try:
        from num2words import num2words
    except ImportError:  # pragma: no cover
        return text

    def words(m: re.Match) -> str:
        try:
            return f" {num2words(int(m.group(0)), lang=lang)} "
        except (OverflowError, NotImplementedError, ValueError):
            return m.group(0)
    return re.sub(r"\d+", words, text)


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
