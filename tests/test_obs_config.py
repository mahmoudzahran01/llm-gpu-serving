"""Static checks on the observability configuration: it parses, and the pieces agree with each other."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from prometheus_client import REGISTRY

from llmserve.obs import metrics as _metrics  # noqa: F401  (registers the llmapp_* families)

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "obs"
RULES = [OBS / "prometheus/rules/alerts.yml", OBS / "prometheus/rules/recording.yml"]
CONFIGS = [
    OBS / "docker-compose.yml",
    OBS / "prometheus/prometheus.yml",
    OBS / "alertmanager/alertmanager.yml",
    OBS / "otel/collector.yml",
    OBS / "tempo/tempo.yml",
    OBS / "loki/loki.yml",
    OBS / "grafana/provisioning/datasources/datasources.yml",
    OBS / "grafana/provisioning/dashboards/dashboards.yml",
    *RULES,
]


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rules(path: Path) -> list[dict]:
    return [r for g in _load(path)["groups"] for r in g["rules"]]


def _known_metric_names() -> set[str]:
    names = set()
    for family in REGISTRY.collect():
        if not family.name.startswith("llmapp_"):
            continue
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":
            names.update(f"{family.name}{s}" for s in ("_bucket", "_sum", "_count"))
        else:
            names.add(family.name)
    return names


def _generator():
    spec = importlib.util.spec_from_file_location("gen_dashboards", OBS / "scripts/gen_dashboards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_config_file_is_valid_yaml(path: Path):
    assert _load(path)


def test_alerts_have_severity_summary_and_known_recorded_series():
    recorded = {r["record"] for r in _rules(RULES[1])}
    alerts = _rules(RULES[0])
    assert len(alerts) == len({a["alert"] for a in alerts}) >= 8
    for alert in alerts:
        assert alert["labels"]["severity"] in {"page", "warning", "info"}, alert["alert"]
        assert alert["annotations"]["summary"], alert["alert"]
        used = set(re.findall(r"\b(?:app|llm):[a-z_0-9:]+", alert["expr"]))
        assert used <= recorded, f"{alert['alert']} uses undefined recorded series {used - recorded}"


def test_burn_rate_alerts_follow_the_multiwindow_pattern():
    by_name = {a["alert"]: a["expr"] for a in _rules(RULES[0])}
    fast, slow = by_name["AppSLOBurnRateFast"], by_name["AppSLOBurnRateSlow"]
    assert "rate1h" in fast and "rate5m" in fast and "14.4" in fast
    assert "rate6h" in slow and "rate30m" in slow and re.search(r"\b6\b", slow)


def test_rules_and_dashboards_only_reference_metrics_the_app_exports():
    known = _known_metric_names()
    assert "llmapp_http_requests_total" in known
    texts = [r.get("expr", "") for path in RULES for r in _rules(path)]
    texts += [p.read_text(encoding="utf-8") for p in (OBS / "grafana/dashboards").glob("*.json")]
    referenced = set(re.findall(r"\bllmapp_[a-z_]+", "\n".join(texts)))
    assert referenced and referenced <= known, f"unknown metrics: {referenced - known}"


def test_recorded_series_used_by_dashboards_are_defined():
    recorded = {r["record"] for r in _rules(RULES[1])}
    used = set()
    for path in (OBS / "grafana/dashboards").glob("*.json"):
        used |= set(re.findall(r"\b(?:app|llm):[a-z_0-9]+:[a-z_0-9]+", path.read_text(encoding="utf-8")))
    assert used and used <= recorded


def test_committed_dashboards_match_the_generator():
    gen = _generator()
    for board in (gen.llm_serving(), gen.app_board(), gen.infra()):
        committed = json.loads((OBS / "grafana/dashboards" / f"{board.uid}.json").read_text(encoding="utf-8"))
        assert committed == board.json(), f"{board.uid}.json is stale: run `make dashboards`"


def test_dashboard_panels_have_unique_ids_and_titles():
    for path in (OBS / "grafana/dashboards").glob("*.json"):
        panels = json.loads(path.read_text(encoding="utf-8"))["panels"]
        ids = [p["id"] for p in panels]
        assert len(ids) == len(set(ids)), path.name
        assert all(p.get("title") for p in panels if p["type"] != "text"), path.name


def test_grafana_password_comes_from_the_environment_not_the_repo():
    compose = (OBS / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GRAFANA_ADMIN_PASSWORD:?" in compose
    assert "change-me" in (OBS / ".env.example").read_text(encoding="utf-8")
    forbidden = "-".join(("lab", "admin"))  # the password an earlier version hard-coded; must never reappear
    for path in OBS.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".sh", ".yml", ".yaml", ".md"}:
            assert forbidden not in path.read_text(encoding="utf-8"), path


def test_scrape_job_port_matches_the_launch_scripts():
    prom = _load(OBS / "prometheus/prometheus.yml")
    app_job = next(j for j in prom["scrape_configs"] if j["job_name"] == "llm-app")
    port = app_job["static_configs"][0]["targets"][0].split(":")[1]
    assert f"--port {port}" in (OBS / "scripts/up.sh").read_text(encoding="utf-8")
    assert f"--port {port}" in (OBS / "scripts/app.sh").read_text(encoding="utf-8")


def test_shell_scripts_have_valid_syntax():
    for script in [*OBS.glob("scripts/*.sh"), *(ROOT / "k8s/gpu").rglob("*.sh"), *(ROOT / "scripts").glob("*.sh")]:
        done = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert done.returncode == 0, f"{script}: {done.stderr}"


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_promtool_accepts_the_rule_files():
    for path in RULES:
        done = subprocess.run(["promtool", "check", "rules", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr
