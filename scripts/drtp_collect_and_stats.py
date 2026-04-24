#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import argparse
import bz2
import csv
import gzip
import io
import json
import lzma
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional
from urllib.parse import urljoin

import requests

SESSION = requests.Session()


def fetch_html(url: str) -> str:
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def extract_links(html: str, base_url: str) -> List[str]:
    hrefs = re.findall(r'href=[\'"]([^\'"]+)[\'"]', html, flags=re.I)
    out = []
    for h in hrefs:
        if h.startswith("#"):
            continue
        out.append(urljoin(base_url, h))
    return out


def discover_trace_links(index_url: str) -> List[str]:
    """
    先抓首页，再抓其中可能的 traces 子页面
    """
    all_links = set()

    html = fetch_html(index_url)
    lvl1 = extract_links(html, index_url)
    for x in lvl1:
        all_links.add(x)

    # 进一步抓一层含 traces / trace 的目录页
    for x in list(lvl1):
        if ("trace" in x.lower()) and x.endswith("/"):
            try:
                html2 = fetch_html(x)
                for y in extract_links(html2, x):
                    all_links.add(y)
            except Exception:
                pass

    keep = []
    for x in sorted(all_links):
        low = x.lower()
        if any(low.endswith(ext) for ext in [".json", ".jsonl", ".gz", ".zip", ".bz2", ".xz", ".txt"]):
            keep.append(x)
        elif "trace" in low and not low.endswith("/"):
            keep.append(x)

    return sorted(set(keep))


def download_file(url: str, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    name = url.rstrip("/").split("/")[-1]
    if not name:
        return
    dst = outdir / name
    if dst.exists():
        print(f"[SKIP] {dst}")
        return

    print(f"[DOWN] {url} -> {dst}")
    with SESSION.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def open_any(path: Path):
    low = path.name.lower()
    if low.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    if low.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8", errors="ignore")
    if low.endswith(".xz"):
        return lzma.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, "r", encoding="utf-8", errors="ignore")


def iter_zip_json_lines(path: Path) -> Iterator[Dict]:
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            with zf.open(name, "r") as f:
                text = io.TextIOWrapper(f, encoding="utf-8", errors="ignore")
                yield from iter_json_records(text)


def iter_json_records(fp) -> Iterator[Dict]:
    """
    支持:
    - 一行一个 json
    - 整体是 json array
    """
    first = fp.read(1)
    if not first:
        return
    rest = fp.read()
    data = first + rest

    stripped = data.lstrip()
    if stripped.startswith("["):
        try:
            arr = json.loads(data)
            for x in arr:
                if isinstance(x, dict):
                    yield x
            return
        except Exception:
            pass

    for line in data.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            x = json.loads(s)
            if isinstance(x, dict):
                yield x
        except Exception:
            continue


def iter_trace_records(path: Path) -> Iterator[Dict]:
    low = path.name.lower()
    if low.endswith(".zip"):
        yield from iter_zip_json_lines(path)
    else:
        with open_any(path) as fp:
            yield from iter_json_records(fp)


def parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def parse_repo_from_uri(uri: str) -> str:
    """
    类似:
      v2/<repo>/manifests/<tag>
      v2/<repo>/blobs/<digest>
    匿名 trace 里 repo 也是匿名串，但仍可作为 workload id
    """
    if not uri:
        return "unknown"
    parts = [x for x in uri.strip("/").split("/") if x]
    if len(parts) < 4 or parts[0] != "v2":
        return "unknown"
    # 找 manifests / blobs / uploads 的位置
    for key in ("manifests", "blobs", "uploads"):
        if key in parts:
            idx = parts.index(key)
            if idx >= 2:
                return "/".join(parts[1:idx])
    return "unknown"


def parse_kind(uri: str) -> str:
    if not uri:
        return "other"
    if "/manifests/" in uri:
        return "manifest"
    if "/blobs/uploads/" in uri or uri.endswith("/blobs/uploads"):
        return "upload"
    if "/blobs/" in uri:
        return "layer_blob"
    return "other"


def process_trace_files(files: List[Path], outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)

    total_requests = 0
    total_bytes = 0
    method_counter = Counter()
    kind_counter = Counter()
    repo_counter = Counter()
    client_counter = Counter()
    minute_counter = Counter()
    hour_counter = Counter()

    for fp in files:
        print(f"[PROC] {fp}", file=sys.stderr)
        for rec in iter_trace_records(fp):
            ts = parse_ts(rec.get("timestamp", ""))
            method = rec.get("http.request.method", "UNKNOWN")
            uri = rec.get("http.request.uri", "")
            remote = rec.get("http.request.remoteaddr", "unknown")
            written = rec.get("http.response.written", 0)

            try:
                written = int(written)
            except Exception:
                written = 0

            repo = parse_repo_from_uri(uri)
            kind = parse_kind(uri)

            total_requests += 1
            total_bytes += written
            method_counter[method] += 1
            kind_counter[kind] += 1
            repo_counter[repo] += 1
            client_counter[remote] += 1

            if ts is not None:
                minute_key = ts.strftime("%Y-%m-%d %H:%M")
                hour_key = ts.strftime("%H")
                minute_counter[minute_key] += 1
                hour_counter[hour_key] += 1

    summary = {
        "total_requests": total_requests,
        "total_bytes": total_bytes,
        "method_counter": dict(method_counter),
        "kind_counter": dict(kind_counter),
        "top_repos": repo_counter.most_common(100),
        "top_clients": client_counter.most_common(100),
        "hour_counter": dict(sorted(hour_counter.items())),
    }

    with open(outdir / "drtp_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    with open(outdir / "minute_arrivals.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["minute", "requests"])
        for k, v in sorted(minute_counter.items()):
            w.writerow([k, v])

    with open(outdir / "repo_popularity.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["repo_id", "requests"])
        for repo, cnt in repo_counter.most_common():
            w.writerow([repo, cnt])

    with open(outdir / "client_popularity.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["client_id", "requests"])
        for cid, cnt in client_counter.most_common():
            w.writerow([cid, cnt])

    # 给脚本3用的 profile
    total_repo = sum(repo_counter.values()) or 1
    profile = {
        "total_requests": total_requests,
        "repo_weights": [
            {"repo_id": repo, "weight": cnt / total_repo}
            for repo, cnt in repo_counter.most_common(200)
        ],
        "hour_weights": [
            {"hour": h, "weight": c / max(sum(hour_counter.values()), 1)}
            for h, c in sorted(hour_counter.items())
        ],
        "method_probs": {
            k: v / max(sum(method_counter.values()), 1)
            for k, v in method_counter.items()
        },
        "kind_probs": {
            k: v / max(sum(kind_counter.values()), 1)
            for k, v in kind_counter.items()
        },
    }
    with open(outdir / "drtp_profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)

    print(f"[OK] wrote stats to {outdir}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-url", default="https://dssl.cs.vt.edu/drtp/",
                        help="DRTP 首页或 traces 目录")
    parser.add_argument("--download-dir", default="drtp_raw")
    parser.add_argument("--stats-dir", default="drtp_stats")
    parser.add_argument("--download", action="store_true", help="先自动发现并下载 traces")
    parser.add_argument("--pattern", default="", help="只下载/处理文件名里包含该子串的文件，比如 dal lon fra")
    args = parser.parse_args()

    raw_dir = Path(args.download_dir)
    stats_dir = Path(args.stats_dir)

    if args.download:
        links = discover_trace_links(args.index_url)
        if args.pattern:
            links = [x for x in links if args.pattern.lower() in x.lower()]
        print("[INFO] discovered links:")
        for x in links:
            print("  ", x)
        for x in links:
            try:
                download_file(x, raw_dir)
            except Exception as e:
                print(f"[WARN] download failed {x}: {e}", file=sys.stderr)

    files = []
    for p in raw_dir.rglob("*"):
        if p.is_file():
            if args.pattern and args.pattern.lower() not in p.name.lower():
                continue
            files.append(p)

    if not files:
        raise SystemExit("没有找到可处理的 trace 文件")

    process_trace_files(sorted(files), stats_dir)


if __name__ == "__main__":
    main()