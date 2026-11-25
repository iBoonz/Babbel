"""Turn your own recordings (video or audio) into benchmark clips (spec NFR-11, D5).

For every file in the input folder:
  1. extract the sound as 16 kHz mono (what a good microphone would give);
  2. cut it into 8-30 s clips at natural pauses, so no word is split;
  3. make a second copy of every clip that sounds like a phone line (8 kHz, 300-3400 Hz,
     mu-law), because that is what Babbel really hears;
  4. write asr/data/manifest.csv with an empty `reference` column for you to fill in.

Speakers get pseudonyms (S01, S02, ...). The file that links pseudonyms to the original file
names is written next to the manifest (speakers.csv) and must stay private, like the clips.
asr/data/ is in .gitignore: recordings never go into git.

Usage (needs ffmpeg):
  python asr/prepare_audio.py ~/Videos/oma --dialect wvl
  python asr/prepare_audio.py ~/Videos/opa --dialect ovl --append
  python asr/prepare_audio.py ~/Videos/oma --dialect wvl --draft elevenlabs   # pre-fill drafts

--draft runs one speech-to-text provider (see benchmark.py) and puts its text in the `draft`
column. Copy it to `reference` and correct it by ear: never benchmark against an uncorrected draft.
To add drafts later without cutting the clips again: python asr/benchmark.py asr/data/manifest.csv
--provider elevenlabs --fill-drafts

The manifest is written with semicolons and a UTF-8 marker, so Excel on a Belgian Windows opens
it in columns. Save it again as "CSV (puntkomma-gescheiden)" or "CSV UTF-8"; both are read back.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zlib
from pathlib import Path

from babbel.config import load_env_file
from babbel.evals.asr_tools import choose_cuts, parse_silencedetect, read_csv_rows, write_csv_rows

MEDIA = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".mp3", ".m4a", ".wav", ".aac",
         ".ogg", ".flac", ".wma", ".3gp"}
FIELDS = ["audio", "audio_phone", "reference", "draft", "dialect", "speaker", "source_start",
          "source_end"]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def duration_of(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
               "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.stdout.strip() or 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="folder with videos or audio files")
    ap.add_argument("--out", default="asr/data", help="output folder (default asr/data)")
    ap.add_argument("--dialect", default="std", help="wvl | ovl | ant | lim | vbr | std")
    ap.add_argument("--min", type=float, default=8.0, help="shortest clip in seconds")
    ap.add_argument("--max", type=float, default=30.0, help="longest clip in seconds")
    ap.add_argument("--noise-db", default="-32dB", help="silence threshold for cutting")
    ap.add_argument("--append", action="store_true", help="add to an existing manifest")
    ap.add_argument("--draft", default="", help="provider for draft transcripts (see benchmark.py)")
    ap.add_argument("--draft-model", default="", help="model for --draft")
    args = ap.parse_args()

    load_env_file()
    if run(["ffmpeg", "-version"]).returncode != 0:
        sys.exit("ffmpeg not found: install it first (https://ffmpeg.org, or: winget install ffmpeg)")
    src, out = Path(args.input), Path(args.out)
    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in MEDIA)
    if not files:
        sys.exit(f"no video or audio files in {src}")
    for sub in ("full", "clips/wide", "clips/phone"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    manifest, speakers_csv = out / "manifest.csv", out / "speakers.csv"
    rows, speakers = [], {}
    if args.append and manifest.exists():
        rows = read_csv_rows(manifest)
    if speakers_csv.exists():
        speakers = {r["source"]: r["speaker"] for r in read_csv_rows(speakers_csv)}
    elif not args.append and manifest.exists():
        sys.exit(f"{manifest} exists: use --append, or move it away first")

    drafter = None
    if args.draft:
        sys.path.insert(0, str(Path(__file__).parent))
        from benchmark import make_transcriber  # noqa: E402

        drafter = make_transcriber(args.draft, args.draft_model)

    for f in files:
        spk = speakers.setdefault(str(f.resolve()), f"S{len(speakers) + 1:02d}")
        full = out / "full" / f"{spk}-{zlib.crc32(str(f.resolve()).encode()) % 10000:04d}.wav"
        r = run(["ffmpeg", "-y", "-v", "error", "-i", str(f), "-vn", "-ac", "1", "-ar", "16000",
                 str(full)])
        if r.returncode != 0:
            print(f"skip {f.name}: {r.stderr.strip()[:200]}")
            continue
        dur = duration_of(full)
        sd = run(["ffmpeg", "-i", str(full), "-af", f"silencedetect=noise={args.noise_db}:d=0.4",
                  "-f", "null", "-"])
        cuts = choose_cuts(parse_silencedetect(sd.stderr), dur, args.min, args.max)
        print(f"{f.name} -> {spk}: {dur:.0f} s, {len(cuts)} clips")
        for i, (a, b) in enumerate(cuts, 1):
            name = f"{full.stem}-{i:03d}.wav"
            wide, phone = out / "clips/wide" / name, out / "clips/phone" / name
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(a), "-to", str(b), "-i", str(full),
                 "-c:a", "pcm_s16le", str(wide)])
            tmp = phone.with_suffix(".mulaw.wav")
            run(["ffmpeg", "-y", "-v", "error", "-i", str(wide), "-af",
                 "highpass=f=300,lowpass=f=3400", "-ar", "8000", "-c:a", "pcm_mulaw", str(tmp)])
            run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp), "-c:a", "pcm_s16le", str(phone)])
            tmp.unlink(missing_ok=True)
            draft = ""
            if drafter:
                try:
                    draft = drafter(wide)
                except Exception as exc:  # keep going; drafts are a convenience
                    print(f"  draft failed for {name}: {exc}")
            rows.append({"audio": str(wide.relative_to(out)), "audio_phone": str(phone.relative_to(out)),
                         "reference": "", "draft": draft, "dialect": args.dialect, "speaker": spk,
                         "source_start": a, "source_end": b})

    write_csv_rows(manifest, FIELDS, rows)
    write_csv_rows(speakers_csv, ["speaker", "source"],
                   [{"speaker": s, "source": p} for p, s in speakers.items()])
    print(f"\n{len(rows)} clips in {manifest}. Next: fill in `reference` for each clip "
          f"(what was really said, standard Dutch spelling).")


if __name__ == "__main__":
    main()
