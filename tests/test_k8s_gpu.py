"""Static checks on the GPU Kubernetes assets (the cluster itself is out of reach of CI)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

GPU = Path(__file__).resolve().parents[1] / "k8s/gpu"
CHART = GPU / "charts/vllm-gpu"


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


def test_chart_requests_exactly_one_integer_gpu_and_avoids_a_surge_pod():
    v = _values()
    assert v["gpu"] == {"resourceName": "nvidia.com/gpu", "count": 1}
    assert v["strategy"]["rollingUpdate"] == {"maxSurge": 0, "maxUnavailable": 1}  # one GPU = one pod at a time


def test_startup_probe_budget_outlasts_model_load_and_compile():
    v = _values()
    budget_s = v["probes"]["startup"]["failureThreshold"] * v["probes"]["startup"]["periodSeconds"]
    assert budget_s >= 1500 and v["progressDeadlineSeconds"] >= budget_s


def test_deployment_template_encodes_the_documented_gotchas():
    text = (CHART / "templates/deployment.yaml").read_text(encoding="utf-8")
    assert "enableServiceLinks: false" in text  # a Service called `vllm` otherwise injects VLLM_PORT=tcp://...
    assert "\n          command:" not in text  # `command:` would bypass the image ENTRYPOINT (CUDA forward-compat)
    assert "medium: Memory" in text  # /dev/shm is 64 MiB in a pod otherwise


def test_device_plugin_values_use_cdi_without_the_nvidia_runtime():
    v = yaml.safe_load((GPU / "device-plugin/values.yaml").read_text(encoding="utf-8"))
    assert v["deviceListStrategy"] == "cdi-cri" and v["securityContext"]["runAsUser"] == 65534
    ts = yaml.safe_load((GPU / "device-plugin/values-timeslicing.yaml").read_text(encoding="utf-8"))
    sharing = yaml.safe_load(ts["config"]["map"][ts["config"]["default"]])["sharing"]["timeSlicing"]
    assert sharing["resources"] == [{"name": "nvidia.com/gpu", "replicas": 2}]  # one GPU advertised as two slots


def test_manifests_are_valid_yaml_and_the_pending_demo_asks_for_two_gpus():
    for path in (GPU / "manifests").glob("*.yaml"):
        assert list(yaml.safe_load_all(path.read_text(encoding="utf-8"))), path.name
    pod = yaml.safe_load((GPU / "manifests/gpu-x2-pending.yaml").read_text(encoding="utf-8"))
    assert pod["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 2


def test_cluster_scripts_do_not_switch_the_users_kubectl_context():
    for script in GPU.rglob("*.sh"):
        for line in script.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("#"):
                assert "use-context" not in line, script


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_lints_and_renders_the_chart():
    assert subprocess.run(["helm", "lint", str(CHART)], capture_output=True).returncode == 0
    out = subprocess.run(["helm", "template", "vllm", str(CHART)], capture_output=True, text=True, check=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    limits = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits["nvidia.com/gpu"] == 1
