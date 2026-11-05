#!/usr/bin/env bash
# A chat completion through the Service (cluster DNS), then from the host through port-forward.
source "$(dirname "$0")/_common.sh"
URL=http://vllm.$NS.svc.cluster.local:8000
$K -n $NS get pod client >/dev/null 2>&1 || { $K apply -f manifests/curl-client.yaml >/dev/null; $K -n $NS wait --for=condition=Ready pod/client --timeout=120s >/dev/null; }
C="$K -n $NS exec client --"
run $C curl -s $URL/v1/models
echo; echo; echo "### chat completion from another pod: DNS name -> Service -> the vLLM pod"
BODY='{"model":"qwen3-4b","messages":[{"role":"system","content":"Answer in one sentence."},{"role":"user","content":"Why does a Kubernetes pod that requests 2 GPUs stay Pending on a node with 1 GPU?"}],"max_tokens":120,"temperature":0}'
echo "\$ curl -s -w '\\n[http=%{http_code} total=%{time_total}s]' $URL/v1/chat/completions -d '<body>'"
$C curl -s -w '\n[http=%{http_code} total=%{time_total}s]\n' $URL/v1/chat/completions -H 'content-type: application/json' -d "$BODY"
echo; echo "### 8 concurrent requests (vLLM batches them on the one GPU): wall time"
START=$(date +%s.%N)
$C sh -c "for i in 1 2 3 4 5 6 7 8; do curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' $URL/v1/chat/completions -H 'content-type: application/json' -d '{\"model\":\"qwen3-4b\",\"messages\":[{\"role\":\"user\",\"content\":\"Write two sentences about GPUs, request '\$i'.\"}],\"max_tokens\":100}' & done; wait"
echo "wall: $(echo "$(date +%s.%N) - $START" | bc) s"
echo; echo "### the same thing from the host (ssh session on the Spark) through kubectl port-forward"
$K -n $NS port-forward svc/vllm 18200:8000 >/dev/null 2>&1 &
PF=$!; sleep 3
echo "\$ curl -s localhost:18200/v1/models | head -c 300"; curl -s localhost:18200/v1/models | head -c 300; echo
kill $PF 2>/dev/null
