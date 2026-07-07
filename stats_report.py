#!/usr/bin/env python3
"""
Aggregate stats-gh.ndjson + stats-mac.ndjson into a human-readable Telegram
report, scoped to the last N hours (default 2h, matching the heartbeat cadence).

Kept as a standalone script (not inlined in any workflow YAML run: block) on
purpose — a past incident broke a workflow's YAML parsing silently when
Python was embedded directly in a `run: |` block. Calling `python3
stats_report.py` from YAML is a single plain line with no quoting hazards.

Usage: python3 stats_report.py [--hours N]
Reads stats-gh.ndjson and stats-mac.ndjson from the current directory (missing
files are treated as empty — not an error, e.g. before the first record ever
lands). Prints a Telegram-ready summary to stdout (plain text, real newlines;
the caller is responsible for %0A-encoding if posting via curl -d).
"""
import argparse
import json
import sys
import time


def load_records(path, since_ts):
    """Read an ndjson file, return records with ts >= since_ts. Missing/unreadable file -> []."""
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = obj.get("ts")
                if not isinstance(ts, (int, float)):
                    continue
                if ts >= since_ts:
                    records.append(obj)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return records


def summarize(records):
    """Flatten a source's records (each with a 'workers' list) into totals."""
    total_attempts = 0
    total_rate_limits = 0
    total_elapsed = 0
    min_size = None
    max_size = None
    per_ad = {}  # ad -> attempts
    per_ad_429 = {}  # ad -> rate_limits
    per_size = {}  # ocpu (int) -> attempts, from s1..s4 keys
    total_no_cap = 0
    total_other = 0
    total_limit = 0
    max_gap = 0
    last_pace = None
    last_avail = None
    generations = 0

    for rec in records:
        workers = rec.get("workers")
        if not isinstance(workers, list):
            continue
        generations += 1
        for w in workers:
            if not isinstance(w, dict):
                continue
            attempts = w.get("attempts", 0) or 0
            rate_limits = w.get("rate_limits", 0) or 0
            elapsed = w.get("elapsed", 0) or 0
            mn = w.get("min_size")
            mx = w.get("max_size")
            ad = w.get("ad", "?")

            try:
                total_attempts += int(attempts)
                total_rate_limits += int(rate_limits)
                total_elapsed += int(elapsed)
            except (TypeError, ValueError):
                continue

            if isinstance(mn, (int, float)):
                min_size = mn if min_size is None else min(min_size, mn)
            if isinstance(mx, (int, float)):
                max_size = mx if max_size is None else max(max_size, mx)

            per_ad[ad] = per_ad.get(ad, 0) + (int(attempts) if isinstance(attempts, (int, float)) else 0)
            if isinstance(rate_limits, (int, float)) and rate_limits:
                per_ad_429[ad] = per_ad_429.get(ad, 0) + int(rate_limits)

            nc = w.get("no_cap")
            if isinstance(nc, (int, float)):
                total_no_cap += int(nc)
            ot = w.get("other")
            if isinstance(ot, (int, float)):
                total_other += int(ot)
            lm = w.get("limit")
            if isinstance(lm, (int, float)):
                total_limit += int(lm)
            for ocpu in (1, 2, 3, 4):
                sv = w.get(f"s{ocpu}")
                if isinstance(sv, (int, float)) and sv:
                    per_size[ocpu] = per_size.get(ocpu, 0) + int(sv)
            mg = w.get("max_gap")
            if isinstance(mg, (int, float)) and mg > max_gap:
                max_gap = int(mg)
            pc = w.get("pace")
            if isinstance(pc, (int, float)):
                last_pace = int(pc)
            av = w.get("avail")
            if isinstance(av, (int, float)) and av >= 0:
                last_avail = int(av)

    return {
        "generations": generations,
        "total_attempts": total_attempts,
        "total_rate_limits": total_rate_limits,
        "total_elapsed": total_elapsed,
        "min_size": min_size,
        "max_size": max_size,
        "per_ad": per_ad,
        "per_ad_429": per_ad_429,
        "per_size": per_size,
        "total_no_cap": total_no_cap,
        "total_other": total_other,
        "max_gap": max_gap,
        "last_pace": last_pace,
        "last_avail": last_avail,
        "total_limit": total_limit,
    }


def fmt_rate(attempts, elapsed):
    if elapsed <= 0:
        return "0.00"
    return f"{attempts / elapsed:.2f}"


def fmt_size_range(min_size, max_size, gb_per_ocpu=6):
    if min_size is None or max_size is None:
        return "нет данных"
    return f"{min_size}-{max_size} OCPU ({min_size * gb_per_ocpu}-{max_size * gb_per_ocpu} ГБ)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=2.0)
    parser.add_argument("--gh-file", default="stats-gh.ndjson")
    parser.add_argument("--mac-file", default="stats-mac.ndjson")
    args = parser.parse_args()

    since_ts = time.time() - args.hours * 3600
    gh_records = load_records(args.gh_file, since_ts)
    mac_records = load_records(args.mac_file, since_ts)

    gh = summarize(gh_records)
    mac = summarize(mac_records)

    combined_attempts = gh["total_attempts"] + mac["total_attempts"]
    combined_rate_limits = gh["total_rate_limits"] + mac["total_rate_limits"]

    sizes = [s for s in (gh["min_size"], mac["min_size"]) if s is not None]
    combined_min = min(sizes) if sizes else None
    sizes = [s for s in (gh["max_size"], mac["max_size"]) if s is not None]
    combined_max = max(sizes) if sizes else None

    hours_label = f"{args.hours:g}ч"

    lines = []
    lines.append(f"Статистика за последние {hours_label}:")
    lines.append(f"Всего запросов: {combined_attempts} (GH: {gh['total_attempts']}, Mac: {mac['total_attempts']})")
    lines.append(f"Диапазон размеров: {fmt_size_range(combined_min, combined_max)}")

    if gh["per_ad"]:
        ad_parts = ", ".join(f"{ad}: {n}" for ad, n in sorted(gh["per_ad"].items()))
        lines.append(f"По зонам (GH): {ad_parts}")

    combined_ad_429 = {}
    for src in (gh, mac):
        for ad, n in src["per_ad_429"].items():
            combined_ad_429[ad] = combined_ad_429.get(ad, 0) + n
    if combined_ad_429:
        parts = ", ".join(f"{ad}: {n}" for ad, n in sorted(combined_ad_429.items()))
        lines.append(f"429 по зонам: {parts}")

    combined_size = {}
    for src in (gh, mac):
        for ocpu, n in src["per_size"].items():
            combined_size[ocpu] = combined_size.get(ocpu, 0) + n
    if combined_size:
        parts = ", ".join(f"{ocpu}oc: {n}" for ocpu, n in sorted(combined_size.items()))
        lines.append(f"Попытки по размерам: {parts}")

    combined_no_cap = gh["total_no_cap"] + mac["total_no_cap"]
    combined_other = gh["total_other"] + mac["total_other"]
    combined_limit = gh.get("total_limit", 0) + mac.get("total_limit", 0)
    if combined_no_cap or combined_rate_limits or combined_other or combined_limit:
        # Honest, neutral labels. "no capacity" = Oracle processed the request but
        # had no host (a negative answer, NOT a success). "limit" = LimitExceeded/
        # QuotaExceeded (the request was gated by an account/quota check before
        # capacity was even looked at — we do NOT claim which specific limit).
        # "429" = TooManyRequests (throttled, not considered). Only no-capacity
        # probes are "useful" — they'd have caught a slot had one existed.
        lines.append(
            f"Ответы Oracle: no-capacity (мест нет): {combined_no_cap}, "
            f"limit/quota (отсечён квотой): {combined_limit}, "
            f"429 (throttle): {combined_rate_limits}, прочие: {combined_other}"
        )

    # Real A1 allowance Oracle reports for this tenancy (Limits API). This is
    # the FACT behind why 3-4oc probes get limit/quota: free-tier A1 here is
    # capped below 4 OCPU. Shown so the picture isn't a guess.
    avails = [a for a in (gh.get("last_avail"), mac.get("last_avail")) if a is not None]
    if avails:
        lines.append(f"Доступно A1 по данным Oracle: {max(avails)} OCPU на зону")

    gh_rate = fmt_rate(gh["total_attempts"], gh["total_elapsed"])
    mac_rate = fmt_rate(mac["total_attempts"], mac["total_elapsed"])
    lines.append(f"Скорость запросов: GH {gh_rate}/сек, Mac {mac_rate}/сек")

    def avg_interval(src):
        if src["total_attempts"] > 0 and src["total_elapsed"] > 0:
            return f"{src['total_elapsed'] / src['total_attempts']:.0f}с"
        return "—"
    gaps = [g for g in (gh["max_gap"], mac["max_gap"]) if g]
    worst_gap = f"{max(gaps)}с" if gaps else "—"
    lines.append(
        f"Интервал между запросами: средний GH {avg_interval(gh)} / Mac {avg_interval(mac)}, "
        f"макс. слепое окно (на воркера): {worst_gap}"
    )

    paces = [p for p in (gh["last_pace"], mac["last_pace"]) if p is not None]
    if paces:
        pace_parts = []
        if gh["last_pace"] is not None:
            pace_parts.append(f"GH {gh['last_pace']}с")
        if mac["last_pace"] is not None:
            pace_parts.append(f"Mac {mac['last_pace']}с")
        lines.append(f"Текущий темп (AIMD): {', '.join(pace_parts)} на воркера")

    lines.append(f"Превышений лимита (rate-limit): {combined_rate_limits}")
    lines.append(f"Поколений хантера учтено: GH {gh['generations']}, Mac {mac['generations']}")

    print("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
