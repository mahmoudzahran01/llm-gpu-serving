#!/usr/bin/env bash
# One GPU, one pod: what a rolling update does with the Kubernetes DEFAULT strategy vs maxSurge=0.
source "$(dirname "$0")/_common.sh"
C="$K -n $NS exec client --"
URL=http://vllm.$NS.svc.cluster.local:8000
# in-cluster availability probe: one request every 2 s, timestamped, for the whole experiment
$K -n $NS delete pod prober --ignore-not-found --wait=true >/dev/null
$K -n $NS run prober --image=curlimages/curl:8.11.1 --restart=Never --command -- sh -c \
  "while true; do code=\$(curl -s -o /dev/null -m 3 -w '%{http_code}' $URL/health); echo \"\$(date +%T) \$code\"; sleep 2; done" >/dev/null
$K -n $NS wait --for=condition=Ready pod/prober --timeout=60s >/dev/null
T0=$(date +%s)
echo "### 0. before: strategy in the Deployment"
run $K -n $NS get deploy vllm -o jsonpath='{.spec.strategy}{"\n"}'
run $K -n $NS get pods -l app.kubernetes.io/name=vllm-gpu -o wide

echo; echo "### 1. roll out a new revision with the Kubernetes DEFAULT strategy (maxSurge 25% -> 1, maxUnavailable 25% -> 0)"
echo "(the change is only a pod annotation: any pod-template change creates a new ReplicaSet, e.g. a new image tag or flag)"
run helm --kube-context $CTX upgrade vllm charts/vllm-gpu -n $NS --reuse-values \
  --set podAnnotations.rollout=v2 --set strategy.rollingUpdate.maxSurge=1 --set strategy.rollingUpdate.maxUnavailable=0
sleep 45
run $K -n $NS get pods -l app.kubernetes.io/name=vllm-gpu -o wide
run $K -n $NS get rs -l app.kubernetes.io/name=vllm-gpu
echo; echo "\$ kubectl describe pod <new pod> | Events"
NEW=$($K -n $NS get pods -l app.kubernetes.io/name=vllm-gpu --field-selector=status.phase=Pending -o name | head -1)
$K -n $NS describe $NEW | sed -n '/^Events:/,$p'
echo "\$ kubectl rollout status deploy/vllm --timeout=30s"; $K -n $NS rollout status deploy/vllm --timeout=30s 2>&1 | tail -2
echo "(old pod still Ready and serving - the rollout is safe but STUCK; progressDeadlineSeconds would eventually flag it)"
echo "prober so far:"; $K -n $NS logs prober | awk '{c[$2]++} END {for (k in c) print "   http " k ": " c[k] " probes"}'

echo; echo "### 2. fix the strategy: maxSurge 0, maxUnavailable 1 -> old pod is deleted first, its GPU is freed, the pending pod schedules"
T1=$(date +%s)
run helm --kube-context $CTX upgrade vllm charts/vllm-gpu -n $NS --reuse-values \
  --set podAnnotations.rollout=v2 --set strategy.rollingUpdate.maxSurge=0 --set strategy.rollingUpdate.maxUnavailable=1
for _ in $(seq 1 120); do
  r=$($K -n $NS get deploy vllm -o jsonpath='{.status.readyReplicas}/{.status.updatedReplicas}/{.status.replicas}'); echo "+$(( $(date +%s) - T1 ))s ready/updated/total=$r  $($K -n $NS get pods -l app.kubernetes.io/name=vllm-gpu --no-headers -o custom-columns=P:.metadata.name,S:.status.phase,R:.status.containerStatuses[0].ready | tr '\n' ' ')"
  [ "$r" = "1/1/1" ] && break; sleep 10
done
echo; echo "### 3. availability timeline from the in-cluster prober (state changes only)"
$K -n $NS logs prober | awk '{ if ($2 != last) { print; last=$2 } }'
echo "total probes: $($K -n $NS logs prober | wc -l)   non-200: $($K -n $NS logs prober | grep -vc ' 200$')"
$K -n $NS delete pod prober --wait=false >/dev/null
run $K -n $NS get pods,rs -l app.kubernetes.io/name=vllm-gpu
