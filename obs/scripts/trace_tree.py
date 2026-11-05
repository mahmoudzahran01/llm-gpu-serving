#!/usr/bin/env python3
"""Pretty-print a Tempo trace as an indented span tree.   trace_tree.py <trace_id> [--json out.json]"""

import json
import sys
import urllib.request

TEMPO = "http://localhost:3200"


def fetch(tid: str) -> dict:
    with urllib.request.urlopen(f"{TEMPO}/api/traces/{tid}", timeout=10) as r:
        return json.load(r)


def attr(a):
    v = a["value"]
    return next(iter(v.values())) if v else None


def spans(doc):
    for rs in doc.get("batches", doc.get("resourceSpans", [])):
        svc = next((attr(a) for a in rs["resource"]["attributes"] if a["key"] == "service.name"), "?")
        for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])):
            for s in ss["spans"]:
                s["_svc"] = svc
                yield s


def main():
    tid = sys.argv[1]
    doc = fetch(tid)
    if "--json" in sys.argv:
        json.dump(doc, open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=1)
    all_spans = sorted(spans(doc), key=lambda s: int(s["startTimeUnixNano"]))
    t0 = int(all_spans[0]["startTimeUnixNano"])
    kids = {}
    for s in all_spans:
        kids.setdefault(s.get("parentSpanId", ""), []).append(s)
    keys = {
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.response.finish_reasons",
        "app.llm.ttft_ms",
        "error.type",
        "app.outcome",
        "app.fault",
        "gen_ai.latency.time_in_queue",
        "gen_ai.latency.time_to_first_token",
        "gen_ai.latency.e2e",
    }

    def walk(pid, depth):
        for s in kids.get(pid, []):
            st = int(s["startTimeUnixNano"]) - t0
            du = int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])
            at = {a["key"]: attr(a) for a in s.get("attributes", []) if a["key"] in keys}
            err = " ERROR" if s.get("status", {}).get("code") in ("STATUS_CODE_ERROR", 2) else ""
            evs = [e["name"] for e in s.get("events", []) if e["name"] in ("exception", "llm.retry")]
            print(
                f"{'  ' * depth}{s['name']:<45} [{s['_svc']}] +{st / 1e6:8.0f}ms {du / 1e6:8.0f}ms{err} {at if at else ''} {evs if evs else ''}"
            )
            walk(s["spanId"], depth + 1)

    ids = {s["spanId"] for s in all_spans}
    for root_parent in {s.get("parentSpanId", "") for s in all_spans if s.get("parentSpanId", "") not in ids}:
        walk(root_parent, 0)


main()
