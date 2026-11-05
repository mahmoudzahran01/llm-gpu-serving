# Results: LLM serving on one GPU

Read this first.

* **Hardware and sharing.** One NVIDIA DGX Spark (GB10, 121 GB unified memory) running vLLM 0.31 from the NGC 26.10 image. The machine was
  **shared** with other model servers and a data-generation job while these ran, so absolute speeds are pessimistic and a repeat will
  differ. Comparisons inside one table are like-for-like (same server flags, same prompts); treat speed differences under about 15% as
  noise. Single runs, no repeats, no confidence intervals on the speed numbers.
* **Model.** Qwen3-4B-Instruct-2507, either as published (sections 1 and 2) or after QLoRA fine-tuning for text-to-SQL (sections 3 and 4;
  see the companion repository `bilingual-text-to-sql`).
* **Workload.** Every request carried the same ~1.6k-token system prompt (a database schema, 1,621 tokens) followed by a short question,
  with up to 256 output tokens, streamed. The prompt and the questions come from a **private** schema and cannot be published. This
  repository's `llmserve.bench` builds a synthetic stand-in of any size (`--prompt-tokens`), which will not reproduce these absolute
  numbers but exercises the same effect: a long shared prefix makes prefix caching decisive.
* **Accuracy columns** (`exec acc`, `held-out acc`) are execution accuracy of text-to-SQL answers on **private** evaluation sets: the
  49-question hand-written set (one question = 2 points) in sections 1 and 2, and the 297-question frozen held-out set in section 4. The
  held-out references are teacher-agreed results, not human labels. "Within noise" means exactly that: the sets are small. Per-run
  summaries are in `accuracy_summaries.jsonl`.
* **Provenance.** The benchmark JSON behind each table is in `bench/` (section 1: `bf16-prefix-{on,off}`; section 2: `kv5-*`;
  section 3: `spec-{none,ngram,draft06b}` plus `.metrics.txt`; section 4: `final-*`). `*.serve.txt` holds the weight memory and KV-cache
  size reported by the server. The sweep scripts in `../scripts/` reproduce sections 2 to 4; no script is included for section 1.
  The scripts were generalised after these runs and have not been re-run in their published form.

## 1. Continuous batching and prefix caching (Qwen3-4B BF16, KV cache 2.75 GiB = 20k tokens, 32 requests per level)

Prefix caching ON (hit rate 96.8% of prompt tokens):

| Concurrency | TTFT p50 | TTFT p95 | TPOT p50 | Output tok/s |
|---|---|---|---|---|
| 1 | 0.24 s | 0.29 s | 106 ms | 9.6 |
| 4 | 0.26 s | 0.32 s | 86 ms | 46.5 |
| 8 | 0.26 s | 0.33 s | 84 ms | 89.0 |
| 16 | 0.26 s | 0.39 s | 99 ms | 133.6 |

Prefix caching OFF (1 preemption, 100 s total queueing):

| Concurrency | TTFT p50 | TTFT p95 | TPOT p50 | Output tok/s |
|---|---|---|---|---|
| 1 | 0.48 s | 0.60 s | 96 ms | 9.9 |
| 4 | 0.65 s | 1.20 s | 102 ms | 36.5 |
| 8 | 0.70 s | 2.29 s | 126 ms | 57.5 |
| 16 | 3.43 s | 7.08 s | 157 ms | 72.0 |

Takeaways: continuous batching gives ~14x throughput from c=1 to c=16 with flat TPOT; the shared ~1.6k-token schema prompt (1,621 tokens) makes prefix caching worth 13x on TTFT and 1.9x on throughput at c=16, because without it each request re-prefills the schema and the small KV cache forces queueing/preemption.

## 2. Quantization sweep on the base model (fixed 5 GiB KV cache)

Same server flags for every variant: fixed 5 GiB KV cache (`--kv-cache-memory`, 36,400 tokens),
max-model-len 6144, prefix caching on. Bench: 32 streaming requests per concurrency level.
Shared GB10 (other model servers were running on it), so treat speed differences under ~15% as noise.

| scheme | weights GiB | exec acc | EN | AR | TPOT p50 c=1 | tok/s c=1 | tok/s c=8 | tok/s c=16 | TTFT p50 c=16 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BF16 | 7.64 | 49.0 | 56.7 | 36.8 | 101 ms | 10 | 84 | 131 | 0.32 s |
| FP8 dynamic (W8A8) | 4.27 | 51.0 | 63.3 | 31.6 | 53 ms | 20 | 181 | 260 | 0.15 s |
| GPTQ W4A16 (Marlin) | 2.55 | 55.1 | 60.0 | 47.4 | **28 ms** | **33** | 183 | 261 | 0.14 s |
| NVFP4 (W4A4) | 2.71 | 40.8 | 50.0 | 26.3 | 41 ms | 25 | **193** | **295** | 0.17 s |

Takeaways:
- **Single stream is memory-bandwidth bound**: decode reads every weight per token, so time per token
  tracks weight bytes. 4-bit weights give 3.6x faster TPOT than BF16 at c=1.
- **Under batching, compute matters**: W4A16 dequantizes to BF16 before the matmul, so its edge disappears at
  c=16 (same as FP8). NVFP4 runs the matmul on FP4 tensor cores and is the fastest at c=16 (+13% vs W4A16).
- **Accuracy**: FP8 and W4A16 are within noise of BF16 (n=49, 1 question = 2 pts). NVFP4 lost 8 pts,
  mostly Arabic (36.8 -> 26.3). It quantizes activations to 4 bits too, which hurts more than weight-only.
  It would need more calibration or QAT (quantization-aware training) to be safe.
- **Pick for this workload**: FP8 if you want zero-risk 2x; W4A16 for latency-bound single-user serving;
  NVFP4 only with a bigger eval proving no quality loss.
- BF16 scored 51.0 in the original baseline run and 49.0 in both sweeps: same weights, temperature 0, different batching.
  That is the run-to-run noise floor (batch-dependent float reductions).

## 3. Speculative decoding (fine-tuned model, merged, BF16)

| method | acceptance | tok/s c=1 | tok/s c=8 | tok/s c=16 | TPOT p50 c=1 | held-out acc |
|---|---:|---:|---:|---:|---:|---:|
| none | — | 20.7 | 170 | 266 | 47.6 ms | 76.8 |
| n-gram (prompt lookup) | 25% | 24.1 | 187 | **122** | 33.5 ms | 77.1 |
| **Qwen3-0.6B draft model** | **66%** | **45.3** | **280** | **411** | **20.5 ms** | 76.1 |

- The **draft model doubles single-user speed (2.2x)** and still wins at 16 users (1.5x).
- **n-gram lookup hurts under load**: only 25% of its guesses are accepted, and at c=16 the rejected tokens
  waste batch capacity (266 → 122 tok/s). It only pays when outputs copy long spans of the prompt.
- Accuracy is unchanged (within ±0.7): speculative decoding is lossless by construction (the target model verifies every token).

## 4. The final fine-tuned model, quantized

Same server flags as the kv5 sweep (5 GiB KV cache, prefix caching on, 32 requests per level).

| variant | size | held-out acc | EN | AR | tok/s c=1 | tok/s c=8 | tok/s c=16 | TPOT p50 c=1 | TTFT p50 c=16 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BF16 merged (vLLM) | 7.6 GB | 85.9 | 81.6 | 92.9 | 21.2 | 173 | 245 | 46.6 ms | 0.15 s |
| FP8 dynamic (vLLM) | 4.2 GB | 84.5 | 80.5 | 91.1 | 41.2 | 282 | 401 | 24.0 ms | 0.10 s |
| **GPTQ W4A16 (vLLM)** | **2.5 GB** | **85.2** | 83.2 | 88.4 | **64.6** | **428** | **672** | **15.2 ms** | 0.08 s |
| GGUF Q4_K_M (llama.cpp) | 2.5 GB | 85.5 | 84.3 | 87.5 | 60.4 | 191 | 199 | 15.4 ms | 0.30 s |

- **INT4 (W4A16) is 3x smaller and 3x faster** at one user (21 → 65 tok/s) and 2.7x at sixteen, for -0.7 pts (n.s.).
  On the fine-tuned model W4A16 kept its lead under batching too, unlike the base-model sweep above,
  where it tied FP8 at c=16. (Different run, shared GPU: treat the c=16 ordering as indicative.)
- **llama.cpp matches vLLM at one user but not under load**: 199 tok/s at c=16 vs 672, and TTFT p95 1.6 s.
  Its strength is single-user/edge serving and CPU/Mac portability, not batched GPU serving.
- **Arabic drops 2-5 pts under 4-bit** (92.9 → 88.4/87.5) while English holds or rises. Watch per-language
  scores, not just the overall number, when quantizing a bilingual model.
  Not significant on its own: 6 vs 1 discordant Arabic questions (p=0.12) for W4A16, 9 vs 3 (p=0.15) for GGUF. Treat it as a watch item, not a finding.
