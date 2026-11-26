"""Fine-tune an NVIDIA NeMo ASR model (e.g. Parakeet-TDT v3) on a clip manifest, with recordings held
out (spec NFR-11, D5).

Same split, targets, epochs and phone/wide mix as asr/finetune_whisper.py, so they compare fairly.
Full fine-tuning (the usual way for a 0.6B NeMo model) with the pure-PyTorch TDT loss, which also runs
on AMD (ROCm); on WSL set MIOPEN_FIND_MODE=FAST. Measure the result with
  python asr/compare_models.py MANIFEST --model nemo:OUT/model.nemo --recordings REC1,REC2
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from babbel.evals.asr_tools import read_csv_rows, training_clips  # noqa: E402


def amd_safe(cfg) -> None:
    """Config tweak so a NeMo transducer loads on ROCm: no CUDA-graph decoder (needs NVIDIA libraries)."""
    from omegaconf import open_dict
    with open_dict(cfg):
        if cfg.get("decoding") is not None and "greedy" in cfg.decoding:
            cfg.decoding.greedy.use_cuda_graph_decoder = False


def pytorch_loss(model, cfg) -> None:
    """Swap NeMo's TDT loss (numba CUDA kernels, NVIDIA only) for its pure-PyTorch twin with the same
    durations and sigma. Done after loading, because the loss name also decides the shape of the joint
    network, which must keep its duration outputs."""
    from nemo.collections.asr.losses.rnnt import RNNTLoss
    if cfg.get("loss") is None or cfg.loss.get("loss_name") != "tdt":
        return
    kw = cfg.loss.get("tdt_kwargs", {})
    # vocabulary size without blank and without the duration outputs, as NeMo computes it for TDT
    vocab = model.joint.num_classes_with_blank - 1 - model.joint.num_extra_outputs
    model.loss = RNNTLoss(num_classes=vocab, loss_name="tdt_pytorch",
                          loss_kwargs={"durations": list(kw["durations"]), "sigma": kw.get("sigma", 0.0)},
                          reduction=cfg.get("rnnt_reduction", "mean_batch"))
    if hasattr(model.joint, "set_loss"):
        model.joint.set_loss(model.loss)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--test", required=True, help="comma-separated recordings held out")
    ap.add_argument("--text", default="gcnd_text")
    ap.add_argument("--model", default="nvidia/parakeet-tdt-0.6b-v3")
    ap.add_argument("--out", default="asr/runs/nemo")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--phone-share", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-steps", type=int, default=-1, help="stop early (smoke test); -1 = full epochs")
    args = ap.parse_args()

    import lightning.pytorch as pl
    import nemo.collections.asr as nemo_asr
    import soundfile as sf
    from omegaconf import OmegaConf

    random.seed(args.seed)
    pl.seed_everything(args.seed)
    manifest, out = Path(args.manifest), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    train = training_clips(read_csv_rows(manifest), args.text, set(args.test.split(",")))
    train_manifest = out / "train_manifest.json"
    with train_manifest.open("w", encoding="utf-8") as fh:
        for r in train:  # one phone-or-wide copy per clip, the same 50/50 mix as the other scripts
            rel = r["audio_phone"] if r.get("audio_phone") and random.random() < args.phone_share else r["audio"]
            path = (manifest.parent / rel).resolve()
            fh.write(json.dumps({"audio_filepath": str(path), "duration": sf.info(str(path)).duration,
                                 "text": r["text"]}, ensure_ascii=False) + "\n")
    print(f"train: {len(train)} clips from {len({r['recording'] for r in train})} recordings -> {train_manifest}")

    cfg = nemo_asr.models.ASRModel.from_pretrained(model_name=args.model, return_config=True)
    amd_safe(cfg)
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=args.model, override_config_path=cfg)
    pytorch_loss(model, cfg)
    for module in model.modules():
        if type(module).__name__ == "MaskedConvSequential":
            module.fuse_triton = False

    model.setup_training_data(train_data_config=OmegaConf.create({
        "manifest_filepath": str(train_manifest), "sample_rate": 16000, "batch_size": args.batch,
        "shuffle": True, "num_workers": 4, "pin_memory": True, "max_duration": 30.0, "min_duration": 0.1,
        "use_lhotse": False, "is_tarred": False}))
    trainer = pl.Trainer(accelerator="gpu", devices=1, max_epochs=args.epochs, max_steps=args.max_steps,
                         accumulate_grad_batches=args.accum,
                         precision="bf16-mixed", gradient_clip_val=1.0, logger=False, enable_checkpointing=False,
                         limit_val_batches=0, log_every_n_steps=20, enable_progress_bar=True)
    model.set_trainer(trainer)
    model.setup_optimization(optim_config=OmegaConf.create({
        "name": "adamw", "lr": args.lr, "betas": [0.9, 0.98], "weight_decay": 1e-3,
        "sched": {"name": "CosineAnnealing", "warmup_ratio": 0.05, "min_lr": 1e-6}}))
    trainer.fit(model)
    model.save_to(str(out / "model.nemo"))
    print(f"model saved to {out / 'model.nemo'}")


if __name__ == "__main__":
    main()
