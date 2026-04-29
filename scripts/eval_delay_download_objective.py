import argparse
import csv
import json

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--weights", nargs="+", type=float, default=[0.0, 0.1, 0.2, 0.3])
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.rows, "r", encoding="utf-8")))

    print("| setting | size | w_down | method | ACT | AMS | downloaded | DelayNorm | DownloadNorm | CompositeScore | reduction_vs_ILRSA_% |")
    print("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|")

    for row in rows:
        setting = row["setting"]
        size = row["size"]

        il = load_json(row["ilrsa"])["summary"]
        gc = load_json(row["gc"])["summary"]

        data = {
            "ILRSA": il,
            "FG-DSCR-GC": gc,
        }

        ref = il
        eps = 1e-12

        metrics = {}
        for name, s in data.items():
            delay_norm = 0.5 * float(s["ACT"]) / max(float(ref["ACT"]), eps) + \
                         0.5 * float(s["AMS"]) / max(float(ref["AMS"]), eps)
            down_norm = float(s["downloaded_mb"]) / max(float(ref["downloaded_mb"]), eps)

            metrics[name] = {
                "ACT": float(s["ACT"]),
                "AMS": float(s["AMS"]),
                "downloaded": float(s["downloaded_mb"]),
                "DelayNorm": delay_norm,
                "DownloadNorm": down_norm,
            }

        for w in args.weights:
            scores = {}
            for name, m in metrics.items():
                scores[name] = (1.0 - w) * m["DelayNorm"] + w * m["DownloadNorm"]

            for name, m in metrics.items():
                red = (scores["ILRSA"] - scores[name]) / max(scores["ILRSA"], eps) * 100.0
                print("| {} | {} | {:.2f} | {} | {:.3f} | {:.3f} | {:.0f} | {:.6f} | {:.6f} | {:.6f} | {:.2f} |".format(
                    setting, size, w, name,
                    m["ACT"], m["AMS"], m["downloaded"],
                    m["DelayNorm"], m["DownloadNorm"],
                    scores[name], red
                ))

if __name__ == "__main__":
    main()
