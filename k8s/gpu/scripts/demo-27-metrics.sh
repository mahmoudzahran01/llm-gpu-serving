#!/usr/bin/env bash
# vLLM /metrics -> ServiceMonitor -> the cluster's kube-prometheus-stack (release kps, serviceMonitorSelector {}).
# (that Prometheus runs with routePrefix /prom, hence the /prom/api/... URLs)
source "$(dirname "$0")/_common.sh"
C="$K -n $NS exec client --"
URL=http://vllm.$NS.svc.cluster.local:8000
echo "### /metrics straight from the pod (selection)"
$C curl -s $URL/metrics | grep -E '^(vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc|gpu_cache_usage_perc|prompt_tokens_total|generation_tokens_total|request_success_total|time_to_first_token_seconds_count|time_to_first_token_seconds_sum|e2e_request_latency_seconds_count|prefix_cache_(queries|hits)_total))' | cut -c1-200
echo; run helm --kube-context $CTX upgrade vllm charts/vllm-gpu -n $NS --reuse-values --set serviceMonitor.enabled=true
$K -n $NS get servicemonitor vllm -o yaml | sed -n '/^spec:/,$p'
echo; echo "### generate some traffic so rate() has data"
$C sh -c "for i in 1 2 3 4 5 6 7 8 9 10 11 12; do curl -s -o /dev/null $URL/v1/chat/completions -H 'content-type: application/json' -d '{\"model\":\"qwen3-4b\",\"messages\":[{\"role\":\"user\",\"content\":\"Explain device plugins in 3 sentences.\"}],\"max_tokens\":150}' & done; wait"
sleep 20
$K -n monitoring port-forward svc/kps-prometheus 19191:9090 >/dev/null 2>&1 &
PF=$!; sleep 3
q() { echo; echo "\$ PromQL: $1"; curl -s localhost:19191/prom/api/v1/query --data-urlencode "query=$1" | python3 -c 'import sys,json; r=json.load(sys.stdin)["data"]["result"]; [print("  ", {k:v for k,v in x["metric"].items() if k in ("__name__","pod","namespace","service","model_name","engine")}, "=>", x["value"][1]) for x in r] or print("   (no series)")'; }
echo; echo "### Prometheus target for gpu-lab"
curl -s localhost:19191/prom/api/v1/targets | python3 -c 'import sys,json; [print("  ", t["labels"].get("namespace"), t["labels"].get("pod"), t["health"], t["scrapeUrl"], t["lastScrapeDuration"] if "lastScrapeDuration" in t else "") for t in json.load(sys.stdin)["data"]["activeTargets"] if t["labels"].get("namespace")=="gpu-lab"]'
q 'up{namespace="gpu-lab"}'
q 'vllm:num_requests_running'
q 'sum(vllm:generation_tokens_total)'
q 'sum(rate(vllm:generation_tokens_total[1m]))'
q 'sum(vllm:time_to_first_token_seconds_sum) / sum(vllm:time_to_first_token_seconds_count)'
kill $PF 2>/dev/null
