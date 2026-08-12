"""Expected-return forecasting from historical analogues.

At the moment 수요예측 publishes, two unknowns stand between the screen's verdict
and the cash outcome: how hard *retail* will compete (which sets the allocation)
and where the stock will open. Both are estimated by reweighting past deals that
resembled this one.

Design constraints, in order of importance:

* **n is 234.** Every accuracy gain here comes from smooth weighting and honest
  evaluation, not from model complexity. There are no fitted coefficients --
  only bandwidths set by rule -- so there is nothing to overfit in the usual
  sense; the risk is selection across variants, which the walk-forward harness
  in ``evaluate.py`` is there to police.
* **(multiple, ret) are drawn jointly** from the same past deal, never averaged
  independently. Retail enthusiasm and the opening pop are correlated, and a
  component-wise model would silently destroy that.
* **Log-space for the multiple.** 비례률/기관률 spans 0.41x to 4.83x; arithmetic
  means on that are meaningless.
* Stdlib only, Python 3.9 -- the daily job runs on stock macOS python3.
"""

import math
import random
from typing import Dict, List, Optional

# Bandwidths in standardized units; wider = smoother = more effective samples.
# Validated by walk-forward in evaluate.py over 234 deals. Two features only:
# adding 3개월+ tenor share, band position, or recency decay each made rank
# correlation WORSE, which is the overfitting the harness exists to catch.
# Bandwidth 0.6 is not the argmax (0.30 scored rho .578 vs .534) -- it is chosen
# one step to the smooth side because MAE was identical across the whole sweep
# and 0.6 rests on ~61 effective neighbours instead of ~31.
DEFAULT = {
    "features": ("linst", "lock"),
    "bandwidth": 0.6,
    "halflife_days": 0.0,   # recency decay tested and rejected: rho .315 vs .397
    "min_history": 40,
}

# Below this 기관경쟁률 the estimate is not trustworthy. Calibration by tercile:
# at >=600 predicted EV tracked realised net (+1.4/+6.3/+21.4 predicted vs
# +2.2/+4.2/+15.3 realised, bias +2.5만). Below 300 the bias was +178.9만 -- weak
# demand means a huge allocation, so a single bad deal loses tens of millions and
# 28 historical examples cannot pin that tail down.
CALIBRATED_MIN_INST = 600.0

# Per-feature scale, so bandwidth is comparable across features. Set from the
# spread actually observed in the sample rather than fitted.
SCALE = {"linst": 0.45, "lock": 12.0, "tenor3": 0.28, "bpos": 1.0}


def _feat(d: Dict, name: str) -> float:
    if name == "linst":
        return math.log10(max(d["inst"], 1.0))
    if name == "lock":
        return d["lock"] or 0.0
    if name == "tenor3":
        return d.get("tenor3") or 0.0
    if name == "bpos":
        return float(d.get("bpos") or 0)
    raise KeyError(name)


def _days_between(a: str, b: str) -> int:
    ay, am, ad = [int(x) for x in a.split("-")]
    by, bm, bd = [int(x) for x in b.split("-")]
    import datetime
    return (datetime.date(by, bm, bd) - datetime.date(ay, am, ad)).days


def weights(target: Dict, history: List[Dict], cfg: Dict) -> List[float]:
    """Gaussian kernel weight per historical deal, optionally decayed by age."""
    feats = cfg["features"]
    h = cfg["bandwidth"]
    hl = cfg.get("halflife_days") or 0.0
    out = []
    for d in history:
        d2 = 0.0
        for f in feats:
            delta = (_feat(target, f) - _feat(d, f)) / SCALE[f]
            d2 += delta * delta
        w = math.exp(-0.5 * d2 / (h * h))
        if hl > 0:
            age = _days_between(d["listing"], target["sub_start"])
            w *= 0.5 ** (max(age, 0) / hl)
        out.append(w)
    return out


def _wpercentile(pairs, p):
    """pairs: list of (value, weight), already unsorted. Weighted percentile."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return 0.0
    cut = total * p / 100.0
    run = 0.0
    for v, w in pairs:
        run += w
        if run >= cut:
            return v
    return pairs[-1][0]


def predict(target: Dict, history: List[Dict], cfg: Optional[Dict] = None,
            equal_shares: int = 0, bootstrap: int = 0) -> Optional[Dict]:
    """Distribution of net won for ``target``, from reweighted analogues.

    Each analogue contributes one scenario: its retail-enthusiasm multiple sets
    the allocation, its opening move sets the gain. Returns None when history is
    too thin to say anything.
    """
    c = dict(DEFAULT)
    if cfg:
        c.update(cfg)
    if len(history) < c["min_history"]:
        return None

    w = weights(target, history, c)
    tw = sum(w)
    if tw <= 0:
        return None

    limit, price, cost = target["limit"], target["price"], target["cost"]
    scen = []
    for d, wi in zip(history, w):
        if wi <= 0:
            continue
        prop = target["inst"] * d["mult"]
        shares = int(limit // prop) + equal_shares if prop > 0 else equal_shares
        scen.append((shares * price * (d["ret"] / 100.0) - cost, wi, shares))

    if not scen:
        return None
    ev = sum(v * wi for v, wi, _ in scen) / tw
    nets = [(v, wi) for v, wi, _ in scen]
    shs = [(s, wi) for _, wi, s in scen]
    win = 100.0 * sum(wi for v, wi, _ in scen if v > 0) / tw
    # Effective sample size (Kish): how many deals this estimate really rests on.
    n_eff = (tw * tw) / sum(wi * wi for wi in w if wi > 0)

    out = {
        "ev": ev,
        "calibrated": target["inst"] >= CALIBRATED_MIN_INST,
        "win_prob": win,
        "n_eff": n_eff,
        "n_history": len(history),
        "shares_p10": _wpercentile(shs, 10), "shares_med": _wpercentile(shs, 50),
        "shares_p90": _wpercentile(shs, 90),
        "p10": _wpercentile(nets, 10), "p25": _wpercentile(nets, 25),
        "p50": _wpercentile(nets, 50), "p75": _wpercentile(nets, 75),
        "p90": _wpercentile(nets, 90),
    }
    if bootstrap:
        # Resample analogues in proportion to their weight; the spread of the
        # resulting EVs is the uncertainty in the estimate itself, which a bare
        # point estimate hides.
        rng = random.Random(12345)
        vals = [v for v, _, _ in scen]
        wts = [wi for _, wi, _ in scen]
        cum, acc = [], 0.0
        for x in wts:
            acc += x
            cum.append(acc)
        evs = []
        n = len(vals)
        for _ in range(bootstrap):
            s = 0.0
            for _ in range(n):
                r = rng.random() * acc
                lo, hi = 0, n - 1
                while lo < hi:
                    mid = (lo + hi) // 2
                    if cum[mid] < r:
                        lo = mid + 1
                    else:
                        hi = mid
                s += vals[lo]
            evs.append(s / n)
        evs.sort()
        out["ev_lo"] = evs[int(0.05 * (len(evs) - 1))]
        out["ev_hi"] = evs[int(0.95 * (len(evs) - 1))]
    return out


def history_for(target: Dict, all_deals: List[Dict]) -> List[Dict]:
    """Deals whose outcome was already KNOWN when this one had to be decided.

    The gate is the analogue's *listing* date against the target's subscription
    date -- not its position in the list. A deal that had subscribed but not yet
    listed has no 시초/공모 to learn from, and using one leaks the future.
    """
    cutoff = target["sub_start"]
    return [d for d in all_deals if d["listing"] < cutoff and d["no"] != target["no"]]
