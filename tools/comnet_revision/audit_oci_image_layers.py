#!/usr/bin/env python3
"""Fetch REAL remote OCI/Docker manifest digests and compressed layer sizes.

Docker Hub public repository only (no credentials, no Docker daemon needed).
Uses the public registry Bearer token endpoint and an explicit optional local
HTTP CONNECT proxy. This tool does not download layer blobs, mutate containerd,
or assert actual node cache contents. Records immutable Linux/amd64 manifest
references, exact compressed layer digests/sizes and pairwise overlap.
"""
import argparse,csv,hashlib,json,pathlib,sys,time,urllib.error,urllib.parse,urllib.request

ACCEPT=", ".join((
 "application/vnd.oci.image.index.v1+json",
 "application/vnd.docker.distribution.manifest.list.v2+json",
 "application/vnd.oci.image.manifest.v1+json",
 "application/vnd.docker.distribution.manifest.v2+json",
))
def operation(opener,url,headers=None,timeout=30):
    req=urllib.request.Request(url,headers=headers or {})
    start=time.perf_counter()
    try:
        with opener.open(req,timeout=timeout) as response:
            raw=response.read()
            return json.loads(raw),dict(response.headers),round(time.perf_counter()-start,3),("sha256:"+hashlib.sha256(raw).hexdigest())
    except urllib.error.HTTPError as e:
        txt=e.read(500).decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} at {url.split('?')[0]}: {txt}") from e

def normalize(name):
    raw=name.removeprefix("docker.io/").removeprefix("index.docker.io/")
    if "/" in raw and "." in raw.split("/",1)[0]:
        raise ValueError("Only public Docker Hub repositories are supported: "+name)
    if "@sha256:" in raw:
        path,tag=raw.split("@sha256:",1)
        ref="sha256:"+tag
    elif ":" in raw.rsplit("/",1)[-1]:
        path,ref=raw.rsplit(":",1)
    else:
        path,ref=raw,"latest"
    if "/" not in path:path="library/"+path
    return path,ref


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--images",nargs="+",required=True)
    ap.add_argument("--proxy",default="",help="e.g. http://127.0.0.1:17897")
    ap.add_argument("--timeout",type=float,default=40.)
    ap.add_argument("--platform",default="linux/amd64")
    ap.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/oci_catalog")
    a=ap.parse_args()
    if a.proxy:
        opener=urllib.request.build_opener(
          urllib.request.ProxyHandler({"http":a.proxy,"https":a.proxy}))
    else:
        opener=urllib.request.build_opener()
    osname,arch=a.platform.split("/")
    catalog=[];raw_records={};errors=[]
    for text in a.images:
        try:
            path,tag=normalize(text)
            token_url="https://auth.docker.io/token?"+urllib.parse.urlencode({
                "service":"registry.docker.io","scope":f"repository:{path}:pull"})
            tok,_,td,_=operation(opener,token_url,timeout=a.timeout)
            hdr={"Authorization":"Bearer "+tok["token"],"Accept":ACCEPT}
            url=f"https://registry-1.docker.io/v2/{path}/manifests/{tag}"
            ix,ixh,ixdur,ix_body_digest=operation(opener,url,hdr,a.timeout)
            ix_digest=ixh.get("Docker-Content-Digest") or ix_body_digest
            if ix_digest!=ix_body_digest:
                raise RuntimeError("Registry manifest body digest disagrees with response header: "+text)
            if tag.startswith("sha256:") and ix_digest!=tag:
                raise RuntimeError("Requested digest does not match retrieved manifest: "+text)
            manifest=ix
            chosen_digest=ix_digest
            if "manifests" in ix:
                opts=[m for m in ix["manifests"] if
                    m.get("platform",{}).get("os")==osname and
                    m.get("platform",{}).get("architecture")==arch and
                    m.get("platform",{}).get("variant") in (None,"")]
                if not opts:raise RuntimeError("No supported "+a.platform+" manifest in index")
                chosen_digest=opts[0]["digest"]
                manifest,mh,mdur,manifest_body_digest=operation(opener,
                    f"https://registry-1.docker.io/v2/{path}/manifests/{chosen_digest}",
                    hdr,a.timeout)
                if manifest_body_digest!=chosen_digest:
                    raise RuntimeError("Platform manifest digest mismatch: "+text)
            else:
                mdur=0.
            if not chosen_digest or not chosen_digest.startswith("sha256:"):
                raise RuntimeError("No verified immutable platform manifest digest: "+text)
            if "layers" not in manifest:
                raise RuntimeError("Image does not have a manifest layers array")
            layers=[{"digest":z["digest"],"size_bytes":int(z["size"]),
                    "media_type":z.get("mediaType","")}
                    for z in manifest["layers"]]
            ref=f"docker.io/{path}@{chosen_digest}"
            entry={"source":text,"repository":path,"tag_or_digest":tag,
                   "image_ref":ref,"index_digest":ix_digest,
                   "platform_manifest_digest":chosen_digest,
                   "platform":a.platform,"layers":layers,
                   "compressed_layer_total_bytes":sum(z["size_bytes"] for z in layers),
                   "layer_count":len(layers),"manifest_fetch_seconds":ixdur+mdur,
                   "token_fetch_seconds":td,
                   "manifest_digest_source":"descriptor" if "manifests" in ix else "verified_raw_response_sha256"}
            catalog.append(entry)
            raw_records[text]={"manifest":manifest,"index":ix}
            print(f"OK {text:<32} layers={len(layers):2d} "
                  f"compressed={entry['compressed_layer_total_bytes']/1048576:.2f}MiB "
                  f"pinned={ref}",flush=True)
        except Exception as exc:
            errors.append({"image":text,"error":str(exc)})
            print("ERROR",text,str(exc),file=sys.stderr,flush=True)
    out=pathlib.Path(a.out_dir);out.mkdir(parents=True,exist_ok=True)
    pairs=[]
    for i,left in enumerate(catalog):
        la={x["digest"]:x["size_bytes"] for x in left["layers"]}
        for right in catalog[i+1:]:
            rb={x["digest"]:x["size_bytes"] for x in right["layers"]}
            shared=set(la)&set(rb)
            same_size=all(la[k]==rb[k] for k in shared)
            row={"image_a":left["source"],"image_b":right["source"],
                 "shared_layer_count":len(shared),
                 "shared_compressed_bytes":sum(la[x] for x in shared),
                 "shared_mib":round(sum(la[x] for x in shared)/1048576,5),
                 "size_metadata_agrees":same_size}
            pairs.append(row)
            print(f"PAIR {row['image_a']:<27} + {row['image_b']:<27} "
                  f"shared={row['shared_layer_count']} "
                  f"{row['shared_mib']:.3f}MiB",flush=True)
    result={
      "images":catalog,"errors":errors,"platform":a.platform,
      "assumptions":{
        "oci_content":"Immutable compressed layer digests and declared blob sizes from verified public registry manifests",
        "not_measured":"Physical network download bytes, per-node on-disk cache inventory and the runtime layer-eviction policy",
        "cache_states":"UNKNOWN; DO NOT assume initial_cache empty simply because source registry layers are known",
        "platform":"Only one architecture selected; pin platform-specific digest",
        "use":"Suitable as FGP/DCO image-layer workload INPUT after checking actual node cache and registry; not physical PGDSF evidence."
      }}
    (out/"catalog.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    if pairs:
        with (out/"shared_layers.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(pairs[0]));w.writeheader();w.writerows(pairs)
    (out/"raw_manifest_responses.json").write_text(
        json.dumps(raw_records,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Saved:",out/"catalog.json",out/"shared_layers.csv")
    print("SHARED LAYERS PAIRS:",sum(r["shared_layer_count"]>0 for r in pairs))
    if len(catalog)<2:return 2
    return 0 if not errors else 3

if __name__=="__main__":
    sys.exit(main())
