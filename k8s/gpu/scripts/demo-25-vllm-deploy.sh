#!/usr/bin/env bash
# helm install the vllm-gpu chart and watch the pod go Pending -> ContainerCreating -> Running -> Ready.
source "$(dirname "$0")/_common.sh"
run free -g
wait_for_memory
$K get ns $NS >/dev/null 2>&1 || $K create ns $NS
T0=$(date +%s)
run helm --kube-context $CTX upgrade --install vllm charts/vllm-gpu -n $NS
echo; echo "### watching (one line per 20 s)"
for _ in $(seq 1 90); do
  line=$($K -n $NS get pod -l app.kubernetes.io/name=vllm-gpu --no-headers -o custom-columns='POD:.metadata.name,READY:.status.containerStatuses[0].ready,STARTED:.status.containerStatuses[0].started,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount' 2>/dev/null | head -1)
  echo "+$(( $(date +%s) - T0 ))s  $line"
  ready=$($K -n $NS get deploy vllm -o jsonpath='{.status.readyReplicas}' 2>/dev/null); [ "${ready:-0}" -ge 1 ] && break
  sleep 20
done
echo; echo "### time to Ready: $(( $(date +%s) - T0 )) s"
echo; echo "### vLLM start-up milestones from the pod log"
$K -n $NS logs deploy/vllm | grep -E "Model loading took|torch.compile takes|Available KV cache|GPU KV cache size|Maximum concurrency|Capturing CUDA graph|Graph capturing finished|Application startup complete|engine.*init|Starting vLLM|non-default args|Loading weights took" | cut -c1-230 | head -30
echo; run $K -n $NS get pods,svc,deploy -o wide
echo; echo "\$ kubectl describe pod (resources, probes, volumes, events)"
$K -n $NS describe pod -l app.kubernetes.io/name=vllm-gpu | sed -n '/^Containers:/,/^Conditions:/p;/^Events:/,$p' | grep -vE "^\s*(SERVICE|KUBERNETES)" 
echo; echo "### node accounting: the GPU is now fully allocated"
$K describe node $GPU_NODE | sed -n '/^Allocated resources:/,/^Events:/p' | grep -E "Resource|nvidia|cpu|memory" 
echo; echo "### host view of the same process (GPU memory is unified: nvidia-smi shows per-process usage only)"
run nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
run free -g
