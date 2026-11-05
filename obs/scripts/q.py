#!/usr/bin/env python3
"""Tiny query helper (no jq/shell-quoting pain).
q.py prom 'rate(llmapp_requests_total[1m])'            # instant query
q.py range 'expr' [minutes=10] [step=15]               # range query, prints a compact table
q.py loki '{service_name="llm-gateway-demo"} |= "ERROR"' [minutes=15] [limit=5]
q.py alerts                                           # firing / pending alerts
"""

import json
import sys
import time
import urllib.parse
import urllib.request


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def main():
    cmd = sys.argv[1]
    if cmd == "prom":
        d = get("http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": sys.argv[2]}))
        for r in d["data"]["result"]:
            print(json.dumps(r["metric"], sort_keys=True), r["value"][1])
    elif cmd == "range":
        mins = float(sys.argv[3]) if len(sys.argv) > 3 else 10
        step = sys.argv[4] if len(sys.argv) > 4 else "15"
        now = time.time()
        d = get(
            "http://127.0.0.1:9090/api/v1/query_range?"
            + urllib.parse.urlencode({"query": sys.argv[2], "start": now - mins * 60, "end": now, "step": step})
        )
        for r in d["data"]["result"]:
            vals = [(time.strftime("%H:%M:%S", time.localtime(t)), v) for t, v in r["values"]]
            print(json.dumps(r["metric"], sort_keys=True))
            print("  " + "  ".join(f"{t}={float(v):.3g}" for t, v in vals))
    elif cmd == "loki":
        mins = float(sys.argv[3]) if len(sys.argv) > 3 else 15
        limit = sys.argv[4] if len(sys.argv) > 4 else "5"
        now = time.time()
        d = get(
            "http://127.0.0.1:3100/loki/api/v1/query_range?"
            + urllib.parse.urlencode(
                {"query": sys.argv[2], "limit": limit, "start": int((now - mins * 60) * 1e9), "end": int(now * 1e9)}
            )
        )
        for s in d["data"]["result"]:
            for _, line in s["values"]:
                print(line[:400])
    elif cmd == "alerts":
        d = get("http://127.0.0.1:9090/api/v1/alerts")
        for a in d["data"]["alerts"]:
            print(
                a["state"],
                a["labels"]["alertname"],
                a["labels"].get("instance", ""),
                a.get("activeAt", ""),
                a["annotations"].get("summary", ""),
            )


main()
