#!/usr/bin/env python3
"""
Rigorous statistical analysis over raw per-probe events for the audit:
percentiles (p50/p90/p95/p99/max) per class, correlations (pace vs 429 /
throughput / capacity), time-of-day heatmap, 95%/99% CIs, and top outliers.
Pure stdlib (no numpy) — runs anywhere. Read-only analysis.

Usage: python3 deep_stats.py events-gh.ndjson events-mac.ndjson [--hours N]
"""
import json
import math
import sys
import time
from collections import defaultdict


def load(paths, since):
    out = []
    for p in paths:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(o, dict) and isinstance(o.get("ts"), (int, float)) and o["ts"] >= since:
                        out.append(o)
        except OSError:
            pass
    out.sort(key=lambda e: e["ts"])
    return out


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f) if f != c else s[f]


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def prop_ci(k, n, z=1.96):
    """Wilson score interval for a proportion (429-rate etc.)."""
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def main():
    args = sys.argv[1:]
    hours = 48.0
    if "--hours" in args:
        i = args.index("--hours")
        hours = float(args[i + 1])
        args = args[:i] + args[i + 2:]
    events = load(args, time.time() - hours * 3600)
    if not events:
        print("no events")
        return
    print(f"N={len(events)} events over {hours:g}h\n")

    # --- percentiles by class ---
    lat = defaultdict(list)
    for e in events:
        ms = e.get("req_ms")
        if isinstance(ms, (int, float)) and ms > 0:
            lat[e.get("class", "?")].append(ms)
    print("=== latency ms by class: p50 / p90 / p95 / p99 / max (n) ===")
    for c in sorted(lat):
        v = lat[c]
        print(f"  {c:9s}: {pct(v,.5):.0f} / {pct(v,.9):.0f} / {pct(v,.95):.0f} / {pct(v,.99):.0f} / {max(v):.0f}  (n={len(v)})")
    print()

    # --- per-hour-of-day heatmap ---
    print("=== time-of-day (UTC hour): attempts | no_cap | limit | 429 | avg_ms ===")
    hour = defaultdict(lambda: defaultdict(int))
    hour_ms = defaultdict(list)
    for e in events:
        h = time.gmtime(e["ts"]).tm_hour
        hour[h]["n"] += 1
        hour[h][e.get("class", "?")] += 1
        ms = e.get("req_ms")
        if isinstance(ms, (int, float)) and ms > 0:
            hour_ms[h].append(ms)
    for h in range(24):
        if hour[h]["n"] == 0:
            continue
        d = hour[h]
        am = sum(hour_ms[h]) / len(hour_ms[h]) if hour_ms[h] else 0
        bar = "#" * int(40 * d["no_cap"] / max(1, d["n"]))
        print(f"  {h:02d}h: {d['n']:5d} | nc={d.get('no_cap',0):5d} lim={d.get('limit',0):5d} 429={d.get('429',0):4d} | {am:.0f}ms {bar}")
    print()

    # --- correlations (bucket by generation-ish windows) ---
    print("=== correlations (Pearson r, hourly buckets) ===")
    buck = defaultdict(lambda: {"n": 0, "429": 0, "nc": 0, "lim": 0, "pace": [], "ms": []})
    for e in events:
        b = int(e["ts"] // 3600)
        bk = buck[b]
        bk["n"] += 1
        c = e.get("class", "?")
        if c == "429":
            bk["429"] += 1
        elif c == "no_cap":
            bk["nc"] += 1
        elif c == "limit":
            bk["lim"] += 1
        if isinstance(e.get("pace"), (int, float)):
            bk["pace"].append(e["pace"])
        if isinstance(e.get("req_ms"), (int, float)) and e["req_ms"] > 0:
            bk["ms"].append(e["req_ms"])
    rows = [b for b in buck.values() if b["n"] >= 20 and b["pace"]]
    if len(rows) >= 3:
        pace = [sum(r["pace"]) / len(r["pace"]) for r in rows]
        rate429 = [r["429"] / r["n"] for r in rows]
        thru = [r["n"] for r in rows]
        caprate = [r["nc"] / r["n"] for r in rows]
        avgms = [sum(r["ms"]) / len(r["ms"]) if r["ms"] else 0 for r in rows]

        def show(name, a, b):
            r = pearson(a, b)
            print(f"  {name:28s}: r={r:+.3f}" if r is not None else f"  {name:28s}: n/a")
        show("pace vs 429-rate", pace, rate429)
        show("pace vs throughput", pace, thru)
        show("pace vs no_cap-rate", pace, caprate)
        show("throughput vs 429-rate", thru, rate429)
        show("latency vs no_cap-rate", avgms, caprate)
        print(f"  (buckets used: {len(rows)})")
    print()

    # --- 429-rate with 95% / 99% CI (Wilson) ---
    total = len(events)
    n429 = sum(1 for e in events if e.get("class") == "429")
    nlim = sum(1 for e in events if e.get("class") == "limit")
    nnc = sum(1 for e in events if e.get("class") == "no_cap")
    print("=== proportions with confidence intervals (Wilson) ===")
    for name, k in [("429-rate", n429), ("limit-rate", nlim), ("no_cap-rate", nnc)]:
        lo95, hi95 = prop_ci(k, total, 1.96)
        lo99, hi99 = prop_ci(k, total, 2.576)
        print(f"  {name:12s}: {100*k/total:.2f}%  95%CI[{100*lo95:.2f},{100*hi95:.2f}]  99%CI[{100*lo99:.2f},{100*hi99:.2f}]")
    print()

    # --- top outliers ---
    print("=== top 10 slowest requests (any class) ===")
    ranked = sorted((e for e in events if isinstance(e.get("req_ms"), (int, float))),
                    key=lambda e: e["req_ms"], reverse=True)[:10]
    for e in ranked:
        print(f"  {e['req_ms']}ms {e.get('class'):9s} size={e.get('size')} ad={e.get('ad')} "
              f"{time.strftime('%m-%d %H:%M UTC', time.gmtime(e['ts']))}")


if __name__ == "__main__":
    main()
