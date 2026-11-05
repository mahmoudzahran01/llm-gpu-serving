"""Load-test an OpenAI-compatible server with streaming requests and report what users feel.

Per concurrency level:

* **TTFT** (time to first token): queueing + prefill. A long *shared* system prompt makes this the metric that
  prefix caching improves.
* **TPOT** (time per output token after the first): per-request decode speed.
* **Output tokens/s** across all requests: the throughput continuous batching buys.

Requests stream (``stream=True``) so TTFT is measured where the user feels it.

    python -m llmserve.bench --base-url http://localhost:8010/v1 --model m --levels 1,8,16 --requests 32 --tag bf16
    python -m llmserve.bench ... --system-prompt-file prompt.txt --questions-file questions.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from llmserve.workload import Workload, load_workload


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank-below percentile (``pct`` in 0..1) of a non-empty list, rounded to milliseconds."""
    ordered = sorted(values)
    return round(ordered[min(int(pct * (len(ordered) - 1)), len(ordered) - 1)], 3)


async def one_request(
    client: httpx.AsyncClient, base_url: str, model: str, system: str, question: str, max_tokens: int
) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}],
    }
    start = time.perf_counter()
    first = None
    tokens = 0
    async with client.stream("POST", f"{base_url}/chat/completions", json=body) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if chunk.get("usage"):
                tokens = chunk["usage"].get("completion_tokens", tokens)
            choices = chunk.get("choices") or []
            if choices and (choices[0].get("delta") or {}).get("content") and first is None:
                first = time.perf_counter()
    end = time.perf_counter()
    first = first or end
    return {"ttft": first - start, "total": end - start, "tokens": tokens, "tpot": (end - first) / max(tokens - 1, 1)}


def summarize_level(results: list[dict[str, Any]], concurrency: int, wall: float) -> dict[str, Any]:
    ttft = [r["ttft"] for r in results]
    tpot = [r["tpot"] for r in results]
    total_tokens = sum(r["tokens"] for r in results)
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "ttft_p50_s": percentile(ttft, 0.5),
        "ttft_p95_s": percentile(ttft, 0.95),
        "tpot_p50_ms": round(1000 * statistics.median(tpot), 1),
        "output_tok_per_s": round(total_tokens / wall, 1),
        "requests_per_s": round(len(results) / wall, 2),
        "wall_s": round(wall, 1),
    }


async def run_level(
    base_url: str,
    model: str,
    workload: Workload,
    concurrency: int,
    n_requests: int,
    max_tokens: int,
    headers: dict[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    sem = asyncio.Semaphore(concurrency)
    questions = workload.questions
    async with httpx.AsyncClient(headers=headers, timeout=600, transport=transport) as client:

        async def guarded(i: int) -> dict[str, Any]:
            async with sem:
                return await one_request(
                    client, base_url, model, workload.system, questions[i % len(questions)], max_tokens
                )

        wall_start = time.perf_counter()
        results = await asyncio.gather(*(guarded(i) for i in range(n_requests)))
        wall = time.perf_counter() - wall_start
    return summarize_level(list(results), concurrency, wall)


async def main_async(
    args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, Any]]:
    workload = load_workload(
        Path(args.system_prompt_file) if args.system_prompt_file else None,
        Path(args.questions_file) if args.questions_file else None,
        args.prompt_tokens,
    )
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    rows = []
    for level in (int(x) for x in args.levels.split(",")):
        row = await run_level(
            args.base_url, args.model, workload, level, max(args.requests, level), args.max_tokens, headers, transport
        )
        print(json.dumps(row), flush=True)
        rows.append(row)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.tag}.json").write_text(json.dumps({"tag": args.tag, "model": args.model, "rows": rows}, indent=1))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="e.g. http://localhost:8010/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--levels", default="1,4,16,32", help="comma-separated concurrency levels")
    parser.add_argument("--requests", type=int, default=64, help="requests per level (at least the level itself)")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--system-prompt-file", default="", help="shared system prompt (default: synthetic schema)")
    parser.add_argument("--questions-file", default="", help="one question per line, or jsonl with a 'question' field")
    parser.add_argument("--prompt-tokens", type=int, default=1600, help="size of the synthetic system prompt")
    parser.add_argument("--out-dir", default="runs/bench")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
