from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from llmserve.bench import main_async, one_request, percentile, run_level, summarize_level
from llmserve.workload import Workload, approx_tokens, load_questions, load_workload, synthetic_workload


def test_synthetic_workload_is_deterministic_and_close_to_the_target_size():
    a, b = synthetic_workload(1600), synthetic_workload(1600)
    assert a == b
    assert 1600 <= approx_tokens(a.system) < 2200
    assert synthetic_workload(600).system != a.system and len(synthetic_workload(600).system) < len(a.system)
    assert a.system.startswith("You are a text-to-SQL engine") and "TABLE stations" in a.system


def test_synthetic_questions_cover_english_and_arabic_and_reference_existing_tables():
    w = synthetic_workload(800)
    assert any("ما عدد" in q for q in w.questions) and any(q.startswith("What") for q in w.questions)
    tables = {line.split()[1] for line in w.system.splitlines() if line.startswith("TABLE ")}
    assert all(any(t in q for t in tables) for q in w.questions)


def test_questions_load_from_plain_text_and_jsonl(tmp_path: Path):
    (tmp_path / "q.txt").write_text("first\n\nsecond\n", encoding="utf-8")
    (tmp_path / "q.jsonl").write_text('{"question": "one"}\n{"question": "two", "sql": "x"}\n', encoding="utf-8")
    assert load_questions(tmp_path / "q.txt") == ("first", "second")
    assert load_questions(tmp_path / "q.jsonl") == ("one", "two")


def test_load_workload_prefers_user_files_and_rejects_empty_questions(tmp_path: Path):
    (tmp_path / "sys.txt").write_text("MY PROMPT\n", encoding="utf-8")
    (tmp_path / "q.txt").write_text("hello\n", encoding="utf-8")
    assert load_workload(tmp_path / "sys.txt", tmp_path / "q.txt") == Workload("MY PROMPT", ("hello",))
    assert load_workload(None, tmp_path / "q.txt").system == synthetic_workload().system
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_workload(None, tmp_path / "empty.txt")


def test_percentile_is_order_independent_and_bounded():
    assert percentile([3, 1, 2], 0.5) == 2 and percentile([5], 0.95) == 5
    assert percentile(list(range(101)), 0.95) == 95


def _sse(texts: list[str], tokens: int) -> bytes:
    chunks = [{"choices": [{"delta": {"content": t}}]} for t in texts]
    chunks.append({"choices": [], "usage": {"completion_tokens": tokens}})
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


def _transport(seen: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=_sse(["SEL", "ECT 1"], 12), headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def test_one_request_measures_ttft_tpot_and_counts_usage_tokens():
    seen: list[dict] = []

    async def go():
        async with httpx.AsyncClient(transport=_transport(seen)) as client:
            return await one_request(client, "http://stub/v1", "m", "SYSTEM", "Q?", 64)

    r = asyncio.run(go())
    assert r["tokens"] == 12 and 0 <= r["ttft"] <= r["total"] and r["tpot"] >= 0
    assert seen[0]["stream"] is True and seen[0]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert seen[0]["stream_options"] == {"include_usage": True} and seen[0]["max_tokens"] == 64


def test_run_level_cycles_through_questions_and_summarises():
    seen: list[dict] = []
    w = Workload("S", ("a", "b", "c"))
    row = asyncio.run(run_level("http://stub/v1", "m", w, 2, 5, 16, {}, _transport(seen)))
    assert row["concurrency"] == 2 and row["requests"] == 5
    assert sorted(s["messages"][1]["content"] for s in seen) == ["a", "a", "b", "b", "c"]
    assert {"ttft_p50_s", "ttft_p95_s", "tpot_p50_ms", "output_tok_per_s", "requests_per_s", "wall_s"} <= set(row)
    assert row["output_tok_per_s"] > 0


def test_summarize_level_arithmetic():
    results = [{"ttft": 0.1, "tpot": 0.01, "tokens": 100}, {"ttft": 0.3, "tpot": 0.03, "tokens": 100}]
    row = summarize_level(results, concurrency=2, wall=2.0)
    assert row["output_tok_per_s"] == 100.0 and row["requests_per_s"] == 1.0
    assert row["tpot_p50_ms"] == 20.0 and row["ttft_p50_s"] == 0.1 and row["ttft_p95_s"] == 0.1


def test_cli_defaults(monkeypatch):
    from llmserve import bench

    captured = {}

    def fake_run(coro):
        captured["args"] = coro.cr_frame.f_locals["args"]
        coro.close()

    monkeypatch.setattr("sys.argv", ["bench", "--base-url", "http://x/v1", "--model", "m", "--tag", "t"])
    monkeypatch.setattr(bench.asyncio, "run", fake_run)
    bench.main()
    assert captured["args"].levels == "1,4,16,32" and captured["args"].prompt_tokens == 1600


def test_main_writes_one_result_file_per_tag(tmp_path: Path):
    import argparse

    args = argparse.Namespace(
        base_url="http://stub/v1",
        model="m",
        tag="t1",
        levels="1,2",
        requests=2,
        max_tokens=8,
        api_key="k",
        system_prompt_file="",
        questions_file="",
        prompt_tokens=400,
        out_dir=str(tmp_path),
    )
    rows = asyncio.run(main_async(args, _transport([])))
    saved = json.loads((tmp_path / "t1.json").read_text())
    assert [r["concurrency"] for r in rows] == [1, 2] and saved["tag"] == "t1" and len(saved["rows"]) == 2
