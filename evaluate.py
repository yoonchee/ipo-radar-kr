#!/usr/bin/env python3
"""Walk-forward evaluation of the forecast models.

Every candidate is scored the same way: for each deal in time order, predict
using ONLY deals that had already listed when this one had to be decided, then
compare against what actually happened. No random splits -- the sample spans a
drifting regime, and shuffling it would leak the future into the past.

Three things this harness is careful about, each of which flipped a conclusion
during development:

* **Common subset.** Models that decline to predict (too few analogues) would
  otherwise be scored on an easier subset. Every metric below is computed over
  the deals where *all* variants produced a prediction.
* **Heavy tails.** Net won spans -2,800만 to +2,800만, so MAE is dominated by a
  handful of deals. Median absolute error and rank correlation carry more
  signal about whether a model is actually informative.
* **Sign accuracy is nearly useless here** -- 79% of deals have positive net, so
  "always yes" scores 79%. It is reported only to show that nothing beats it.

    ./evaluate.py
"""

import json
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipo_radar import forecast

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "cache", "dataset.json")
SKIP_FIRST = 60
MAN = 10000.0


def load():
    with open(DATA, encoding="utf-8") as fh:
        rows = json.load(fh)
    rows.sort(key=lambda r: r["sub_start"])
    return rows


def baseline_global(target, history):
    if len(history) < forecast.DEFAULT["min_history"]:
        return None
    mult = 10 ** statistics.median([math.log10(d["mult"]) for d in history])
    ret = statistics.median([d["ret"] for d in history])
    shares = int(target["limit"] // (target["inst"] * mult))
    return {"ev": shares * target["price"] * (ret / 100.0) - target["cost"]}


def baseline_hardbin(target, history):
    lo, hi = target["inst"] / 1.5, target["inst"] * 1.5
    same = (lambda d: d["lock"] < 5.0) if target["lock"] < 5.0 else (lambda d: d["lock"] >= 5.0)
    comps = [d for d in history if lo <= d["inst"] <= hi and same(d)]
    if len(comps) < 12:
        comps = [d for d in history if lo <= d["inst"] <= hi]
    if len(comps) < 8:
        return None
    nets = [int(target["limit"] // (target["inst"] * d["mult"])) * target["price"] * (d["ret"] / 100.0)
            - target["cost"] for d in comps]
    return {"ev": sum(nets) / len(nets)}


def kernel(cfg):
    return lambda t, h: forecast.predict(t, h, cfg)


def spearman(xs, ys):
    def rank(a):
        order = sorted(range(len(a)), key=lambda i: a[i])
        r = [0.0] * len(a)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def collect(models, rows):
    """Predictions per model per deal, plus the set of deals all models covered."""
    preds = {name: {} for name, _ in models}
    for i, target in enumerate(rows):
        if i < SKIP_FIRST:
            continue
        hist = forecast.history_for(target, rows)
        for name, model in models:
            p = model(target, hist)
            if p:
                preds[name][target["no"]] = p["ev"]
    common = None
    for name, _ in models:
        keys = set(preds[name])
        common = keys if common is None else (common & keys)
    return preds, sorted(common or [])


def score(evs, acts):
    errs = sorted(abs(e - a) for e, a in zip(evs, acts))
    return {
        "n": len(evs),
        "mae": sum(errs) / len(errs) / MAN,
        "medae": errs[len(errs) // 2] / MAN,
        "sign": 100.0 * sum(1 for e, a in zip(evs, acts) if (e > 0) == (a > 0)) / len(evs),
        "rho": spearman(evs, acts),
        "bias": (sum(evs) - sum(acts)) / len(evs) / MAN,
    }


def boot_diff(evs_a, evs_b, acts, n=2000, seed=7):
    """Bootstrap CI on (MAE_a - MAE_b). Straddling zero means 'no difference'."""
    rng = random.Random(seed)
    m = len(acts)
    diffs = []
    for _ in range(n):
        idx = [rng.randrange(m) for _ in range(m)]
        a = sum(abs(evs_a[i] - acts[i]) for i in idx) / m
        b = sum(abs(evs_b[i] - acts[i]) for i in idx) / m
        diffs.append((a - b) / MAN)
    diffs.sort()
    return diffs[int(0.05 * (n - 1))], diffs[int(0.95 * (n - 1))]


def main():
    rows = load()
    models = [
        ("A global median", baseline_global),
        ("B hard bins [INCUMBENT]", baseline_hardbin),
        # Whatever run.py actually uses, so the table always scores the shipped
        # model rather than a neighbour of it. Reads forecast.DEFAULT directly:
        # retuning that config retunes this row.
        ("S kernel h=%.1f [SHIPPED]" % forecast.DEFAULT["bandwidth"], kernel(forecast.DEFAULT)),
        ("C kernel h=1.0", kernel({"features": ("linst", "lock"), "bandwidth": 1.0})),
        ("D kernel h=0.7", kernel({"features": ("linst", "lock"), "bandwidth": 0.7})),
        ("E kernel h=1.5", kernel({"features": ("linst", "lock"), "bandwidth": 1.5})),
        ("F kernel h=0.5", kernel({"features": ("linst", "lock"), "bandwidth": 0.5})),
        ("G C + tenor3", kernel({"features": ("linst", "lock", "tenor3"), "bandwidth": 1.0})),
        ("H C + bpos", kernel({"features": ("linst", "lock", "bpos"), "bandwidth": 1.0})),
        ("I C + recency 365d", kernel({"features": ("linst", "lock"), "bandwidth": 1.0, "halflife_days": 365})),
    ]
    preds, common = collect(models, rows)
    by_no = {r["no"]: r for r in rows}
    acts = [by_no[no]["net"] for no in common]

    print("dataset %d deals, %s .. %s" % (len(rows), rows[0]["sub_start"], rows[-1]["sub_start"]))
    print("evaluated on the %d deals every variant could predict (walk-forward, no leakage)" % len(common))
    base = 100.0 * sum(1 for a in acts if a > 0) / len(acts)
    print("base rate positive: %.1f%%   mean actual net: %+.1f만\n" % (base, sum(acts) / len(acts) / MAN))

    print("%-26s %9s %10s %8s %8s %10s" % ("variant", "MAE(만)", "MedAE(만)", "sign%", "rho", "bias(만)"))
    print("-" * 76)
    table = {}
    for name, _ in models:
        evs = [preds[name][no] for no in common]
        s = score(evs, acts)
        table[name] = (s, evs)
        print("%-26s %9.1f %10.2f %7.1f%% %8.3f %+10.1f"
              % (name, s["mae"], s["medae"], s["sign"], s["rho"], s["bias"]))
    print("-" * 76)
    print("rho = Spearman rank corr between predicted EV and realised net."
          "\n      This is the metric that says whether a model ranks deals at all.\n")

    inc_name = "B hard bins [INCUMBENT]"
    inc_evs = table[inc_name][1]
    print("MAE difference vs incumbent, with 90%% bootstrap CI (negative = better):")
    for name, _ in models:
        if name == inc_name:
            continue
        evs = table[name][1]
        lo, hi = boot_diff(evs, inc_evs, acts)
        verdict = "BETTER" if hi < 0 else ("worse" if lo > 0 else "no difference")
        print("   %-26s %+7.2f만  CI [%+.2f, %+.2f]  -> %s"
              % (name, table[name][0]["mae"] - table[inc_name][0]["mae"], lo, hi, verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
