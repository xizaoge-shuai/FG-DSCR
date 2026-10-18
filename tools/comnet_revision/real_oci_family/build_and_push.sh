#!/usr/bin/env bash
# Build controlled sibling images with a REAL shared 8MiB layer and unique data.
# Requires Docker and a pre-created PUBLIC Docker Hub repository.
# It does not modify Kubernetes nodes or containerd.
set -euo pipefail
DOCKERHUB_USER="${DOCKERHUB_USER:?Set your Docker Hub username}"
IMAGE_REPO="${IMAGE_REPO:-${DOCKERHUB_USER}/falcons-layer-exp}"
TAG_PREFIX="${TAG_PREFIX:-v1}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
command -v docker >/dev/null || { echo 'Docker CLI missing; use a separate Docker build machine' >&2; exit 2; }
docker info >/dev/null
mkdir -p payloads
if [[ ! -f payloads/shared.bin ]];then
  dd if=/dev/urandom of=payloads/shared.bin bs=1M count=8 status=none
fi
for entry in a:4 b:8 c:12;do
  variant="${entry%%:*}"; mib="${entry##*:}"
  if [[ ! -f "payloads/${variant}.bin" ]];then
    dd if=/dev/urandom of="payloads/${variant}.bin" bs=1M count="$mib" status=none
  fi
done
base_ref="${IMAGE_REPO}:${TAG_PREFIX}-base"
docker build --platform linux/amd64 -f Dockerfile.base -t "$base_ref" .
docker push "$base_ref"
for variant in a b c;do
  image="${IMAGE_REPO}:${TAG_PREFIX}-${variant}"
  docker build --platform linux/amd64 -f Dockerfile.variant \
    --build-arg "BASE_IMAGE=$base_ref" \
    --build-arg "VARIANT=$variant" -t "$image" .
  docker push "$image"
done
printf '\nPUBLISHED IMAGE REFERENCES:\n'
for variant in a b c;do echo "${IMAGE_REPO}:${TAG_PREFIX}-${variant}";done
echo 'Pin platform manifest digests and audit shared layers before running experiments.'
