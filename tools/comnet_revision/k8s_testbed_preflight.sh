#!/usr/bin/env bash
# Read-only host and Kubernetes readiness audit for FALCONS COMNET revision.
# Does not install software, change network settings, create VMs or modify K8s.
set -uo pipefail
section(){ printf '\n===== %s =====\n' "$1"; }
run(){ if command -v "$1" >/dev/null 2>&1; then "$@" 2>&1 || true; else echo "Missing: $1"; fi; }
section "Provenance"
date -Is
uname -a
whoami
printf 'hostname=';hostname
git rev-parse --short HEAD 2>/dev/null || true
section "CPU / architecture / virtualization"
run lscpu
printf '/dev/kvm: ';if [ -e /dev/kvm ];then ls -l /dev/kvm;else echo "not present";fi
run systemd-detect-virt
section "Memory"
run free -h
section "Disk space and filesystems"
run df -hT / /var /home
run lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT
run df -i /
section "Container / orchestration commands"
for c in kubectl kubeadm k3s k3d kind minikube helm docker podman ctr crictl containerd nerdctl virsh VBoxManage vagrant;do
  if command -v "$c" >/dev/null 2>&1;then printf '%-14s %s\n' "$c" "$(command -v "$c")";else printf '%-14s %s\n' "$c" "not installed";fi
done
section "Existing K8s context if already connected (read-only)"
if command -v kubectl >/dev/null 2>&1;then
  kubectl config current-context 2>&1 || true
  kubectl --request-timeout=5s get nodes -o wide 2>&1 || true
  kubectl --request-timeout=5s get --raw /version 2>&1 || true
fi
section "Local Python packages"
python3 - <<'PY'
import importlib.util, platform, sys
print('python=',sys.version.split()[0],'arch=',platform.machine())
for name in ('kubernetes','yaml','psutil','docker'):
    print(name, 'installed' if importlib.util.find_spec(name) else 'missing')
PY
section "Disk-space safety"
free_kb=$(df -Pk / | awk 'END{print $4}')
if [[ "$free_kb" =~ ^[0-9]+$ ]] && (( free_kb < 30 * 1024 * 1024 )); then
  printf 'BLOCKER: only %.2f GiB free on root; do not install or create VM images here.\\n' "$(awk -v k="$free_kb" 'BEGIN{print k/1024/1024}')"
fi
section "Notes"
echo "This preflight performs NO Kubernetes installation, privileged command, node reset or image eviction."
echo "Avoid running cache-flush / image-removal commands on a shared production cluster."
