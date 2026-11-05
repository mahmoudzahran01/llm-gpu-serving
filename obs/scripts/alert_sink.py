#!/usr/bin/env python3
"""Alertmanager webhook receiver for the lab: appends every notification to a JSONL file.
Stands in for PagerDuty/Slack so we can PROVE an alert was routed and delivered."""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = sys.argv[1] if len(sys.argv) > 1 else "alerts.jsonl"


class H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        with open(OUT, "a") as f:
            f.write(
                json.dumps(
                    {
                        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "status": body.get("status"),
                        "alerts": [
                            {
                                "status": a["status"],
                                "name": a["labels"].get("alertname"),
                                "severity": a["labels"].get("severity"),
                                "instance": a["labels"].get("instance"),
                                "summary": a["annotations"].get("summary"),
                                "startsAt": a["startsAt"],
                                "endsAt": a["endsAt"],
                            }
                            for a in body.get("alerts", [])
                        ],
                    }
                )
                + "\n"
            )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):  # quiet
        pass


HTTPServer(("127.0.0.1", 8035), H).serve_forever()
