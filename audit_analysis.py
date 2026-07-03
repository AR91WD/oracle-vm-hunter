#!/usr/bin/env python3
"""
Deep-audit analysis over raw per-probe events (events-gh.ndjson, events-mac.ndjson).
Built to answer the 8 evidence-based questions raised about the AIMD pacing change:
per-worker breakdown, per-size response-class cross-tab, full-cycle interval
percentiles, and a combined-timeline proof of whether any true "all 4 workers
silent" gap ever occurs. Read-only, does not touch pace/backoff/sequencing —
pure analysis of already-collected instrumentation.

Usage: python3 audit_analysis.py [--hours N] [--since ISO8601]
"""
import argparse
import json
import sys
import time
from collections import defaultdict


def load_events(path, since_ts):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(o, dict):
                    continue
                ts = o.get("ts")
                if not isinstance(ts, (int, float)) or ts < since_ts:
                    continue
                out.append(o)
    except FileNotFoundError:
        pass
    return out


def worker_key(ev):
    src = ev.get("source", "?")
    ad = ev.get("ad", "?")
    return f"{src}-{ad}" if src == "GH" else src


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--gh-events", default="events-gh.ndjson")
    ap.add_argument("--mac-events", default="events-mac.ndjson")
    args = ap.parse_args()

    since_ts = time.time() - args.hours * 3600
    events = load_events(args.gh_events, since_ts) + load_events(args.mac_events, since_ts)
    if not events:
        print("No events in window — nothing to analyze.")
        return 0

    events.sort(key=lambda e: e["ts"])
    print(f"Total events in window: {len(events)}  (window: {args.hours:g}h)\n")

    # --- 1. Per-worker x per-class cross-tab ---
    by_worker_class = defaultdict(lambda: defaultdict(int))
    by_worker_size_class = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    req_ms_by_class = defaultdict(list)
    for ev in events:
        w = worker_key(ev)
        c = ev.get("class", "?")
        s = ev.get("size", "?")
        by_worker_class[w][c] += 1
        by_worker_size_class[w][s][c] += 1
        ms = ev.get("req_ms")
        if isinstance(ms, (int, float)) and ms > 0:
            req_ms_by_class[c].append(ms)

    print("=== 1. Per-worker response-class breakdown ===")
    all_classes = sorted({c for wc in by_worker_class.values() for c in wc})
    for w in sorted(by_worker_class):
        total = sum(by_worker_class[w].values())
        parts = ", ".join(f"{c}={by_worker_class[w].get(c, 0)}" for c in all_classes)
        print(f"  {w}: total={total}  ({parts})")
    print()

    # --- 2. Per-size x per-class (all workers combined) ---
    print("=== 2. Per-size response-class breakdown (all workers) ===")
    size_class = defaultdict(lambda: defaultdict(int))
    for ev in events:
        size_class[ev.get("size", "?")][ev.get("class", "?")] += 1
    for sz in sorted(size_class, key=lambda x: (isinstance(x, str), x)):
        total = sum(size_class[sz].values())
        rej = size_class[sz].get("429", 0)
        rej_pct = 100 * rej / total if total else 0
        parts = ", ".join(f"{c}={n}" for c, n in sorted(size_class[sz].items()))
        print(f"  size={sz}oc: total={total} 429-rate={rej_pct:.1f}%  ({parts})")
    print()

    # --- 3. Request latency (Oracle round-trip) by class ---
    print("=== 3. Oracle response latency (ms) by class ===")
    for c in sorted(req_ms_by_class):
        vals = sorted(req_ms_by_class[c])
        if not vals:
            continue
        print(f"  {c}: n={len(vals)} min={vals[0]} p50={pct(vals,0.5):.0f} "
              f"p90={pct(vals,0.9):.0f} max={vals[-1]}")
    print()

    # --- 4. Full-cycle interval (send-to-send) per worker, percentiles ---
    print("=== 4. Full-cycle interval per worker (send N -> send N+1, seconds) ===")
    by_worker_ts = defaultdict(list)
    for ev in events:
        by_worker_ts[worker_key(ev)].append(ev["ts"])
    for w in sorted(by_worker_ts):
        ts_list = sorted(by_worker_ts[w])
        deltas = [ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)]
        if not deltas:
            continue
        deltas.sort()
        print(f"  {w}: n={len(deltas)} min={deltas[0]}s p50={pct(deltas,0.5):.0f}s "
              f"p90={pct(deltas,0.9):.0f}s max={deltas[-1]}s")
    print()

    # --- 5. Combined timeline: is there ever a moment where ALL workers are silent? ---
    print("=== 5. Combined timeline — proof of no all-workers-silent gap ===")
    all_ts = sorted(e["ts"] for e in events)
    combined_gaps = [all_ts[i + 1] - all_ts[i] for i in range(len(all_ts) - 1)]
    if combined_gaps:
        combined_gaps_sorted = sorted(combined_gaps)
        worst = combined_gaps_sorted[-1]
        worst_idx = combined_gaps.index(worst)
        worst_start = all_ts[worst_idx]
        print(f"  Combined (any worker) max silent gap: {worst}s "
              f"(at ts={worst_start}, {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(worst_start))})")
        print(f"  Combined p50 gap: {pct(combined_gaps_sorted,0.5):.1f}s  "
              f"p90: {pct(combined_gaps_sorted,0.9):.1f}s")
        over_30 = sum(1 for g in combined_gaps if g > 30)
        over_60 = sum(1 for g in combined_gaps if g > 60)
        print(f"  Gaps > 30s: {over_30}/{len(combined_gaps)}   Gaps > 60s: {over_60}/{len(combined_gaps)}")
    print()

    print("=== 6. Active worker count ===")
    print(f"  Distinct workers seen: {sorted(by_worker_ts.keys())}")


if __name__ == "__main__":
    sys.exit(main())
