"""The GO / WATCH boundary on 의무보유확약, against config.json as shipped.

    python3 -m unittest discover tests
"""

import json
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from ipo_radar import score  # noqa: E402


def deal(lockup, ratio=900.0, breakdown=None):
    return {"name": "가나", "institutional_ratio": ratio, "lockup_pct": lockup,
            "final_price": 20000, "band_low": 16000, "band_high": 20000,
            "lockup_breakdown": breakdown or {}}


class LockupBoundaryTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "config.json")) as fh:
            self.cfg = json.load(fh)

    def test_two_percent_is_go_at_600_and_band_top(self):
        # 600:1+ at the band top with 확약 2-5%: 29 deals, all net-positive
        # (2026-11-13), so the 확약 +1 that makes GO starts at 2%.
        self.assertEqual(score.evaluate(deal(2.0), self.cfg)["verdict"], "GO")
        self.assertEqual(score.evaluate(deal(4.9), self.cfg)["verdict"], "GO")
        self.assertEqual(score.evaluate(deal(1.99), self.cfg)["verdict"], "WATCH")

    def test_tenor_bonus_still_needs_the_lockup_bar(self):
        # 80% long tenor of a 0.87% 확약 holds nothing back: no bonus.
        weak = deal(0.87, ratio=450.0, breakdown={"total": 100, "m3": 80})
        self.assertEqual(score.evaluate(weak, self.cfg)["score"], 3)
        strong = deal(2.5, ratio=450.0, breakdown={"total": 100, "m3": 80})
        self.assertEqual(score.evaluate(strong, self.cfg)["score"], 5)


if __name__ == "__main__":
    unittest.main()
