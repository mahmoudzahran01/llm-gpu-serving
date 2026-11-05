#!/usr/bin/env bash
# Evidence for "why there is no separate lab-gpu cluster on this box":
# a new kind node (systemd as PID 1) cannot start because uid 0's inotify-instance quota is used up.
# Safe to re-run: the probe cluster is deleted at the end; the perl probes hold <= 20 instances for milliseconds.
export PATH=$HOME/.local/bin:$PATH
cd "$(dirname "$0")/.."
echo "\$ sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches"
sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches
echo; echo "\$ sudo -n true   (can I raise the limit?)"; sudo -n true 2>&1 | head -1
echo; echo "\$ kind create cluster --name lab-gpu-probe --retain   (kind v0.33, same node image as lab)"
KUBECONFIG=/tmp/lab-gpu-probe.kubeconfig kind create cluster --name lab-gpu-probe --retain --wait 30s 2>&1 | tail -6
echo; echo "\$ docker logs lab-gpu-probe-control-plane | tail -6      # systemd (PID 1 of the node) dies at boot"
docker logs lab-gpu-probe-control-plane 2>&1 | tail -6 | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'
kind delete cluster --name lab-gpu-probe >/dev/null 2>&1; rm -f /tmp/lab-gpu-probe.kubeconfig
echo; echo "== how many inotify instances can each uid still create right now? (inotify_init1 in a loop, capped at 20)"
PROBE='my $n=0; my @h; for (1..20) { my $r = syscall(26, 0); last if $r < 0; $n++; push @h,$r } print "$n of 20\n";'
echo -n "uid 0     (every container root, kubelet, systemd-in-kind, containerd-shim...): "
docker run --rm --entrypoint perl kindest/node:v1.37.0 -e "$PROBE"
echo -n "uid 1000  (user):                                                          "
docker run --rm --user 1000:1000 --entrypoint perl kindest/node:v1.37.0 -e "$PROBE"
echo -n "uid 65534 (nobody):                                                            "
docker run --rm --user 65534:65534 --entrypoint perl kindest/node:v1.37.0 -e "$PROBE"
echo; echo "== who holds uid-0 inotify instances? (read-only scan of /proc from a privileged helper)"
docker run --rm --privileged --pid=host --entrypoint bash kindest/node:v1.37.0 -c '
for p in /proc/[0-9]*; do
  [ "$(awk "/^Uid:/{print \$2}" $p/status 2>/dev/null)" = 0 ] || continue
  n=$(ls -l $p/fd 2>/dev/null | grep -c "anon_inode:inotify"); [ "$n" -gt 0 ] || continue
  cg=$(sed -n "s/.*docker-\([0-9a-f]\{12\}\).*/\1/p" $p/cgroup | head -1)
  echo "$n $(cat $p/comm) ${cg:-host}"
done | sort | awk "{k=\$2\" \"\$3; c[k]+=\$1; n[k]++} END {for (k in c) print c[k], n[k]\" procs\", k}" | sort -rn | head -14' |
while read -r cnt procs _ comm cid; do
  name=$([ "$cid" = host ] && echo host || docker ps -a --filter "id=$cid" --format '{{.Names}}' | head -1)
  echo "$cnt instances  $procs procs  $comm  [${name:-?}]"
done
