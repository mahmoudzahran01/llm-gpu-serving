# One pod that requests ONE "gpu" and burns it for 45 s. The demo renders it three times (ts-a, ts-b, ts-c) with
#   sed s/__NAME__/ts-a/ manifests/timeslice-pod.yaml.tpl | kubectl apply -f -
# With time-slicing replicas=2: a and b run on the same physical GPU, c stays Pending (only 2 slots).
apiVersion: v1
kind: Pod
metadata: {name: __NAME__, namespace: gpu-lab, labels: {app: timeslice}}
spec:
  restartPolicy: Never
  nodeSelector: {nvidia.com/gpu.present: "true"}
  containers:
  - name: c
    image: nvcr.io/nvidia/vllm:26.10-py3        # only for its torch; goes through the image ENTRYPOINT (CUDA compat setup)
    imagePullPolicy: IfNotPresent
    args:
    - bash
    - -c
    - |
      python3 - <<'PY'
      import os, time, torch
      free, total = torch.cuda.mem_get_info()
      print(f"{os.environ['HOSTNAME']}: sees {torch.cuda.get_device_name(0)}; free={free/2**30:.1f} GiB of total={total/2**30:.1f} GiB (whole unified memory, no per-pod cap)", flush=True)
      hold = torch.empty(2**30, dtype=torch.uint8, device='cuda')            # take 1 GiB so it is visible
      a = torch.randn(4096, 4096, device='cuda', dtype=torch.float16)
      t0 = time.time(); n = 0
      while time.time() - t0 < 45:
          for _ in range(20): a @ a
          torch.cuda.synchronize(); n += 20
      print(f"{os.environ['HOSTNAME']}: {n} matmuls in 45 s = {n/45:.0f}/s", flush=True)
      PY
    resources:
      limits: {nvidia.com/gpu: 1}
