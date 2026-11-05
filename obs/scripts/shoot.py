#!/usr/bin/env python3
"""Headless-Chromium screenshots of Grafana (dashboards, a trace in Explore, an alert list).

  shoot.py dash app out.png [--from 1730000000] [--to 1730003600] [--var instance=obs-vllm] [--height 3200]
  shoot.py trace <trace_id> out.png
  shoot.py url <relative-url> out.png [--height 1000]
Needs: playwright + `python -m playwright install chromium`.
"""

import argparse
import base64
import json
import os
import time
import urllib.parse

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3030"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["dash", "trace", "url"])
    ap.add_argument("target")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--from", dest="t0", type=float)
    ap.add_argument("--to", dest="t1", type=float)
    ap.add_argument("--var", action="append", default=[])
    ap.add_argument("--height", type=int, default=3300)
    ap.add_argument("--width", type=int, default=1700)
    ap.add_argument("--wait", type=float, default=9)
    a = ap.parse_args()
    now = time.time()
    t0, t1 = a.t0 or now - 1800, a.t1 or now
    if a.kind == "dash":
        qs = {"orgId": 1, "from": int(t0 * 1000), "to": int(t1 * 1000), "kiosk": "", "refresh": ""}
        qs = urllib.parse.urlencode(qs).replace("kiosk=", "kiosk")
        extra = "".join(f"&var-{v}" for v in a.var)
        url = f"{BASE}/d/{a.target}?{qs}{extra}"
        out = a.out
    elif a.kind == "trace":
        pane = {
            "a": {
                "datasource": "tempo",
                "queries": [
                    {
                        "refId": "A",
                        "datasource": {"type": "tempo", "uid": "tempo"},
                        "queryType": "traceql",
                        "query": a.target,
                    }
                ],
                "range": {"from": "now-6h", "to": "now"},
            }
        }
        url = f"{BASE}/explore?schemaVersion=1&orgId=1&panes=" + urllib.parse.quote(json.dumps(pane))
        out = a.out
    else:
        url, out = (a.target if a.target.startswith("http") else BASE + a.target), a.out
    with sync_playwright() as p:
        b = p.chromium.launch()
        password = os.environ["GRAFANA_ADMIN_PASSWORD"]  # Explore needs a non-viewer role
        token = base64.b64encode(f"admin:{password}".encode()).decode()
        ctx = b.new_context(
            viewport={"width": a.width, "height": a.height},
            device_scale_factor=1,
            extra_http_headers={"Authorization": f"Basic {token}"},
        )
        pg = ctx.new_page()
        pg.goto(url, wait_until="load" if a.target.startswith("http") else "networkidle", timeout=60000)
        pg.wait_for_timeout(int(a.wait * 1000))
        pg.screenshot(path=out, full_page=False)
        b.close()
    print("saved", out, url[:140])


main()
