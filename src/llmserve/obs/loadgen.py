"""Load generator for the observability stack.

``app`` mode (default): closed-loop clients hitting the demo service's ``POST /ask``, concurrency RAMPED in phases::

    python -m llmserve.obs.loadgen --phases "1:45,2:45,4:60,8:60" --out runs/obs/load-ramp.jsonl
    python -m llmserve.obs.loadgen --phases "8:180" --fault llm_timeout --fault-rate 1.0 --annotate "stalled LLM"

``raw`` mode: hammer the vLLM server directly with long, UNIQUE prompts (which defeat the prefix cache) to drive
KV-cache and queue saturation for the alert drills. Only ever point it at a server you own::

    python -m llmserve.obs.loadgen raw --concurrency 16 --duration 90 --prompt-words 550

Closed loop = each virtual user sends the next request when the previous answer arrives, so offered load
self-limits (like real interactive users). Every request is written to JSONL with its trace_id, so a slow or
failed request can be opened in Tempo afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import time
import urllib.request
from pathlib import Path

import httpx

from llmserve.workload import load_questions, synthetic_workload

APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8030")
VLLM_URL = os.getenv("VLLM_URL", "http://127.0.0.1:8020")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://127.0.0.1:3030")
VLLM_MODEL = os.getenv("LLM_MODEL", "Qwen/Qwen3-4B-Instruct-2507")
VOCAB = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo sierra tango"
).split()


def annotate(text: str, start_s: float, end_s: float, tags: list[str]) -> None:
    """Mark the run on every Grafana dashboard (orange region, tag 'fault'). Best effort."""
    password = os.getenv("GRAFANA_ADMIN_PASSWORD", "")
    if not password:
        print("(annotation skipped: set GRAFANA_ADMIN_PASSWORD)")
        return
    body = json.dumps({"text": text, "tags": tags, "time": int(start_s * 1000), "timeEnd": int(end_s * 1000)}).encode()
    req = urllib.request.Request(f"{GRAFANA_URL}/api/annotations", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"admin:{password}".encode()).decode())
    try:
        urllib.request.urlopen(req, timeout=5).read()  # noqa: S310 - fixed http(s) URL from our own config
    except Exception as exc:  # noqa: BLE001 - annotation is best-effort
        print(f"(annotation failed: {exc})")


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def summarize(label: str, recs: list[dict], seconds: float) -> dict:
    ok = [r for r in recs if r["status"] == 200]
    lat = [r["latency_s"] for r in recs]
    codes: dict[int, int] = {}
    for r in recs:
        codes[r["status"]] = codes.get(r["status"], 0) + 1
    row = {
        "label": label,
        "n": len(recs),
        "rps": round(len(recs) / max(seconds, 1e-9), 2),
        "ok": len(ok),
        "codes": codes,
        "p50_s": round(pct(lat, 50), 2),
        "p95_s": round(pct(lat, 95), 2),
        "max_s": round(max(lat, default=0), 2),
    }
    print(
        f"[{label}] n={row['n']} rps={row['rps']} ok={row['ok']} codes={codes} "
        f"lat p50={row['p50_s']}s p95={row['p95_s']}s max={row['max_s']}s",
        flush=True,
    )
    return row


async def app_worker(
    client: httpx.AsyncClient,
    qs: list[str],
    deadline: float,
    fault: str,
    fault_rate: float,
    out: list[dict],
    rng: random.Random,
) -> None:
    while time.time() < deadline:
        question = rng.choice(qs)
        headers = {"X-Fault": fault} if fault and rng.random() < fault_rate else {}
        t0 = time.time()
        try:
            r = await client.post(f"{APP_URL}/ask", json={"question": question}, headers=headers)
            body = r.json()
            rec = {
                "t": t0,
                "latency_s": time.time() - t0,
                "status": r.status_code,
                "trace_id": body.get("trace_id"),
                "fault": headers.get("X-Fault", ""),
                "error": body.get("error"),
                "tokens": body.get("tokens"),
                "stage_ms": body.get("stage_ms"),
            }
        except (httpx.HTTPError, ValueError) as exc:
            rec = {
                "t": t0,
                "latency_s": time.time() - t0,
                "status": 0,
                "error": type(exc).__name__,
                "fault": headers.get("X-Fault", ""),
            }
        out.append(rec)


def parse_phases(spec: str) -> list[tuple[int, float]]:
    return [(int(c), float(s)) for c, s in (p.split(":") for p in spec.split(","))]


async def run_app(args: argparse.Namespace) -> None:
    qs = (
        list(load_questions(Path(args.questions_file))) if args.questions_file else list(synthetic_workload().questions)
    )
    rng = random.Random(args.seed)
    allrecs: list[dict] = []
    started = time.time()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(args.timeout, connect=5), limits=httpx.Limits(max_connections=512)
    ) as client:
        for conc, secs in parse_phases(args.phases):
            recs: list[dict] = []
            t0 = time.time()
            await asyncio.gather(
                *[app_worker(client, qs, t0 + secs, args.fault, args.fault_rate, recs, rng) for _ in range(conc)]
            )
            summarize(f"concurrency={conc} {secs:.0f}s", recs, time.time() - t0)
            allrecs += [{**r, "phase_concurrency": conc} for r in recs]
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(json.dumps(r) for r in allrecs) + "\n")
        print("wrote", args.out)
    summarize("TOTAL", allrecs, time.time() - started)
    if args.annotate:
        annotate(args.annotate, started, time.time(), ["fault" if args.fault else "load", "loadgen"])


def make_prompt(rng: random.Random, words: int) -> str:
    body = " ".join(
        rng.choice(VOCAB) + str(rng.randint(0, 999)) for _ in range(words)
    )  # unique -> no prefix-cache hits
    return f"Summarise the following noisy log in detail, at length:\n{body}"


async def raw_worker(
    client: httpx.AsyncClient, deadline: float, words: int, max_tokens: int, out: list[dict], rng: random.Random
) -> None:
    while time.time() < deadline:
        t0 = time.time()
        try:
            r = await client.post(
                f"{VLLM_URL}/v1/chat/completions",
                json={
                    "model": VLLM_MODEL,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                    "messages": [{"role": "user", "content": make_prompt(rng, words)}],
                },
            )
            out.append({"t": t0, "latency_s": time.time() - t0, "status": r.status_code})
        except httpx.HTTPError as exc:
            out.append({"t": t0, "latency_s": time.time() - t0, "status": 0, "error": type(exc).__name__})


async def run_raw(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    recs: list[dict] = []
    t0 = time.time()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(args.timeout, connect=5), limits=httpx.Limits(max_connections=256)
    ) as client:
        await asyncio.gather(
            *[
                raw_worker(client, t0 + args.duration, args.prompt_words, args.max_tokens, recs, rng)
                for _ in range(args.concurrency)
            ]
        )
    summarize(f"raw concurrency={args.concurrency}", recs, time.time() - t0)
    if args.annotate:
        annotate(args.annotate, t0, time.time(), ["load", "loadgen"])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode")
    ap.add_argument("--phases", default="1:30", help='"concurrency:seconds,..." e.g. "1:45,2:45,4:60"')
    ap.add_argument("--questions-file", default="", help="one question per line, or jsonl with a 'question' field")
    ap.add_argument("--fault", default="", choices=["", "llm_timeout"])
    ap.add_argument("--fault-rate", type=float, default=1.0, help="fraction of requests that carry the fault header")
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--out", default="")
    ap.add_argument("--annotate", default="")
    ap.add_argument("--seed", type=int, default=7)
    raw = sub.add_parser("raw")
    raw.add_argument("--concurrency", type=int, default=16)
    raw.add_argument("--duration", type=float, default=90)
    raw.add_argument("--prompt-words", type=int, default=550)
    raw.add_argument("--max-tokens", type=int, default=300)
    raw.add_argument("--timeout", type=float, default=240)
    raw.add_argument("--annotate", default="")
    raw.add_argument("--seed", type=int, default=7)
    return ap


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_raw(args) if args.mode == "raw" else run_app(args))


if __name__ == "__main__":
    main()
