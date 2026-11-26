"""Compare speech-to-text models that run locally on the same clips, per recording
(spec NFR-11, D5; asr/README.md).

Transcribe a fixed sample (the same clips for every model) with one model:
  python asr/compare_models.py MANIFEST --model whisper:openai/whisper-large-v3-turbo --out runs/cmp
Backends (model = backend:hf-id):
  whisper:<id>[@adapter-dir]   Hugging Face Whisper, optionally with a LoRA adapter (asr/finetune_whisper.py)
  qwen3:<id>                   Qwen3-ASR via `pip install qwen-asr`
  voxtral:<id>                 Voxtral (2507) in transcription mode
  voxtral-rt:<id>              Voxtral Realtime (2602)
  nemo:<id>                    NVIDIA NeMo ASR (Parakeet, Canary, Nemotron)
Then score every model in the folder side by side:
  python asr/compare_models.py MANIFEST --score runs/cmp

Scores are word and character error rates after babbel.evals.asr_tools.clean_transcript against the
`text` column (default gcnd_text, a dialect transcript), plus a word error rate against the standard-Dutch
`reference` column wherever one is filled in. Phone-line audio by default (what Babbel hears).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from babbel.evals.asr_tools import (  # noqa: E402
    clean_transcript,
    read_csv_rows,
    spell_numbers,
    tail_clips,
    training_clips,
)

SR = 16000


def sample(rows: list[dict], text_col: str, per_rec: int, seed: int, tail: float = 0.0) -> list[dict]:
    """The same clips for every model: per recording all rows with a standard-Dutch reference, topped up
    with a seeded random choice of the others (clips of 30 s or less with a usable transcript). With
    `tail`, only the last fraction of every recording (held out by finetune_qwen3.py --holdout-tail)."""
    usable = training_clips(rows, text_col, set())
    if tail:
        keep = tail_clips(usable, tail)
        usable = [r for r in usable if r["audio"] in keep]
    by_rec: dict[str, list[dict]] = {}
    for r in usable:
        by_rec.setdefault(r["recording"], []).append(r)
    out = []
    for rec in sorted(by_rec):
        clips = by_rec[rec]
        fixed = [r for r in clips if r.get("reference", "").strip()]
        rest = sorted((r for r in clips if not r.get("reference", "").strip()), key=lambda r: r["audio"])
        random.Random(f"{seed}-{rec}").shuffle(rest)
        out += fixed + rest[:max(0, per_rec - len(fixed))]
    return out


def load_audio(path: Path):
    import numpy as np
    import soundfile as sf
    wav, sr = sf.read(path, dtype="float32")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != SR:
        import torch
        import torchaudio
        wav = torchaudio.functional.resample(torch.from_numpy(wav), sr, SR).numpy()
    return np.ascontiguousarray(wav)


def make_backend(spec: str):
    """Returns (transcribe(list[Path]) -> list[str], batch size). Every backend decodes greedily, without
    prompts or word hints, and forces Dutch where the model allows it."""
    backend, _, model_id = spec.partition(":")
    if backend not in {"whisper", "qwen3", "voxtral", "voxtral-rt", "nemo"}:
        sys.exit(f"unknown backend {backend!r}")
    import torch

    if backend == "whisper":
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        model_id, _, adapter = model_id.partition("@")
        proc = WhisperProcessor.from_pretrained(model_id, language="dutch", task="transcribe")
        model = WhisperForConditionalGeneration.from_pretrained(model_id, attn_implementation="sdpa").to("cuda")
        if adapter:
            from peft import PeftModel
            model = PeftModel.from_pretrained(model, adapter).to("cuda")
        model.eval()

        def run(paths):
            feats = proc.feature_extractor([load_audio(p) for p in paths], sampling_rate=SR,
                                           return_tensors="pt").input_features.to("cuda")
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
                ids = model.generate(input_features=feats, language="nl", task="transcribe",
                                     max_new_tokens=220, num_beams=1)
            return proc.batch_decode(ids, skip_special_tokens=True)
        return run, 16

    if backend == "qwen3":
        from qwen_asr import Qwen3ASRModel
        model_id, _, adapter = model_id.partition("@")
        model = Qwen3ASRModel.from_pretrained(model_id, dtype=torch.bfloat16, device_map="cuda:0",
                                              max_inference_batch_size=16, max_new_tokens=256)
        if adapter:  # LoRA from asr/finetune_qwen3.py, merged into the decoder ("thinker")
            from peft import PeftModel
            model.model.thinker = PeftModel.from_pretrained(model.model.thinker, adapter).merge_and_unload()

        def run(paths):
            res = model.transcribe(audio=[str(p) for p in paths], language=["Dutch"] * len(paths))
            return [r.text for r in res]
        return run, 16

    if backend == "voxtral":
        from transformers import AutoProcessor, VoxtralForConditionalGeneration
        proc = AutoProcessor.from_pretrained(model_id)
        model = VoxtralForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch.bfloat16,
                                                                device_map="cuda")

        def run(paths):
            out = []
            for p in paths:
                inputs = proc.apply_transcription_request(language="nl", audio=str(p), model_id=model_id)
                inputs = inputs.to("cuda", dtype=torch.bfloat16)
                with torch.no_grad():
                    ids = model.generate(**inputs, max_new_tokens=300, do_sample=False)
                out.append(proc.batch_decode(ids[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)[0])
            return out
        return run, 8

    if backend == "voxtral-rt":
        from transformers import AutoProcessor, VoxtralRealtimeForConditionalGeneration
        proc = AutoProcessor.from_pretrained(model_id)
        model = VoxtralRealtimeForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch.bfloat16,
                                                                        device_map="cuda")

        def run(paths):
            out = []
            for p in paths:
                inputs = proc(load_audio(p), return_tensors="pt").to("cuda", dtype=torch.bfloat16)
                with torch.no_grad():
                    ids = model.generate(**inputs)
                out.append(proc.batch_decode(ids, skip_special_tokens=True)[0])
            return out
        return run, 8

    if backend == "nemo":
        import nemo.collections.asr as nemo_asr
        from omegaconf import open_dict
        # NeMo's CUDA-graph decoder needs NVIDIA driver libraries; switch it off so the same model also
        # loads on AMD (ROCm). Decoding stays greedy, as for every other backend.
        local = model_id.endswith(".nemo")  # a fine-tuned model from asr/finetune_nemo.py
        load = ((lambda **kw: nemo_asr.models.ASRModel.restore_from(model_id, **kw)) if local
                else (lambda **kw: nemo_asr.models.ASRModel.from_pretrained(model_name=model_id, **kw)))
        cfg = load(return_config=True)
        with open_dict(cfg):
            for key in ("decoding", "aux_ctc"):
                section = cfg.get(key)
                if section is not None and "greedy" in section:
                    section.greedy.use_cuda_graph_decoder = False
                if key == "aux_ctc" and section is not None and "decoding" in section \
                        and "greedy" in section.decoding:
                    section.decoding.greedy.use_cuda_graph_decoder = False
        model = load(override_config_path=cfg)
        for module in model.modules():
            # NeMo's fused Triton kernel for the first conv layers divides by zero on ROCm under WSL;
            # the plain PyTorch path computes the same thing.
            if type(module).__name__ == "MaskedConvSequential":
                module.fuse_triton = False
        model = model.to("cuda").eval()

        force = {"target_lang": "nl-NL"}  # prompt-conditioned models (Nemotron); others detect the language

        def run(paths):
            files = [str(p) for p in paths]
            try:
                hyps = model.transcribe(files, batch_size=16, verbose=False, **force)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                if force:
                    print(f"  this model takes no language setting ({type(exc).__name__}); automatic detection")
                    force.clear()
                hyps = model.transcribe(files, batch_size=16, verbose=False)
            if isinstance(hyps, tuple):
                hyps = hyps[0]
            texts = [h.text if hasattr(h, "text") else str(h) for h in hyps]
            return [re.sub(r"<[^>]+>", " ", t) for t in texts]  # drop language tags like <nl-NL>
        return run, 16

    sys.exit(f"unknown backend {backend!r}")


def transcribe(args) -> None:
    manifest = Path(args.manifest)
    rows = sample(read_csv_rows(manifest), args.text, args.per_recording, args.seed, args.tail)
    if args.recordings:
        keep = set(args.recordings.split(","))
        rows = [r for r in rows if r["recording"] in keep]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or re.sub(r"[^\w.-]+", "_", args.model.split(":", 1)[1].split("/")[-1].replace("@", "+"))
    out = out_dir / f"{name}.jsonl"
    done = {json.loads(line)["audio"] for line in out.open(encoding="utf-8")} if out.exists() else set()
    todo = [r for r in rows if r["audio"] not in done]
    print(f"{name}: {len(rows)} clips from {len({r['recording'] for r in rows})} recordings, {len(todo)} to do")
    if not todo:
        return
    run, batch = make_backend(args.model)
    with out.open("a", encoding="utf-8") as fh:
        for k in range(0, len(todo), batch):
            chunk = todo[k:k + batch]
            paths = [manifest.parent / (r.get(args.audio) or r["audio"]) for r in chunk]
            for r, hyp in zip(chunk, run(paths)):
                fh.write(json.dumps({"audio": r["audio"], "recording": r["recording"], "text": r[args.text],
                                     "reference": r.get("reference", ""), "hypothesis": hyp},
                                    ensure_ascii=False) + "\n")
            fh.flush()
            print(f"  {min(k + batch, len(todo))}/{len(todo)}", flush=True)


def norm(text: str) -> str:
    """The one normalisation every model and every reference goes through."""
    return clean_transcript(spell_numbers(text))


def score(args) -> None:
    import jiwer
    folder = Path(args.score)
    only = set(args.recordings.split(",")) if args.recordings else None
    files = sorted(folder.glob("*.jsonl"))
    loaded = {f.stem: [json.loads(line) for line in f.open(encoding="utf-8")
                       if only is None or json.loads(line)["recording"] in only] for f in files}
    # Fairness: every model is scored on exactly the same clips. A model that only ran on part of them
    # (e.g. a fine-tuned model, only fair on its held-out recordings) is left out here; score it with
    # --recordings set to the recordings it was not trained on.
    sets = {m: {r["audio"] for r in rows} for m, rows in loaded.items()}
    union = set().union(*sets.values()) if sets else set()
    partial = sorted(m for m, s in sets.items() if len(s) < len(union))
    for m in partial:
        print(f"skipped {m}: ran on {len(sets[m])} of {len(union)} clips (score it with --recordings)")
        loaded.pop(m)
    common = set.intersection(*(sets[m] for m in loaded)) if loaded else set()
    results: dict[str, dict] = {}
    for name, rows in loaded.items():
        rows = [r for r in rows if r["audio"] in common and (only is None or r["recording"] in only)]
        per: dict[str, tuple[list, list]] = {}
        std_r, std_h, made_up = [], [], 0
        for r in rows:
            ref, hyp = norm(r["text"]), norm(r["hypothesis"])
            refs, hyps = per.setdefault(r["recording"], ([], []))
            refs.append(ref)
            hyps.append(hyp)
            made_up += jiwer.wer(ref, hyp) > 1 if ref else 0
            if r.get("reference", "").strip():
                std_r.append(norm(r["reference"]))
                std_h.append(hyp)
        all_r = [x for a, _ in per.values() for x in a]
        all_h = [x for _, b in per.values() for x in b]
        results[name] = {
            "clips": len(rows), "wer": jiwer.wer(all_r, all_h), "cer": jiwer.cer(all_r, all_h),
            "made_up": made_up, "std_wer": jiwer.wer(std_r, std_h) if std_r else None, "std_clips": len(std_r),
            "per": {rec: (jiwer.wer(a, b), jiwer.cer(a, b)) for rec, (a, b) in per.items()}}
    models = sorted(results, key=lambda m: results[m]["cer"])
    print(f"\n{len(common)} clips transcribed by every model" + (f", scored on {sorted(only)}" if only else ""))
    print("\n| model | clips | WER | CER | made up (>100% WER) | WER vs standard Dutch |")
    print("|---|---|---|---|---|---|")
    for m in models:
        s = results[m]
        std = f"{s['std_wer']:.1%} ({s['std_clips']})" if s["std_wer"] is not None else "-"
        print(f"| {m} | {s['clips']} | {s['wer']:.1%} | {s['cer']:.1%} | {s['made_up']} | {std} |")
    recs = sorted({rec for s in results.values() for rec in s["per"]})
    print("\nCER per recording\n\n| recording | " + " | ".join(models) + " |")
    print("|---|" + "---|" * len(models))
    for rec in recs:
        cells = [f"{results[m]['per'][rec][1]:.0%}" if rec in results[m]["per"] else "-" for m in models]
        print(f"| {rec} | " + " | ".join(cells) + " |")
    suffix = "-" + "-".join(sorted(only)) if only else ""
    with (folder / f"scores{suffix}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "recording", "wer", "cer"])
        for m in models:
            for rec, (wer, cer) in sorted(results[m]["per"].items()):
                w.writerow([m, rec, f"{wer:.4f}", f"{cer:.4f}"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--model", help="backend:hf-id, e.g. whisper:openai/whisper-large-v3-turbo")
    ap.add_argument("--name", default="", help="output name (default: from the model id)")
    ap.add_argument("--out", default="asr/runs/compare")
    ap.add_argument("--score", default="", help="score every model in this folder instead of transcribing")
    ap.add_argument("--text", default="gcnd_text", help="column with the reference transcript")
    ap.add_argument("--audio", default="audio_phone", choices=["audio_phone", "audio"])
    ap.add_argument("--per-recording", type=int, default=80)
    ap.add_argument("--recordings", default="", help="comma-separated subset of recordings")
    ap.add_argument("--tail", type=float, default=0.0,
                    help="only the last fraction of every recording (pairs with finetune --holdout-tail)")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    if args.score:
        score(args)
    elif args.model:
        transcribe(args)
    else:
        sys.exit("give --model or --score")


if __name__ == "__main__":
    main()
