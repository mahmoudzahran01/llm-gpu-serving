"""Merge a LoRA adapter into its base model, then quantize the merged model.

Schemes (all produce checkpoints vLLM loads directly):

* ``fp8``    FP8_DYNAMIC: weights FP8 per-channel, activations FP8 per-token at runtime. No calibration data
             needed. About 2x smaller than BF16.
* ``w4a16``  GPTQ INT4 weights, BF16 activations. Calibrated on prompts+answers from your own workload so the
             quantization error is minimised where the model actually operates. 3.0x smaller as measured
             (7.64 to 2.55 GiB: embeddings and the output head are not quantized).
* ``nvfp4``  NVFP4 (4-bit float with FP8 block scales), Blackwell's native format. Calibrated like w4a16.
             On the model measured here it lost accuracy; see results/RESULTS.md.

    python -m llmserve.quantize merge --base Qwen/Qwen3-4B-Instruct-2507 --adapter runs/qlora/adapter --out runs/merged
    python -m llmserve.quantize quant --model runs/merged --scheme w4a16 \\
        --calib-file data/train.jsonl --system-prompt-file prompt.txt
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

CALIBRATION_SAMPLES = 256
MAX_SEQ_LENGTH = 4096


def merge(base: str, adapter: str, out: str) -> None:  # pragma: no cover - needs the GPU stack
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    # Merge into a BF16 base (not the 4-bit one used for QLoRA training): merging into quantized weights
    # would bake NF4 rounding error into the result.
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16)
    model = PeftModel.from_pretrained(model, adapter).merge_and_unload()
    model.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(adapter).save_pretrained(out)
    print("merged ->", out)


def calibration_texts(
    apply_chat_template: Callable[..., str], pairs: list[dict], system_prompt: str, limit: int = CALIBRATION_SAMPLES
) -> list[str]:
    """Chat-formatted prompt+answer texts from ``{"question", "sql"}`` pairs (never your evaluation questions)."""
    texts = []
    for rec in pairs[:limit]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": rec["question"]},
            {"role": "assistant", "content": rec["sql"]},
        ]
        texts.append(apply_chat_template(messages, tokenize=False))
    return texts


def quantize(  # pragma: no cover - needs the GPU stack
    model_dir: str, scheme: str, out: str | None, calib_file: str, system_prompt_file: str
) -> None:
    from datasets import Dataset
    from llmcompressor import oneshot
    from llmcompressor.modifiers.quantization import GPTQModifier, QuantizationModifier
    from transformers import AutoModelForCausalLM, AutoTokenizer

    out = out or f"{model_dir}-{scheme}"
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForCausalLM.from_pretrained(model_dir, dtype="auto")
    ignore = ["lm_head"]  # the output head is small and sensitive; keep it in BF16

    if scheme == "fp8":
        oneshot(model=model, recipe=QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=ignore))
    else:
        if not calib_file or not system_prompt_file:
            raise SystemExit(f"{scheme} needs --calib-file and --system-prompt-file")
        pairs = [json.loads(ln) for ln in Path(calib_file).open(encoding="utf-8") if ln.strip()]
        system = Path(system_prompt_file).read_text(encoding="utf-8").strip()
        texts = calibration_texts(tokenizer.apply_chat_template, pairs, system)
        ds = Dataset.from_dict({"text": texts}).map(
            lambda x: tokenizer(x["text"], truncation=True, max_length=MAX_SEQ_LENGTH, add_special_tokens=False),
            remove_columns=["text"],
        )
        if scheme == "w4a16":
            recipe = GPTQModifier(targets="Linear", scheme="W4A16", ignore=ignore)
        elif scheme == "nvfp4":
            recipe = QuantizationModifier(targets="Linear", scheme="NVFP4", ignore=ignore)
        else:
            raise ValueError(f"unknown scheme {scheme}")
        oneshot(
            model=model, dataset=ds, recipe=recipe, max_seq_length=MAX_SEQ_LENGTH, num_calibration_samples=len(texts)
        )

    model.save_pretrained(out, save_compressed=True)
    tokenizer.save_pretrained(out)
    size = sum(p.stat().st_size for p in Path(out).glob("*.safetensors")) / 1e9
    print(f"{scheme} -> {out} ({size:.2f} GB)")


def main() -> None:  # pragma: no cover - thin CLI over the GPU functions
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--base", required=True)
    m.add_argument("--adapter", required=True)
    m.add_argument("--out", required=True)
    q = sub.add_parser("quant")
    q.add_argument("--model", required=True)
    q.add_argument("--scheme", choices=["fp8", "w4a16", "nvfp4"], required=True)
    q.add_argument("--out", default=None)
    q.add_argument("--calib-file", default="", help="jsonl of {question, sql} pairs for GPTQ/NVFP4 calibration")
    q.add_argument("--system-prompt-file", default="", help="the system prompt used in serving")
    args = parser.parse_args()
    if args.cmd == "merge":
        merge(args.base, args.adapter, args.out)
    else:
        quantize(args.model, args.scheme, args.out, args.calib_file, args.system_prompt_file)


if __name__ == "__main__":
    main()
