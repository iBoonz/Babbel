"""LoRA fine-tune of Whisper on a clip manifest, with recordings held out for testing
(spec NFR-11, D5; asr/README.md step 2).

Needs a GPU that PyTorch can use (CUDA or ROCm) and: pip install transformers peft soundfile jiwer
The manifest is the one from asr/prepare_audio.py, plus a `recording` column (clips of one
recording or speaker never end up on both sides of the split).

Baseline (Whisper as it comes) on the held-out recordings:
  python asr/finetune_whisper.py MANIFEST --test REC1,REC2 --text gcnd_text --eval-only
Train, then measure the same test set again:
  python asr/finetune_whisper.py MANIFEST --test REC1,REC2 --text gcnd_text --out runs/wvl1

Scores are word and character error rates after babbel.evals.asr_tools.clean_transcript, on
the phone-line copies by default (what Babbel hears). Train only on audio you may use.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

import jiwer
import numpy as np
import soundfile as sf
import torch
import torchaudio
from transformers import WhisperForConditionalGeneration, WhisperProcessor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from babbel.evals.asr_tools import clean_transcript, read_csv_rows  # noqa: E402

SR = 16000
MAX_SECONDS = 29.5  # Whisper sees 30 s windows


def load_audio(path: Path) -> np.ndarray:
    wav, sr = sf.read(path, dtype="float32")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != SR:
        wav = torchaudio.functional.resample(torch.from_numpy(wav), sr, SR).numpy()
    return wav


def select(rows: list[dict], text_col: str) -> list[dict]:
    out = []
    for r in rows:
        text = clean_transcript(r.get(text_col, ""))
        dur = float(r.get("source_end") or 0) - float(r.get("source_start") or 0)
        if text and 0 < dur <= MAX_SECONDS:
            out.append({**r, "text": text})
    return out


class Clips(torch.utils.data.Dataset):
    def __init__(self, rows, root: Path, processor, phone_share: float):
        self.rows, self.root, self.proc, self.phone_share = rows, root, processor, phone_share

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        r = self.rows[i]
        rel = r["audio_phone"] if r.get("audio_phone") and random.random() < self.phone_share else r["audio"]
        feats = self.proc.feature_extractor(load_audio(self.root / rel), sampling_rate=SR,
                                            return_tensors="np").input_features[0]
        return feats, self.proc.tokenizer(r["text"]).input_ids


def collate(batch, start_id: int):
    feats = torch.tensor(np.stack([b[0] for b in batch]))
    width = max(len(b[1]) for b in batch)
    labels = torch.full((len(batch), width), -100, dtype=torch.long)
    for i, (_, ids) in enumerate(batch):
        labels[i, :len(ids)] = torch.tensor(ids)
    if (labels[:, 0] == start_id).all():  # the model adds the start token itself
        labels = labels[:, 1:]
    return feats, labels


@torch.no_grad()
def evaluate(model, processor, rows, root: Path, audio_col: str, batch: int, log: Path) -> dict:
    model.eval()
    by_rec: dict[str, tuple[list[str], list[str]]] = {}
    with log.open("w", encoding="utf-8") as fh:
        for k in range(0, len(rows), batch):
            chunk = rows[k:k + batch]
            wavs = [load_audio(root / (r.get(audio_col) or r["audio"])) for r in chunk]
            feats = processor.feature_extractor(wavs, sampling_rate=SR, return_tensors="pt").input_features
            with torch.autocast("cuda", dtype=torch.bfloat16):
                ids = model.generate(input_features=feats.to("cuda"), language="nl", task="transcribe",
                                     max_new_tokens=220, num_beams=1)
            for r, hyp in zip(chunk, processor.batch_decode(ids, skip_special_tokens=True)):
                h = clean_transcript(hyp)
                refs, hyps = by_rec.setdefault(r["recording"], ([], []))
                refs.append(r["text"])
                hyps.append(h)
                fh.write(json.dumps({"audio": r["audio"], "recording": r["recording"], "reference": r["text"],
                                     "hypothesis": hyp, "wer": jiwer.wer(r["text"], h)}, ensure_ascii=False) + "\n")
    scores = {rec: {"clips": len(rf), "wer": jiwer.wer(rf, hy), "cer": jiwer.cer(rf, hy)}
              for rec, (rf, hy) in sorted(by_rec.items())}
    all_r = [x for rf, _ in by_rec.values() for x in rf]
    all_h = [x for _, hy in by_rec.values() for x in hy]
    scores["all"] = {"clips": len(all_r), "wer": jiwer.wer(all_r, all_h), "cer": jiwer.cer(all_r, all_h)}
    return scores


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--test", required=True, help="comma-separated recordings held out for testing")
    ap.add_argument("--text", default="reference", help="column with the target text")
    ap.add_argument("--model", default="openai/whisper-large-v3-turbo")
    ap.add_argument("--out", default="asr/runs/run1")
    ap.add_argument("--eval-only", action="store_true", help="only measure the base model")
    ap.add_argument("--adapter", default="", help="measure a trained LoRA adapter instead")
    ap.add_argument("--eval-audio", default="audio_phone", choices=["audio_phone", "audio"])
    ap.add_argument("--max-test", type=int, default=150, help="clips per test recording (0 = all)")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--phone-share", type=float, default=0.5, help="share of training clips heard as phone audio")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if not torch.cuda.is_available():
        sys.exit("no GPU visible to PyTorch (on WSL with ROCm: export HSA_ENABLE_DXG_DETECTION=1)")
    manifest = Path(args.manifest)
    root, out = manifest.parent, Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    test_recs = set(args.test.split(","))
    rows = select(read_csv_rows(manifest), args.text)
    train = [r for r in rows if r["recording"] not in test_recs]
    test = []
    for rec in sorted(test_recs):
        clips = [r for r in rows if r["recording"] == rec]
        random.Random(args.seed).shuffle(clips)
        test += clips[:args.max_test] if args.max_test else clips
    missing = test_recs - {r["recording"] for r in test}
    if missing:
        sys.exit(f"no clips for test recordings: {sorted(missing)}")
    print(f"train: {len(train)} clips from {len({r['recording'] for r in train})} recordings | "
          f"test: {len(test)} clips from {sorted(test_recs)}")

    processor = WhisperProcessor.from_pretrained(args.model, language="dutch", task="transcribe")
    model = WhisperForConditionalGeneration.from_pretrained(args.model, attn_implementation="sdpa").to("cuda")
    model.generation_config.forced_decoder_ids = None

    if args.adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, args.adapter).to("cuda")
    if args.eval_only or args.adapter:
        name = "adapter" if args.adapter else "baseline"
        scores = evaluate(model, processor, test, root, args.eval_audio, 16, out / f"{name}.jsonl")
        (out / f"{name}.json").write_text(json.dumps(scores, indent=2))
        report(name, scores)
        return

    from peft import LoraConfig, get_peft_model
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "out_proj", "fc1", "fc2"]))
    model.print_trainable_parameters()

    start_id = model.config.decoder_start_token_id
    loader = torch.utils.data.DataLoader(Clips(train, root, processor, args.phone_share), batch_size=args.batch,
                                         shuffle=True, num_workers=4, collate_fn=lambda b: collate(b, start_id))
    steps = math.ceil(len(loader) * args.epochs / args.accum)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr, weight_decay=0.01)
    warmup = max(1, steps // 20)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warmup) * max(0.0, (steps - s) / max(1, steps - warmup)))
    print(f"training {steps} optimiser steps ({args.epochs} epochs, batch {args.batch} x {args.accum})")

    model.train()
    step, micro, t0, running = 0, 0, time.time(), []
    while step < steps:
        for feats, labels in loader:
            with torch.autocast("cuda", dtype=torch.bfloat16):
                loss = model(input_features=feats.to("cuda"), labels=labels.to("cuda")).loss / args.accum
            loss.backward()
            running.append(loss.item() * args.accum)
            micro += 1
            if micro % args.accum:
                continue
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            step += 1
            if step % 20 == 0 or step == steps:
                print(f"step {step}/{steps}  loss {sum(running) / len(running):.3f}  "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)
                running = []
            if step >= steps:
                break

    model.save_pretrained(out / "adapter")
    model.config.use_cache = True
    scores = evaluate(model, processor, test, root, args.eval_audio, 16, out / "finetuned.jsonl")
    (out / "finetuned.json").write_text(json.dumps(scores, indent=2))
    report("fine-tuned", scores)


def report(name: str, scores: dict) -> None:
    print(f"\n{name}:")
    for rec, s in scores.items():
        print(f"  {rec:>8}: WER {s['wer']:.1%}  CER {s['cer']:.1%}  over {s['clips']} clips")


if __name__ == "__main__":
    main()
