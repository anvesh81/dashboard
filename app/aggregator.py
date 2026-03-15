"""
EBS Aggregator — one-shot ECS Fargate task.
Triggered by EventBridge daily, or manually via POST /api/refresh.

Scans ALL available date folders in S3 (no fixed window).
Writes summary.json back to the monitoring bucket.
"""

import csv
import io
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import boto3

S3          = boto3.client("s3")
BUCKET      = os.environ["MONITORING_BUCKET"]
SUMMARY_KEY = os.environ.get("SUMMARY_KEY", "summary.json")
OUTPOSTS    = ["wp-outpost", "tv-outpost"]


def main():
    start = datetime.now(timezone.utc)
    log(f"start  bucket={BUCKET}")

    # ── 1. Discover all date folders ──────────────────────────────────────────
    all_prefixes = []
    for outpost in OUTPOSTS:
        for page in S3.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix=f"{outpost}/", Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []):
                parts    = cp["Prefix"].rstrip("/").split("/")
                date_str = parts[1] if len(parts) == 2 else ""
                if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
                    all_prefixes.append((outpost, date_str, cp["Prefix"]))

    if not all_prefixes:
        log("no date folders found — bucket empty or SSM hasn't run yet")
        sys.exit(0)

    all_prefixes.sort(key=lambda x: x[1])
    earliest, latest = all_prefixes[0][1], all_prefixes[-1][1]
    log(f"{len(all_prefixes)} date folders  {earliest} → {latest}")

    # ── 2. Read every CSV ─────────────────────────────────────────────────────
    history   = defaultdict(dict)   # "SERVER|DRIVE" → {date: snap}
    meta      = {}
    csv_count = 0
    row_count = 0

    for outpost, date_str, prefix in all_prefixes:
        try:
            for page in S3.get_paginator("list_objects_v2").paginate(
                Bucket=BUCKET, Prefix=prefix
            ):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".csv"):
                        continue
                    try:
                        body    = S3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
                        content = body.decode("utf-8-sig")
                        for row in csv.DictReader(io.StringIO(content)):
                            server     = _s(row, "server_name")
                            drive      = _s(row, "drive")
                            ts         = _s(row, "timestamp_utc")
                            date_val   = ts[:10] if ts else date_str
                            total_gb   = _i(row, "total_gb")
                            used_gb    = _i(row, "used_gb")
                            free_gb    = _i(row, "free_gb")
                            used_pct   = _i(row, "used_percent")
                            volume_id  = _s(row, "volume_id")
                            op_name    = _s(row, "outpost_name") or outpost
                            region     = _s(row, "region")
                            account_id = _s(row, "account_id")
                            inst_id    = _s(row, "instance_id")

                            if not server or not drive:
                                continue

                            hkey = f"{server}|{drive}"
                            history[hkey][date_val] = {
                                "date":     date_val,
                                "used_gb":  used_gb,
                                "free_gb":  free_gb,
                                "total_gb": total_gb,
                                "pct":      used_pct,
                            }
                            if hkey not in meta or date_val > meta[hkey].get("last_seen", ""):
                                meta[hkey] = {
                                    "server":      server,
                                    "outpost":     op_name,
                                    "drive":       drive,
                                    "volume_id":   volume_id,
                                    "region":      region,
                                    "account_id":  account_id,
                                    "instance_id": inst_id,
                                    "last_seen":   date_val,
                                }
                            row_count += 1
                        csv_count += 1
                    except Exception as e:
                        log(f"  WARN: failed to read {key}: {e}")
        except Exception as e:
            log(f"  WARN: error listing {prefix}: {e}")

    log(f"read {csv_count} CSVs / {row_count} rows / {len(history)} drives")

    if not history:
        log("no valid rows — nothing to write")
        sys.exit(0)

    # ── 3. Sort each drive's history ──────────────────────────────────────────
    for hkey in history:
        history[hkey] = sorted(history[hkey].values(), key=lambda x: x["date"])

    # ── 4. Per-drive metrics ──────────────────────────────────────────────────
    drives_out = []
    for hkey, snaps in history.items():
        m      = meta.get(hkey, {})
        latest = snaps[-1]
        n      = len(snaps)
        pct    = latest["pct"]
        free   = latest["free_gb"]
        used   = latest["used_gb"]
        total  = latest["total_gb"]

        week_gr   = growth_rate(snaps, min(7, n))
        month_gr  = growth_rate(snaps, min(30, n))
        qtr_gr    = growth_rate(snaps, min(90, n))
        total_all = snaps[-1]["used_gb"] - snaps[0]["used_gb"] if n >= 2 else 0
        total_90  = total_growth(snaps, min(90, n))

        # Use 7-day rate if 7+ days available, else overall average
        eff_gr    = week_gr if n >= 7 else (total_all / max(n - 1, 1) if n >= 2 else 0)
        days_left = math.floor(free / eff_gr) if eff_gr > 0.05 else 9999
        flat      = n >= 7 and abs(total_all) < 5
        status    = "crit" if pct >= 90 else "warn" if pct >= 75 else "ok"

        drives_out.append({
            "server":             m.get("server", hkey.split("|")[0]),
            "outpost":            m.get("outpost", ""),
            "drive":              m.get("drive", ""),
            "volume_id":          m.get("volume_id", ""),
            "instance_id":        m.get("instance_id", ""),
            "region":             m.get("region", ""),
            "account_id":         m.get("account_id", ""),
            "last_seen":          m.get("last_seen", ""),
            "total_gb":           total,
            "used_gb":            used,
            "free_gb":            free,
            "pct":                pct,
            "status":             status,
            "days_of_data":       n,
            "daily_growth":       round(week_gr, 2),
            "month_growth":       round(month_gr, 2),
            "quarter_growth":     round(qtr_gr, 2),
            "total_growth_all":   total_all,
            "total_growth_90":    total_90,
            "days_left":          days_left,
            "flat":               flat,
            "downsize_candidate": flat or (pct < 40 and eff_gr < 0.5),
            "history":            snaps,
        })

    # ── 5. Fleet + outpost summaries ──────────────────────────────────────────
    tp = sum(d["total_gb"] for d in drives_out)
    tu = sum(d["used_gb"]  for d in drives_out)

    outpost_summary = {}
    for op in OUTPOSTS:
        od  = [d for d in drives_out if d["outpost"] == op]
        otp = sum(d["total_gb"] for d in od) or 1
        otu = sum(d["used_gb"]  for d in od)
        outpost_summary[op] = {
            "drives":       len(od),
            "servers":      len({d["server"] for d in od}),
            "total_gb":     otp,
            "used_gb":      otu,
            "free_gb":      otp - otu,
            "pct":          round(otu / otp * 100),
            "daily_growth": round(sum(d["daily_growth"] for d in od), 2),
            "crits":        sum(1 for d in od if d["status"] == "crit"),
            "warns":        sum(1 for d in od if d["status"] == "warn"),
            "downsize":     sum(1 for d in od if d["downsize_candidate"]),
        }

    elapsed = round((datetime.now(timezone.utc) - start).total_seconds(), 1)
    summary = {
        "generated_utc":   datetime.now(timezone.utc).isoformat(),
        "data_from":       earliest,
        "data_to":         latest,
        "days_of_data":    len(all_prefixes),
        "csv_files_read":  csv_count,
        "elapsed_seconds": elapsed,
        "fleet": {
            "total_gb":            tp,
            "used_gb":             tu,
            "free_gb":             tp - tu,
            "pct":                 round(tu / tp * 100) if tp else 0,
            "daily_growth_gb":     round(sum(d["daily_growth"] for d in drives_out), 2),
            "drives_total":        len(drives_out),
            "servers_total":       len({d["server"] for d in drives_out}),
            "critical":            sum(1 for d in drives_out if d["status"] == "crit"),
            "warning":             sum(1 for d in drives_out if d["status"] == "warn"),
            "healthy":             sum(1 for d in drives_out if d["status"] == "ok"),
            "downsize_candidates": sum(1 for d in drives_out if d["downsize_candidate"]),
            "min_days_until_full": min(
                (d["days_left"] for d in drives_out if d["days_left"] < 9999),
                default=9999,
            ),
        },
        "outposts": outpost_summary,
        "drives":   drives_out,
    }

    # ── 6. Write summary.json ─────────────────────────────────────────────────
    body = json.dumps(summary, default=str).encode("utf-8")
    S3.put_object(
        Bucket=BUCKET, Key=SUMMARY_KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="max-age=60",
    )
    log(
        f"done  drives={len(drives_out)} days={len(all_prefixes)}"
        f" size={len(body):,}b elapsed={elapsed}s"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[aggregator] {msg}", flush=True)

def _s(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()

def _i(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (ValueError, TypeError):
        return 0

def growth_rate(snaps: list, window: int) -> float:
    if len(snaps) < 2:
        return 0.0
    w = snaps[-min(window + 1, len(snaps)):]
    if len(w) < 2:
        return 0.0
    return round((w[-1]["used_gb"] - w[0]["used_gb"]) / max(1, len(w) - 1), 3)

def total_growth(snaps: list, window: int) -> int:
    if len(snaps) < 2:
        return 0
    w = snaps[-min(window + 1, len(snaps)):]
    return w[-1]["used_gb"] - w[0]["used_gb"] if len(w) >= 2 else 0


if __name__ == "__main__":
    main()
