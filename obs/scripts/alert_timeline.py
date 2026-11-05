#!/usr/bin/env python3
"""Alert episodes from Prometheus' own ALERTS series.   alert_timeline.py <start_epoch> [end_epoch]
An episode = continuous stretch where the alert was FIRING (i.e. after its `for:` hold time)."""

import json
import sys
import time
import urllib.parse
import urllib.request

t0 = float(sys.argv[1])
t1 = float(sys.argv[2]) if len(sys.argv) > 2 else time.time()
qs = urllib.parse.urlencode({"query": 'ALERTS{alertstate="firing"}', "start": t0, "end": t1, "step": 10})
res = json.load(urllib.request.urlopen("http://127.0.0.1:9090/api/v1/query_range?" + qs))["data"]["result"]
rows = []
for s in res:
    ts = [float(t) for t, _ in s["values"]]
    ep = [[ts[0], ts[0]]]
    for t in ts[1:]:
        if t - ep[-1][1] > 30:
            ep.append([t, t])
        else:
            ep[-1][1] = t
    rows += [
        (a, b, s["metric"]["alertname"], s["metric"].get("severity", ""), s["metric"].get("instance", "-"))
        for a, b in ep
    ]


def fmt(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


print("Alert episodes while FIRING (after the `for:` hold); local time. Source: Prometheus ALERTS series.")
print(f"{'alertname':<32}{'severity':<10}{'instance':<18}{'from':<10}{'until':<10}duration")
for a, b, n, sev, inst in sorted(rows):
    print(f"{n:<32}{sev:<10}{inst:<18}{fmt(a):<10}{fmt(b):<10}{int(b - a)}s")
