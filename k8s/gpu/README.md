# vLLM on Kubernetes with a real GPU (DGX Spark GB10)

A hands-on lab that you can run, break and explain. It serves `Qwen/Qwen3-4B-Instruct-2507`
with vLLM as a Kubernetes `Deployment` that **requests `nvidia.com/gpu: 1`**, so the scheduler, the kubelet and the
NVIDIA device plugin really hand the GPU to the pod. Everything here was built and verified on 2026-11-03 on a
DGX Spark (Ubuntu, aarch64, GB10, driver 580.159.03). The raw command output behind every claim is in
`evidence/20-*.txt` ... `evidence/32-*.txt`.

> **Scope.** One host, one GPU, a *shared* machine, no root. Several steps here (copying driver files into a kind node, `chmod 0666` on the
> kubelet socket, running the device plugin as uid 65534) are lab-only workarounds for those constraints, each explained where it appears;
> they are not how you would run a production GPU pool. The `evidence/` files are raw command output from the run described below.
>
> **TLDR**
> - Works: device plugin (CDI mode, no nvidia runtime, no sudo) -> node advertises `nvidia.com/gpu: 1` -> vLLM pod
>   Ready in ~4.5 min -> chat completion through the Service -> `/metrics` scraped by the cluster's Prometheus.
> - Key scheduling evidence: a pod asking for 2 GPUs stays `Pending` with `Insufficient nvidia.com/gpu`
>   (evidence/24); a rolling update with the Kubernetes default strategy gets **stuck** on one GPU, `maxSurge: 0`
>   fixes it at the price of ~4 min of downtime (evidence/28); time-slicing turns 1 GPU into 2 slots with **zero**
>   memory isolation (evidence/29).
> - Did NOT work: a **separate** `lab-gpu` cluster. A new kind node cannot even boot because uid 0's inotify quota
>   on this box is exhausted and I have no sudo (evidence/20). The lab therefore uses **one worker of the existing
>   `lab` kind cluster** (namespaces `gpu-lab` and `nvidia-device-plugin`); nothing else on that cluster was touched.
> - Four real bugs on the way (each has an evidence file): inotify quota, `command:` bypassing the NGC entrypoint,
>   `VLLM_PORT` service-link collision, vLLM's start-up free-memory check.

**Contents**
1. [What you built](#1-what-you-built)
2. [Why not a separate cluster (what I tried)](#2-why-not-a-separate-cluster)
3. [How a pod gets the GPU: the chain, object by object](#3-how-a-pod-gets-the-gpu)
4. [Run it](#4-run-it)
5. [Evidence index](#5-evidence-index)
6. [Gotchas, in the order they bit me](#6-gotchas)
7. [Eight questions this lab answers, with evidence](#7-eight-questions-this-lab-answers)
8. [What this does not cover / exercises](#8-what-this-does-not-cover)
9. [If you get sudo: the separate-cluster design](#9-if-you-get-sudo)

---

## 1. What you built

```
 host (DGX Spark, 121 GB unified memory, ONE GB10, driver 580.159.03, other tenants train/serve on it)
 |
 |  /dev/nvidia0 ... + /usr/lib/aarch64-linux-gnu/libcuda.so.580.159.03 ...
 |
 +-- docker: kind cluster "lab"  (k8s v1.37.0, containerd 2.3.4)  <- an existing multi-node kind cluster
      |
      +-- lab-control-plane
      +-- lab-worker            (other workloads, no GPU)
      +-- lab-worker2   <== the "GPU node"  label nvidia.com/gpu.present=true
           |  /opt/nvidia-driver/...   copy of the host's user-space driver files   (scripts/node-inject.sh)
           |  /models/Qwen3-4B-Instruct-2507   7.6 GB weights, node-local disk
           |  /var/cache/vllm                  torch.compile cache, survives pod restarts
           |
           |  ns nvidia-device-plugin   DaemonSet nvdp  (Helm, runs as uid 65534, privileged)
           |        NVML -> finds 1 GPU -> writes /var/run/cdi/k8s.device-plugin.nvidia.com-gpu.json
           |        registers "nvidia.com/gpu" with the kubelet  -> node Capacity/Allocatable nvidia.com/gpu: 1
           |
           |  ns gpu-lab                Deployment vllm  (Helm chart charts/vllm-gpu)
           |        limits: nvidia.com/gpu: 1   startup/readiness/liveness on /health   shm emptyDir   hostPath model
           |        Service vllm :8000  --ServiceMonitor-->  kube-prometheus-stack (ns monitoring, existing)
           v
        in-cluster client:  curl http://vllm.gpu-lab.svc.cluster.local:8000/v1/chat/completions
```

Repository layout (`k8s/gpu/`):

| Path | What it is |
|---|---|
| `up.sh`, `down.sh` | bring the whole thing up / tear it down (idempotent, no sudo) |
| `charts/vllm-gpu/` | the Helm chart: Deployment, Service, optional ServiceMonitor |
| `device-plugin/values.yaml` | NVIDIA device plugin 0.20.1 in CDI mode, non-root |
| `device-plugin/values-timeslicing.yaml` | layer that advertises 1 GPU as 2 |
| `scripts/node-inject.sh` | copies driver files + weights into ONE node, labels it, fixes socket permissions |
| `scripts/make-node-cdi-spec.py` | rewrites the host CDI spec for a node that holds a copy of the driver (debug only) |
| `scripts/demo-2x-*.sh` | one script per experiment; each produced the matching `evidence/2x-*.txt` |
| `scripts/probe-inotify.sh` | reproduces the "new kind node cannot boot" finding |
| `manifests/` | smoke pods, the 2-GPU pending pod, time-slicing burner template, curl client |
| `kind/gen-kind-config.sh` | **untested beyond YAML generation** - the config for a separate cluster, see section 9 |

## 2. Why not a separate cluster

A separate GPU-capable cluster (k3d or kind+CDI) was the first choice. I tried; this is what happened.

1. Wrote `kind/gen-kind-config.sh`: 1 control-plane + 1 worker, driver files bind-mounted with `extraMounts`
   (read from the host CDI spec), HF cache mounted, GPU taint, NodePort mapping. `kind create cluster` failed twice
   within 5 s:
   `Failed to create control group inotify object: Too many open files` - **systemd, PID 1 of the kind node, dies at boot.**
2. Cause: `fs.inotify.max_user_instances = 128` and the quota is **per uid**. Every container root process on the box
   is uid 0 (kubelets, containerd-shims, systemd of the 3 existing kind nodes, 40 transient `udev-worker`s, ...).
   Measured with `inotify_init1` in a loop: uid 0 can create **0** more instances; uid 1000 and uid 65534 can create all
   of them (evidence/20). A second cluster needs several dozen (one kind node's systemd + kubelet alone hold ~11, each pod's containerd-shim 2); there are none, and raising the sysctl needs root
   (`sudo -n` -> "a password is required").
3. Rootless alternatives checked and ruled out: `unshare -Ur` fails (`kernel.apparmor_restrict_unprivileged_userns=1`),
   no `newuidmap`/podman/rootlesskit installed, Docker has no per-container userns. I did not use a privileged
   container to write the sysctl: that is root by another door on a box shared with other services.
4. What I did instead: **add the GPU to one worker of the existing cluster**, in its own namespaces, without touching
   the other workloads on the cluster, their pods, or the other two nodes. Prices paid: the copy of the driver files, a few lab-only
   permission changes on that node (section 6, items 2 and 3), and the cluster is shared, so I could not use a
   node taint without affecting the other workloads' autoscaling (the GPU node is selected by label only).

This also produced the first real lesson: **the same quota that stopped the cluster also crash-looped the device
plugin** (section 6.1).

## 3. How a pod gets the GPU

```
 Deployment vllm: resources.limits["nvidia.com/gpu"] = 1
   |
   | 1. SCHEDULER  filters nodes on allocatable nvidia.com/gpu >= request (integer, never overcommitted)
   |      no node fits -> Pending, event "Insufficient nvidia.com/gpu"                       (evidence/24)
   v
 2. KUBELET (device manager)  asks the registered plugin: Allocate(device id)
   |
   v
 3. DEVICE PLUGIN  answers with a CDI device NAME "k8s.device-plugin.nvidia.com/gpu=GPU-72c8f06b-..."
   |      (deviceListStrategy: cdi-cri)  - the plugin wrote that spec earlier from NVML + the driver files
   v
 4. kubelet -> CRI -> CONTAINERD (enable_cdi=true, spec dirs /etc/cdi and /var/run/cdi)
   |      edits the container's OCI config: + /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm ..., + libcuda & friends as bind
   |      mounts, + a createContainer hook (nvidia-cdi-hook) that fixes symlinks and the ld cache
   v
 5. runc starts the container.  No nvidia runtime, no runtimeClass, no NVIDIA_VISIBLE_DEVICES magic.
```

Consequences you can see in `evidence/23`: the pod that **requests** the GPU sees `/dev/nvidia0` and `nvidia-smi -L`;
the identical pod **without** the request, forced onto the same node, sees no device and no `nvidia-smi`.

Object by object, in `charts/vllm-gpu`:

| Object / field | Why it is there |
|---|---|
| `resources.limits.nvidia.com/gpu: 1` | The only GPU-specific line. Extended resources go in `limits` (request defaults to the limit). |
| `nodeSelector nvidia.com/gpu.present=true` + toleration for `nvidia.com/gpu` | keep it on the GPU node. On a real GPU pool you also **taint** the nodes (`nvidia.com/gpu=present:NoSchedule`) so CPU-only pods do not squat there; the toleration is a no-op here. |
| `args: [vllm, serve, ...]` and **no** `command:` | `command` would replace the NGC image ENTRYPOINT; see gotcha 4. |
| `--kv-cache-memory 4294967296` + `--gpu-memory-utilization 0.15` | pin the KV cache at 4 GiB; the utilisation value only feeds vLLM's start-up "enough free memory?" check. See Q6. |
| `startupProbe` /health, 180 x 10 s | model load + torch.compile + CUDA-graph capture is 4+ minutes; liveness must not start until it passes. |
| `readinessProbe` / `livenessProbe` /health | readiness gates Service endpoints; liveness restarts a wedged engine. |
| `emptyDir {medium: Memory}` on `/dev/shm` | `docker run` used `--ipc=host`; a pod gets a 64 MiB `/dev/shm` otherwise. |
| `hostPath /models/...` (ro) and `/var/cache/vllm` | weights on node-local disk; compile cache survives restarts (a restart skips minutes of compile). Production: PVC / CSI object-store mount / init download. |
| `strategy: maxSurge 0, maxUnavailable 1` | one GPU = one pod at a time (Q3). |
| `progressDeadlineSeconds: 1800` | the default 600 s would flag a perfectly normal slow start as failed. |
| `enableServiceLinks: false` | gotcha 5. |
| `resources.requests cpu 4 / memory 12Gi`, `limits memory 40Gi` | schedulable CPU/RAM next to the other pods; no CPU limit (no throttling of the engine loop). |

## 4. Run it

**Prerequisites** (not installed by this repository): a Linux host with an NVIDIA GPU and driver, Docker, `kind`, `kubectl`, `helm`,
the Hugging Face cache holding `Qwen/Qwen3-4B-Instruct-2507`, and `nvidia-ctk cdi generate --output=/var/run/cdi/nvidia.yaml` run once.
Create the base cluster with `kind create cluster --config kind/lab-cluster.yaml` (context `kind-lab`); `monitoring/kube-prometheus-values.yaml`
is only needed for experiment 27. The scripts assume the aarch64 library paths of a DGX Spark; on x86 adjust `scripts/node-inject.sh`.

```bash
# from the repository root
cd k8s/gpu
./up.sh                    # GPU plumbing + vLLM. First ever run also kind-loads the 27 GB image (~9 min)
SKIP_VLLM=1 ./up.sh        # plumbing only, uses no GPU memory
./down.sh                  # remove vLLM + test pods (frees the GPU). Plumbing stays, so up.sh is fast
./down.sh plumbing         # + device plugin, label, namespaces
./down.sh all              # + driver copy, weights, images from the node (clean slate)
```

`up.sh` refuses to start vLLM until `free -g` shows >= 25 GB available (this box is shared): it polls every
5 minutes. Always pass `--context kind-lab` (the scripts do); never `kubectl config use-context`: other
tooling on this box expects the current context to stay as it is.

```bash
K="kubectl --context kind-lab"
$K describe node lab-worker2 | grep -E "nvidia.com/gpu"          # capacity / allocatable / allocated
$K -n gpu-lab get pods,svc,deploy -o wide
$K -n gpu-lab logs -f deploy/vllm                                      # start-up takes ~4.5 min
$K -n gpu-lab port-forward svc/vllm 18200:8000 &
curl -s localhost:18200/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"qwen3-4b","messages":[{"role":"user","content":"hi"}],"max_tokens":50}'
$K apply -f manifests/gpu-x2-pending.yaml; $K -n gpu-lab describe pod wants-two-gpus   # Pending + reason
```

Re-run any experiment: `scripts/demo-2x-*.sh > evidence/2x-....txt` (needs vLLM Ready for 26-28; 29 stops vLLM).

Scrape from the host Prometheus (`obs/`, :9090) if you want it there too: port-forward or publish the Service and add
```yaml
- job_name: vllm-k8s
  metrics_path: /metrics
  static_configs: [{targets: ['127.0.0.1:18200']}]   # kubectl port-forward svc/vllm 18200:8000
```
I did not edit `obs/` config; the in-cluster `kps` Prometheus (ServiceMonitor, evidence/27) is the tested path.

## 5. Evidence index

| File | What it proves |
|---|---|
| `20-new-cluster-blocked-inotify.txt` | a new kind node dies at boot (systemd inotify); per-uid quota: uid 0 = 0 free, uid 1000/65534 = free; who holds uid-0 instances; `sudo -n` refused |
| `21-device-plugin-failures-and-fix.txt` | plugin as root crash-loops (`couldn't initialize inotify`); as uid 65534 it cannot `connect()` to `kubelet.sock` (`context deadline exceeded`); both fixed; registered |
| `22-node-gpu-capacity.txt` | `kubectl describe node`: `nvidia.com/gpu` Capacity/Allocatable/Allocated; CDI spec written by the plugin; GB10 memory is `[N/A]` in NVML |
| `23-smoke-gpu-vs-no-gpu.txt` | pod with the request sees `/dev/nvidia*` + `nvidia-smi -L`; same pod without it sees nothing |
| `24-pending-two-gpus.txt` | **2-GPU pod stays Pending: `0/3 nodes are available: ... 2 Insufficient nvidia.com/gpu`** |
| `25-vllm-deploy-and-start.txt` | helm install -> Ready in 262 s; start-up milestones; probes, resources, events; node shows GPU 1/1 allocated; host `nvidia-smi` sees `VLLM::EngineCore` (13.3 GB) |
| `25b-startup-free-memory-check.txt` | CrashLoop #1: `Free memory 24.15 GiB < desired 0.2 x 121.69 = 24.34 GiB` |
| `25c-ptx-error-entrypoint-bypass.txt` | CrashLoop #2: `command:` bypassed the NGC entrypoint -> `CUDA_ERROR_UNSUPPORTED_PTX_VERSION` / `No device code available for GPU ISA 121` |
| `25d-vllm-port-service-link-collision.txt` | CrashLoop #3: `VLLM_PORT=tcp://10.96.x.x:8000` injected by the Service named `vllm` |
| `26-chat-completion-via-service.txt` | `/v1/models` and a chat completion via the Service DNS name from another pod; 8 concurrent requests; host port-forward |
| `27-metrics-scrape-prometheus.txt` | `/metrics` series, ServiceMonitor, target `up`, PromQL (`rate(vllm:generation_tokens_total)`, mean TTFT) in the cluster Prometheus |
| `28-rolling-update-one-gpu.txt` | default strategy: new pod Pending (`1 Insufficient nvidia.com/gpu`), rollout stuck, old pod still serves; `maxSurge 0`: 236 s outage, availability timeline from an in-cluster prober |
| `29-time-slicing.txt` | replicas=2: capacity 1 -> 2, two pods on one GPU, third Pending, each pod sees the whole 121.7 GiB, throughput split |
| `30-up-sh-full-run.txt` | `up.sh` end to end (idempotent re-run on a warm node) |
| `31-cdi-annotation-ignored.txt` | a pod annotated `cdi.k8s.io/...: nvidia.com/gpu=all` and **no** GPU request gets no device on containerd |
| `32-teardown.txt` | `down.sh`: no vLLM pod, no GPU memory held by this lab |

## 6. Gotchas

1. **inotify quota (per uid) kills node daemons.** `failed to create FS watcher ... couldn't initialize inotify: too many
   open files` is `EMFILE` from `inotify_init1`, not a file-descriptor limit. Check with the probe in
   `scripts/probe-inotify.sh`. Fix in prod: `sudo sysctl fs.inotify.max_user_instances=1024` (kind docs recommend 512).
   Workaround here: run the plugin as uid 65534 - the quota is counted per uid (evidence/20, 21).
2. **A non-root client cannot `connect()` to a root-owned unix socket without write permission.** `kubelet.sock` is
   `srwxr-xr-x root`; the plugin logged `Could not register device plugin: context deadline exceeded` (evidence/21 B).
   Lab fix: `chmod 0666` on the socket (+ `0777` on `/var/lib/kubelet/device-plugins` and `/var/run/cdi`). The kubelet
   recreates the socket when it restarts, so after a node restart re-run `scripts/node-inject.sh`.
3. **CDI via pod annotations is not what containerd uses.** `cdi.k8s.io/...` annotations (the CRI-O style) did nothing here
   (evidence/31). On containerd the device plugin must return CDI names through the CRI field: `deviceListStrategy: cdi-cri`.
4. **`command:` replaces the image ENTRYPOINT.** The NGC vLLM image is built with CUDA 13.4 but the host driver is
   580 (CUDA 13.0). Its `nvidia_entrypoint.sh` runs the CUDA forward-compat check; with `command: [vllm, ...]` it never ran
   and a PTX-only flash-attention kernel could not be JIT-compiled (evidence/25c). Use `args:` only.
5. **A Service called `vllm` injects `VLLM_PORT=tcp://...` into every pod in the namespace.** vLLM reads `VLLM_PORT` as its own
   setting and aborts - after the model has loaded and compiled, so each attempt costs minutes (evidence/25d).
   `enableServiceLinks: false`.
6. **vLLM's start-up check on a shared GPU.** `--gpu-memory-utilization` x total must be FREE at start-up, whatever
   `--kv-cache-memory` says. At 0.2 the pod needed 24.34 GiB free and the other tenants had left 24.15 (evidence/25b); the pod
   crash-looped until memory moved. The chart defaults to 0.15 (18.3 GiB; real footprint ~15 GiB).
7. **Rolling update + one GPU.** See Q3.
8. **GB10 specifics.** No device-plugin flags were needed: NVML works and the plugin only counts devices. NVML cannot
   report memory (`[N/A]`, unified memory); per-process usage is visible with `nvidia-smi --query-compute-apps`.
   Every pod sees the whole machine's memory (`torch.cuda.mem_get_info` -> 121.7 GiB total). MIG does not exist on GB10.
9. **Nothing in the cluster limits GPU memory.** The pod's `memory: 40Gi` limit is a cgroup limit on CPU-side memory;
   it does not bound what CUDA allocates.
10. Latency numbers in evidence/26 (6 s for a 35-token answer) are not vLLM's speed: the GPU runs at ~96 % utilisation for
    another tenant's training job at the same time.

## 7. Eight questions this lab answers

**Q1. Walk me through what happens between `limits: nvidia.com/gpu: 1` and a CUDA process running in the container.**
The device plugin (a DaemonSet) found the GPU through NVML and registered the extended resource `nvidia.com/gpu` with the
kubelet over `/var/lib/kubelet/device-plugins/kubelet.sock`; the node then reports Capacity/Allocatable `nvidia.com/gpu: 1`
(evidence/22). The scheduler treats it like any integer resource: it only picks nodes with enough allocatable units and
the kubelet's accounting shows `nvidia.com/gpu 1 1` once the pod is running (evidence/25). At container start the kubelet
calls `Allocate` on the plugin, which returns a CDI device name; containerd resolves it against the spec the plugin wrote in
`/var/run/cdi`, adds `/dev/nvidia*`, the driver libraries and an ld-cache hook to the OCI config, and runc starts the
container (evidence/23). Here that works without the nvidia container runtime at all.

**Q2. A pod requests 2 GPUs and the only GPU node has 1. What do you see, and what would you do about it?**
The pod stays `Pending`, with `FailedScheduling: 0/3 nodes are available: 1 node(s) had untolerated taint(s), 2
Insufficient nvidia.com/gpu` (evidence/24). Nothing is queued on the GPU and nothing times out; the scheduler retries
when node resources change. `Insufficient nvidia.com/gpu` means a quantity problem, `didn't match Pod's node
affinity/selector` or `untolerated taint` mean a placement problem - read which one. Fixes: request fewer GPUs, add a GPU
node (cluster autoscaler), or run a queueing layer (Kueue / Volcano) for gang scheduling and fair-share instead of
leaving pods Pending forever.

**Q3. Why does a normal rolling update of a single-GPU vLLM Deployment hang, and what are the options?**
The default strategy is `maxSurge 25%` (rounds up to 1) and `maxUnavailable 25%` (rounds down to 0): create the new pod first,
remove the old one when the new is Ready. The new pod needs a second GPU, so it is `Pending` with `1 Insufficient
nvidia.com/gpu`, the rollout never finishes, and the old pod keeps serving (evidence/28: 38+ good probes while stuck, `rollout
status` timing out). Setting `maxSurge: 0, maxUnavailable: 1` terminates the old pod first, frees the GPU and lets the new one
start; the price is an outage of the whole start-up: 236 s here (160 probes, 117 failed). `strategy: Recreate` does the same
thing more explicitly. To avoid the outage you need a second GPU for the surge (blue/green on another node), or a warm standby
elsewhere. Also raise `progressDeadlineSeconds`, otherwise a slow-but-normal start is reported as failed after 10 minutes.

**Q4. How do you set the probes for vLLM, and what does the start-up sequence look like?**
`startupProbe` on `/health` with a big budget (180 x 10 s here), readiness on `/health` to gate the Service endpoints, liveness
on `/health` to restart a hung engine. Without the startup probe, liveness would start killing the pod during model load
and compile. Evidence/25: Pending 0 s -> container running 20 s -> weights 16 s -> model loaded 27 s -> torch.compile ->
CUDA-graph capture 93 s -> Ready at 262 s, with 24 expected `Startup probe failed: connection refused` events. The compile cache on a
hostPath (`VLLM_CACHE_ROOT`) makes restarts cheaper; the rolling update in evidence/28 still took 245 s because graph capture
and weight loading are not cached.

**Q5. Time-slicing: what do you get, and why is it risky for vLLM?**
`sharing.timeSlicing.resources[].replicas: 2` makes the plugin advertise two `nvidia.com/gpu` units for one device (evidence/29:
allocatable 1 -> 2, pods a and b Running on the same GPU, c Pending). That is accounting only: the driver time-slices the CUDA
contexts, there is **no memory isolation** (each pod saw `total=121.7 GiB`, the entire machine) and **no QoS** (a alone: 198
matmuls/s; a and b together: 144 and 141/s, on a GPU other tenants were also using, so read it as "the share is whatever the
scheduler gives you"). vLLM claims its memory up front (weights + a fixed KV cache) and checks free memory at start-up, so
two vLLM pods on one slice either fail the check, or one pushes the other into an OOM during a burst; latency also jumps with
every context switch. Use it for many small, bursty, memory-light jobs (notebooks, tests). For serving prefer a whole GPU, MIG
(not available on GB10), or MPS with memory limits; on a shared box like this one, keep one vLLM per GPU.

**Q6. Why both `--kv-cache-memory` and `--gpu-memory-utilization` on a shared GPU?**
By default vLLM sizes the KV cache as "utilisation x total - what the model and activations used", which is unpredictable
when other processes allocate in the same unified memory. `--kv-cache-memory` fixes the KV cache in bytes (4 GiB -> 29,120
tokens, 7.11 concurrent 4096-token requests, evidence/25). But vLLM still runs a start-up check that
`free >= utilisation x total`; the log says so: `Free memory ... (24.15/121.69 GiB) on startup is less than desired GPU memory
utilization (0.2, 24.34 GiB)` (evidence/25b). So the utilisation number is a start-up gate, not a cap: choose it just above the real
footprint (13.3 GB process in `nvidia-smi`, ~15 GB budgeted).

**Q7. Name two ways a container that works with `docker run` fails on Kubernetes, from this lab.**
(1) Entry point: `docker run IMAGE vllm serve ...` keeps the image ENTRYPOINT; a Pod's `command:` replaces it. The NGC entrypoint
sets up CUDA forward compatibility, without it `CUDA_ERROR_UNSUPPORTED_PTX_VERSION` (evidence/25c). (2) Environment: Kubernetes injects
`<SERVICE>_PORT=tcp://ip:port` for every Service in the namespace; a Service called `vllm` produced `VLLM_PORT=tcp://...` and vLLM
exited (evidence/25d). Also: `/dev/shm` (`--ipc=host` vs 64 MiB), no `--gpus` flag (the resource limit is the interface), and
the default probe/deadline timings.

**Q8. The device-plugin pod is in CrashLoopBackOff with "too many open files". How do you debug it?**
`EMFILE` from `inotify_init1` is the per-user inotify instance quota (`fs.inotify.max_user_instances`), not the per-process fd limit.
Check by trying to create instances as that uid (evidence/20: uid 0 gets 0, uid 65534 gets all). Everything running as root
on the host shares one quota, including every container's root, so a crowded node runs out; the same quota stopped a new kind node
from booting. Proper fix: raise the sysctl on the node (needs root); workaround used here: run the plugin as another uid, then
fix what that exposes (socket permissions, evidence/21). The general lesson: node-level daemons are the first things to fail
on a crowded host; look at `kubectl logs <pod>` (current, not `--previous`, when the container never got far), `kubectl
describe pod`, and the kubelet's view of the device (`kubectl describe node`).

## 8. What this does not cover

- Multi-GPU / tensor-parallel (`nvidia.com/gpu: 2`, NCCL, topology); this box has one GPU.
- GPU Operator, GPU Feature Discovery / NFD labels (`nvidia.com/gpu.product`, `.memory`); DCGM-exporter in the cluster
  (the host's `obs` stack already has DCGM on :9400).
- Autoscaling vLLM (HPA on `vllm:num_requests_waiting`, KEDA), request routing across replicas, prefix-cache-aware routing.
- A taint on the GPU pool (the GPU node is shared with other workloads here).
- Model weights as a PVC / OCI artifact / init-container download.

Exercises: (1) add `nvidia.com/gpu: 1` to a second Deployment and watch it Pending behind vLLM; (2) switch the
strategy to `Recreate` and compare the outage; (3) make the readiness probe check `/v1/models` instead of `/health`;
(4) HPA/KEDA on `vllm:num_requests_waiting` (needs a 2nd GPU or time-slicing); (5) rewrite the plugin install to run
as root after raising the sysctl, and delete the permission hacks.

## 9. If you get sudo

A separate cluster is cleaner (own taint, no shared node, nothing to undo on `lab`):

```bash
sudo sysctl -w fs.inotify.max_user_instances=1024        # persist in /etc/sysctl.d/99-inotify.conf
cd k8s/gpu && CLUSTER=lab-gpu kind/gen-kind-config.sh > kind/kind-config.generated.yaml
kind create cluster --config kind/kind-config.generated.yaml --kubeconfig ~/.kube/lab-gpu.config
```
`gen-kind-config.sh` bind-mounts the driver files listed in the host CDI spec at the same paths, the HF cache, the CDI hook
binary, taints the worker and publishes NodePort 30200 on 127.0.0.1:18200. **I could not boot it** (only the YAML
generation and parsing were checked), so treat it as a starting point. With root you could also run the device plugin as
root and drop the `chmod`s in `node-inject.sh`; and with `nvidia-ctk runtime configure` + Docker's default runtime `nvidia`
the classic `nvkind` approach (`/dev/null` -> `/var/run/nvidia-container-devices/all` mount) works too.
