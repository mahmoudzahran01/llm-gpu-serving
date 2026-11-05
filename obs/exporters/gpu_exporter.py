#!/usr/bin/env python3
"""Tiny nvidia-smi based exporter: per-WORKLOAD GPU memory, the one thing DCGM cannot give on GB10.

Why this exists: on the DGX Spark's GB10 the GPU shares ONE unified memory pool with the CPU, so
NVML reports memory.used / memory.total as [N/A]. The NVIDIA DCGM exporter runs fine on GB10 and
supplies utilisation / power / temperature / clocks / energy (we run it in docker-compose), but it
exposes NO framebuffer or per-process memory. `nvidia-smi --query-compute-apps` still lists each
process's GPU allocation, so this ~100-line exporter maps pid -> llama-<port> / docker container
name and exports it. (see obs/README.md, "GPU metrics on GB10")

Series
  gpu_process_memory_bytes{workload}   per-process GPU allocation, mapped to a human name
  gpu_exporter_scrape_ok               1 if the last nvidia-smi call worked
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

_cache: dict = {"at": 0.0, "map": {}}


def smi(*args: str) -> list[list[str]]:
    out = subprocess.run(
        ["nvidia-smi", *args, "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True
    ).stdout
    return [[c.strip() for c in line.split(",")] for line in out.strip().splitlines() if line.strip()]


def container_names() -> dict[str, str]:
    """docker short-id -> name, cached 30s (so a container named obs-vllm is labelled as such)."""
    if time.time() - _cache["at"] > 30:
        try:
            out = subprocess.run(
                ["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            _cache["map"] = {ln.split()[0]: ln.split()[1] for ln in out.splitlines() if ln.strip()}
        except Exception:  # noqa: BLE001 - labelling is best-effort
            pass
        _cache["at"] = time.time()
    return _cache["map"]


def workload(pid: str, proc: str) -> str:
    """Bounded, human label for a GPU process: llama-<port>, docker container name, or basename."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
        if "llama-server" in cmd[0] and "--port" in cmd:
            return f"llama-{cmd[cmd.index('--port') + 1]}"
        cg = Path(f"/proc/{pid}/cgroup").read_text()
        m = re.search(r"([0-9a-f]{64})", cg)
        if m and m.group(1) in container_names():
            return container_names()[m.group(1)]
    except OSError:
        pass
    return Path(proc).name or "unknown"


def num(x: str) -> float | None:
    try:
        return float(x)
    except ValueError:  # "[N/A]", "Not Supported"
        return None


class Collector:
    def collect(self):
        ok = GaugeMetricFamily("gpu_exporter_scrape_ok", "1 if nvidia-smi answered")
        try:
            procs = smi("--query-compute-apps=pid,process_name,used_gpu_memory")
        except Exception:  # noqa: BLE001
            ok.add_metric([], 0)
            yield ok
            return
        ok.add_metric([], 1)
        yield ok
        mem = GaugeMetricFamily("gpu_process_memory_bytes", "GPU memory allocated per workload", labels=["workload"])
        totals: dict[str, float] = {}
        for pid, proc, used in procs:
            v = num(used)
            if v is not None:
                w = workload(pid, proc)
                totals[w] = totals.get(w, 0.0) + v * 1024 * 1024
        for w, b in totals.items():
            mem.add_metric([w], b)
        yield mem


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9401)
    ap.add_argument("--addr", default="127.0.0.1")
    a = ap.parse_args()
    REGISTRY.register(Collector())
    start_http_server(a.port, addr=a.addr)
    while True:
        time.sleep(3600)
