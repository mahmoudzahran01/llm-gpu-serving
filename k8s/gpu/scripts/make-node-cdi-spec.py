#!/usr/bin/env python3
"""Rewrite the host's CDI spec so it is valid inside a kind node that holds a COPY of the driver files.

usage: make-node-cdi-spec.py <host-spec.yaml> <node-prefix> [--copied-list FILE] > node-spec.yaml

 - every mount `hostPath: /x` becomes `hostPath: <prefix>/x` (where the copy lives inside the node)
 - mounts whose source is not a regular file on the host (sockets, missing files) are dropped
 - the createContainer hook binary path is rewritten the same way
The containerPath side is left alone, so pods see the normal /usr/lib/... paths.
"""

import os
import sys

import yaml


def main():
    spec_path, prefix = sys.argv[1], sys.argv[2].rstrip("/")
    spec = yaml.safe_load(open(spec_path))

    def fix_edits(edits):
        mounts = []
        for m in edits.get("mounts", []):
            if os.path.isfile(m["hostPath"]):
                mounts.append({**m, "hostPath": prefix + m["hostPath"]})
        if "mounts" in edits:
            edits["mounts"] = mounts
        for h in edits.get("hooks", []):
            if h["path"].startswith("/usr/bin/nvidia-"):
                h["path"] = prefix + h["path"]
        return edits

    fix_edits(spec["containerEdits"])
    for d in spec["devices"]:
        fix_edits(d["containerEdits"])
    yaml.safe_dump(spec, sys.stdout, sort_keys=False)


main()
