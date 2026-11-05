from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import httpx
import pytest

from llmserve.obs import loadgen
from llmserve.obs.telemetry import setup_logging


def test_phase_spec_parsing():
    assert loadgen.parse_phases("1:45,4:60.5") == [(1, 45.0), (4, 60.5)]
    with pytest.raises(ValueError):
        loadgen.parse_phases("nonsense")


def test_percentile_and_summary_row(capsys):
    assert loadgen.pct([], 50) != loadgen.pct([], 50)  # NaN
    assert loadgen.pct([1, 2, 3, 4, 5], 50) == 3
    recs = [{"status": 200, "latency_s": 1.0}, {"status": 200, "latency_s": 3.0}, {"status": 504, "latency_s": 0.1}]
    row = loadgen.summarize("t", recs, 2.0)
    assert row["n"] == 3 and row["ok"] == 2 and row["codes"] == {200: 2, 504: 1} and row["rps"] == 1.5
    assert "[t] n=3" in capsys.readouterr().out


def test_raw_prompts_are_unique_per_call_so_they_defeat_the_prefix_cache():
    rng = random.Random(1)
    a, b = loadgen.make_prompt(rng, 50), loadgen.make_prompt(rng, 50)
    assert a != b and a.count(" ") > 50


def test_app_worker_sends_fault_header_and_records_the_trace_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(504, json={"error": "llm_timeout", "trace_id": "abc"})

    out: list[dict] = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await loadgen.app_worker(client, ["q one"], time.time() + 0.05, "llm_timeout", 1.0, out, random.Random(0))

    asyncio.run(go())
    assert out and all(r["status"] == 504 and r["trace_id"] == "abc" and r["fault"] == "llm_timeout" for r in out)
    assert seen[0].headers["x-fault"] == "llm_timeout" and json.loads(seen[0].content) == {"question": "q one"}


def test_app_worker_records_connection_failures_instead_of_crashing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    out: list[dict] = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await loadgen.app_worker(client, ["q"], time.time() + 0.02, "", 0.0, out, random.Random(0))

    asyncio.run(go())
    assert out and out[0]["status"] == 0 and out[0]["error"] == "ConnectError"


def test_raw_worker_records_status_codes():
    out: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["max_tokens"] == 7
        return httpx.Response(200, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await loadgen.raw_worker(client, time.time() + 0.02, 10, 7, out, random.Random(0))

    asyncio.run(go())
    assert out and out[0]["status"] == 200


def test_annotation_is_skipped_without_a_password(monkeypatch, capsys):
    monkeypatch.delenv("GRAFANA_ADMIN_PASSWORD", raising=False)
    loadgen.annotate("x", 0, 1, ["t"])
    assert "annotation skipped" in capsys.readouterr().out


def test_parser_defaults_and_raw_subcommand():
    args = loadgen.build_parser().parse_args([])
    assert args.phases == "1:30" and args.fault == "" and args.mode is None
    raw = loadgen.build_parser().parse_args(["raw", "--concurrency", "24"])
    assert raw.mode == "raw" and raw.concurrency == 24 and raw.prompt_words == 550


def test_setup_logging_writes_json_lines_to_the_file(tmp_path):
    log_file = tmp_path / "logs" / "app.jsonl"
    setup_logging(str(log_file))
    logging.getLogger("t").warning("hello", extra={"stage": "answer"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    doc = json.loads(log_file.read_text().splitlines()[-1])
    assert doc["msg"] == "hello" and doc["stage"] == "answer" and doc["level"] == "WARNING"
