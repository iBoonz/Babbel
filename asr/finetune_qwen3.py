"""LoRA fine-tune of Qwen3-ASR on a clip manifest, with recordings held out (spec NFR-11, D5).

Same split, targets, epochs and phone/wide mix as asr/finetune_whisper.py, so the two compare fairly.
Needs `pip install qwen-asr peft soundfile` and a GPU PyTorch can use. Measure the result with
  python asr/compare_models.py MANIFEST --model qwen3:Qwen/Qwen3-ASR-1.7B@OUT/adapter --recordings REC1,REC2
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_models import load_audio  # noqa: E402

from babbel.evals.asr_tools import read_csv_rows, tail_clips, training_clips  # noqa: E402

END = "<|im_end|>"  # Qwen chat turns end here; generation stops on it


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--test", default="", help="comma-separated recordings held out entirely")
    ap.add_argument("--holdout-tail", type=float, default=0.0,
                    help="also hold out the last fraction of every recording (e.g. 0.3)")
    ap.add_argument("--text", default="gcnd_text")
    ap.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
    ap.add_argument("--out", default="asr/runs/qwen3")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--phone-share", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    manifest, out = Path(args.manifest), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = read_csv_rows(manifest)
    held_out = set(filter(None, args.test.split(",")))
    train = training_clips(rows, args.text, held_out)
    if args.holdout_tail:  # same tail as compare_models.py --tail, computed over the same usable clips
        tail = tail_clips(training_clips(rows, args.text, set()), args.holdout_tail)
        train = [r for r in train if r["audio"] not in tail]
    if not held_out and not args.holdout_tail:
        sys.exit("hold something out: --test and/or --holdout-tail")
    print(f"train: {len(train)} clips from {len({r['recording'] for r in train})} recordings")

    from peft import LoraConfig, get_peft_model
    from qwen_asr import Qwen3ASRModel
    qa = Qwen3ASRModel.from_pretrained(args.model, dtype=torch.bfloat16, device_map="cuda:0")
    proc, thinker = qa.processor, qa.model.thinker
    prompt = qa._build_text_prompt(context="", force_language="Dutch")  # exactly the inference prompt
    thinker.config.use_cache = False
    thinker.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    thinker.enable_input_require_grads()
    thinker = get_peft_model(thinker, LoraConfig(r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.05,
                                                 target_modules="all-linear"))
    thinker.print_trainable_parameters()

    def batch_inputs(rows):
        wavs = []
        for r in rows:
            rel = r["audio_phone"] if r.get("audio_phone") and random.random() < args.phone_share else r["audio"]
            wavs.append(load_audio(manifest.parent / rel))
        full = proc(text=[prompt + r["text"] + END for r in rows], audio=wavs, return_tensors="pt", padding=True)
        bare = proc(text=[prompt] * len(rows), audio=wavs, return_tensors="pt", padding=True)
        labels = full["input_ids"].clone()
        labels[full["attention_mask"] == 0] = -100
        for i in range(len(rows)):  # learn only the transcript, never the prompt or the audio tokens
            start = int(full["attention_mask"][i].argmax())  # first real token (handles left padding)
            labels[i, start:start + int(bare["attention_mask"][i].sum())] = -100
        full["labels"] = labels
        return {k: (v.to("cuda").to(torch.bfloat16) if v.is_floating_point() else v.to("cuda"))
                for k, v in full.items()}

    per_epoch = math.ceil(len(train) / args.batch)
    steps = math.ceil(per_epoch * args.epochs / args.accum)
    opt = torch.optim.AdamW([p for p in thinker.parameters() if p.requires_grad], lr=args.lr, weight_decay=0.01)
    warmup = max(1, steps // 20)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warmup) * max(0.0, (steps - s) / max(1, steps - warmup)))
    print(f"training {steps} optimiser steps ({args.epochs} epochs, batch {args.batch} x {args.accum})")
    thinker.train()
    step, micro, t0, running = 0, 0, time.time(), []
    while step < steps:
        order = train[:]
        random.shuffle(order)
        for k in range(0, len(order), args.batch):
            loss = thinker(**batch_inputs(order[k:k + args.batch])).loss / args.accum
            loss.backward()
            running.append(loss.item() * args.accum)
            micro += 1
            if micro % args.accum:
                continue
            torch.nn.utils.clip_grad_norm_(thinker.parameters(), 1.0)
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
    thinker.save_pretrained(out / "adapter")
    print(f"adapter saved to {out / 'adapter'}")


if __name__ == "__main__":
    main()
