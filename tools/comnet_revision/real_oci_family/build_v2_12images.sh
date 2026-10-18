#!/usr/bin/env bash
# v2 controlled 12-image corpus: three families x four variants each.
# Run ONLY on a Docker build host. No containerd prune, cluster changes,
# Docker daemon restart or alterations to running containers.
set -euo pipefail
: "${DOCKERHUB_USER:?export DOCKERHUB_USER=<your Docker Hub user>}"
IMAGE_REPO="${IMAGE_REPO:-${DOCKERHUB_USER}/falcons-layer-exp}"
PREFIX="${PREFIX:-v2}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
command -v python3 >/dev/null
docker info >/dev/null
mkdir -p payloads
# Deterministic pseudo-random compressed payloads. No pre-existing file is
# overwritten; a size mismatch is a hard error to avoid silent tag drift.
python3 - <<'PY'
import hashlib,random
from pathlib import Path
base=Path("payloads")
base.mkdir(parents=True,exist_ok=True)
spec={}
for g,mb in enumerate((4,8,12)):
    spec[f"shared-g{g}.bin"]=mb
    for index,variant in enumerate(("a","b","c","d")):
        spec[f"g{g}-{variant}.bin"]=(2,4,6,8)[index]
for name,mib in spec.items():
    p=base/name
    if p.exists():
        if p.stat().st_size != mib*1024*1024:
            raise SystemExit(f"Size mismatch for existing {p}; do NOT silently replace an already published layer")
    else:
        rng=random.Random(int.from_bytes(hashlib.sha256(("FALCONS-v2|"+name).encode()).digest()[:8],"big"))
        with p.open("wb") as f:
            for _ in range(mib):
                f.write(rng.randbytes(1024*1024))
    sha=hashlib.sha256(p.read_bytes()).hexdigest()
    print(f"PAYLOAD {name:23s} {mib:2d} MiB sha256={sha}")
PY
# CAUTION: docker login should be performed beforehand and Hub repository
# must exist and allow push from the logged-in account.
for group in g0 g1 g2; do
  base_image="${IMAGE_REPO}:${PREFIX}-${group}-base"
  echo "BUILD family base ${base_image}"
  docker build --platform linux/amd64 -f Dockerfile.family_base \
    --build-arg "GROUP=${group}" -t "${base_image}" .
  docker push "${base_image}"
  for variant in a b c d; do
    ref="${IMAGE_REPO}:${PREFIX}-${group}-${variant}"
    echo "BUILD variant ${ref}"
    docker build --platform linux/amd64 -f Dockerfile.variant \
      --build-arg "BASE_IMAGE=${base_image}" \
      --build-arg "VARIANT=${group}-${variant}" -t "${ref}" .
    docker push "${ref}"
  done
done
echo 'PUBLIC OCI REFERENCES (v2):'
for group in g0 g1 g2;do
  for variant in a b c d;do
    echo "${IMAGE_REPO}:${PREFIX}-${group}-${variant}"
  done
done
echo 'Re-audit ALL image digests before deploying; do not assume refs stable if tags change.'
