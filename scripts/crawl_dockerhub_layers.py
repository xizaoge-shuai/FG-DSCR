#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import argparse
import csv
import json
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

import requests

REGISTRY = "https://registry-1.docker.io"
AUTH = "https://auth.docker.io/token"

DEFAULT_IMAGES = [
    "python", "golang", "openjdk", "ubuntu", "memcached",
    "httpd", "mysql", "mariadb", "redis", "postgres",
    "rabbitmq", "registry", "wordpress", "ghost",
    "node", "flink", "cassandra",
]

MANIFEST_ACCEPT = [
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
]

SESSION = requests.Session()


def norm_repo(name: str) -> str:
    """
    python -> library/python
    user/repo -> user/repo
    """
    return name if "/" in name else f"library/{name}"


def split_image_ref(image_ref: str) -> Tuple[str, str]:
    """
    python:3.9 -> ('python', '3.9')
    redis -> ('redis', 'latest')
    """
    if ":" in image_ref:
        name, tag = image_ref.rsplit(":", 1)
    else:
        name, tag = image_ref, "latest"
    return name.strip(), tag.strip()


def get_token(repo: str) -> str:
    params = {
        "service": "registry.docker.io",
        "scope": f"repository:{repo}:pull",
    }
    r = SESSION.get(AUTH, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def registry_get(repo: str, path: str, accept: List[str] | None = None) -> requests.Response:
    token = get_token(repo)
    headers = {"Authorization": f"Bearer {token}"}
    if accept:
        headers["Accept"] = ", ".join(accept)
    url = f"{REGISTRY}/v2/{repo}/{path}"
    r = SESSION.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r


def fetch_manifest_descriptor(repo: str, ref: str) -> Tuple[Dict, str]:
    r = registry_get(repo, f"manifests/{ref}", MANIFEST_ACCEPT)
    media_type = r.headers.get("Content-Type", "")
    return r.json(), media_type


def select_platform_manifest(desc: Dict) -> Tuple[str, str]:
    """
    如果是 manifest list / image index，优先挑 linux/amd64
    返回 (digest, mediaType)
    """
    manifests = desc.get("manifests", [])
    if not manifests:
        raise ValueError("manifest list 为空")

    for m in manifests:
        p = m.get("platform", {})
        if p.get("os") == "linux" and p.get("architecture") == "amd64":
            return m["digest"], m["mediaType"]

    m = manifests[0]
    return m["digest"], m["mediaType"]


def fetch_single_manifest(repo: str, digest_or_tag: str) -> Dict:
    r = registry_get(
        repo,
        f"manifests/{digest_or_tag}",
        [
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ],
    )
    return r.json()


def fetch_config_blob(repo: str, digest: str) -> Dict:
    r = registry_get(repo, f"blobs/{digest}")
    return r.json()


def fetch_image_info(image_ref: str) -> Dict:
    name, tag = split_image_ref(image_ref)
    repo = norm_repo(name)

    top_desc, media_type = fetch_manifest_descriptor(repo, tag)

    if "manifest.list.v2+json" in media_type or "image.index.v1+json" in media_type:
        digest, _ = select_platform_manifest(top_desc)
        mani = fetch_single_manifest(repo, digest)
        manifest_digest = digest
    else:
        mani = top_desc
        manifest_digest = mani.get("config", {}).get("digest", tag)

    layers = mani.get("layers", [])
    config_desc = mani.get("config", {})
    config = {}
    if config_desc.get("digest"):
        try:
            config = fetch_config_blob(repo, config_desc["digest"])
        except Exception:
            config = {}

    layer_list = []
    total_size = 0
    for i, layer in enumerate(layers):
        size = int(layer.get("size", 0))
        total_size += size
        layer_list.append({
            "index": i,
            "digest": layer["digest"],
            "size_bytes": size,
            "size_mb": round(size / 1024 / 1024, 4),
            "mediaType": layer.get("mediaType", ""),
        })

    out = {
        "image_ref": image_ref,
        "repo": repo,
        "tag": tag,
        "manifest_digest": manifest_digest,
        "schemaVersion": mani.get("schemaVersion"),
        "config_digest": config_desc.get("digest"),
        "architecture": config.get("architecture", "unknown"),
        "os": config.get("os", "unknown"),
        "total_layers": len(layer_list),
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / 1024 / 1024, 4),
        "layers": layer_list,
    }
    return out


def build_overlap(catalog: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    返回:
    1) image-level overlap rows
    2) layer-to-image rows
    """
    image_layers = {}
    layer_sizes = {}
    for item in catalog:
        s = set()
        for x in item["layers"]:
            s.add(x["digest"])
            layer_sizes[x["digest"]] = x["size_mb"]
        image_layers[item["image_ref"]] = s

    overlap_rows = []
    keys = list(image_layers.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            inter = image_layers[a] & image_layers[b]
            overlap_mb = sum(layer_sizes[d] for d in inter)
            overlap_rows.append({
                "image_a": a,
                "image_b": b,
                "shared_layers": len(inter),
                "shared_size_mb": round(overlap_mb, 4),
            })

    layer_to_image = []
    digest_to_imgs = defaultdict(list)
    for img, layers in image_layers.items():
        for d in layers:
            digest_to_imgs[d].append(img)

    for d, imgs in digest_to_imgs.items():
        layer_to_image.append({
            "layer_digest": d,
            "size_mb": layer_sizes.get(d, 0.0),
            "num_images": len(imgs),
            "images": "|".join(sorted(imgs)),
        })

    return overlap_rows, layer_to_image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", nargs="*", default=DEFAULT_IMAGES,
                        help="镜像列表，如 python:3.9 redis postgres node")
    parser.add_argument("--out-json", default="image_catalog.json")
    parser.add_argument("--out-overlap-csv", default="image_overlap.csv")
    parser.add_argument("--out-layer-map-csv", default="layer_to_image.csv")
    args = parser.parse_args()

    catalog = []
    for img in args.images:
        try:
            print(f"[INFO] fetching {img} ...", file=sys.stderr)
            info = fetch_image_info(img)
            catalog.append(info)
        except Exception as e:
            print(f"[WARN] skip {img}: {e}", file=sys.stderr)

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    overlap_rows, layer_rows = build_overlap(catalog)

    with open(args.out_overlap_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["image_a", "image_b", "shared_layers", "shared_size_mb"])
        w.writeheader()
        w.writerows(overlap_rows)

    with open(args.out_layer_map_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["layer_digest", "size_mb", "num_images", "images"])
        w.writeheader()
        w.writerows(layer_rows)

    print(f"[OK] wrote {args.out_json}, {args.out_overlap_csv}, {args.out_layer_map_csv}")


if __name__ == "__main__":
    main()