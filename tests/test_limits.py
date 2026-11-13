"""Tests for the live 청약한도: run.my_limit() over 투자설명서 limits, and the
DART listing cache it depends on.

The limits are synthetic dicts in parse_limits()'s shape and the tiers are
made up -- not anyone's real account tiers. No keychain, no network.

    python3 -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ipo_radar import dart, report  # noqa: E402
import run  # noqa: E402


def lim(base, tiers=None, pool=(300000, 300000)):
    """One brokerage as parse_limits() returns it; tiers {pct: amount}."""
    t = {k: (v, v) if isinstance(v, int) else v for k, v in (tiers or {100: base}).items()}
    return {"pool": pool, "table": None, "derived": False, "tiers": t,
            "base": (base, base) if isinstance(base, int) else base}


def under(name, low, high):
    return {"name": name, "limit_low": low, "limit_high": high, "shares": 300000}


class MyLimitTest(unittest.TestCase):
    def test_printed_tier_is_used(self):
        # 38 shows 한투's 50% tier (5,000~6,000); the prospectus base is 10,000
        # and prints 300% as 30,000.
        rec = {"underwriters": [under("한국투자증권", 5000, 6000)]}
        limits = {"한국투자증권": lim(10000, {50: 5000, 100: 10000, 300: 30000})}
        got = run.my_limit(rec, {"my_tier_pct": {"한국투자증권": 300}}, limits)
        self.assertEqual((got["shares"], got["source"], got["multiplier"]), (30000, "prospectus", 3.0))
        self.assertEqual((got["base"], got["l38"]), (10000, (5000, 6000)))

    def test_tier_not_printed_is_base_times_pct(self):
        rec = {"underwriters": [under("KB증권", 9000, 11000)]}
        got = run.my_limit(rec, {"my_tier_pct": {"KB증권": 150}}, {"KB증권": lim(9000)})
        self.assertEqual(got["shares"], 13500)

    def test_fallback_multiplier_is_not_applied_to_the_prospectus_base(self):
        # The 38-path multiple (x3 over a 50% tier) must not triple the base.
        rec = {"underwriters": [under("키움증권", 5000, 6000)]}
        cfg = {"my_limit_multiplier": {"키움증권": 3.0}}
        got = run.my_limit(rec, cfg, {"키움증권": lim(10000, {50: 5000, 100: 10000, 200: 20000})})
        self.assertEqual((got["shares"], got["multiplier"]), (10000, None))
        # Without the prospectus the 38 path is unchanged.
        got = run.my_limit(rec, cfg, None, "DART 키 없음")
        self.assertEqual((got["shares"], got["source"], got["limits_note"]), (15000, "38", "DART 키 없음"))

    def test_no_prospectus_is_todays_behaviour(self):
        rec = {"underwriters": [under("KB증권", 10000, 12000), under("신한투자증권", 4000, 5000)]}
        cfg = {"my_limit_multiplier": {"신한투자증권": 1.5}}
        got = run.my_limit(rec, cfg)
        self.assertEqual((got["broker"], got["shares"], got["source"]), ("KB증권", 12000, "38"))

    def test_unread_brokerage_falls_back_to_38(self):
        # NH's base was not read; it competes on 38's figure, labelled.
        rec = {"underwriters": [under("NH투자증권", 20000, 24000), under("KB증권", 8000, 9000)]}
        limits = {"NH투자증권": lim(None, {}), "KB증권": lim(8000)}
        limits["NH투자증권"]["base"] = None
        got = run.my_limit(rec, {}, limits)
        self.assertEqual((got["broker"], got["shares"], got["source"]), ("NH투자증권", 24000, "38"))

    def test_brokerage_only_on_38_is_dropped_when_all_were_read(self):
        # 산일전기: 38 lists 삼성 with 미래's limit; the prospectus has 미래 only.
        rec = {"underwriters": [under("미래에셋증권", 30000, 36000), under("삼성증권", 30000, 36000)]}
        got = run.my_limit(rec, {}, {"미래에셋증권": lim(30000)})
        self.assertEqual((got["broker"], got["shares"]), ("미래에셋증권", 30000))

    def test_ranged_prospectus_takes_the_low_end(self):
        rec = {"underwriters": [under("NH투자증권", 12000, 15000)]}
        got = run.my_limit(rec, {}, {"NH투자증권": lim((12000, 15000), {100: (12000, 15000)})})
        self.assertEqual((got["shares"], got["ranged"]), (12000, (12000, 15000)))
        self.assertTrue(any("범위" in line for line in report.my_limit_lines(got)))

    def test_needs_account_under_another_spelling(self):
        # The prospectus spells 유진투자증권 "유진증권".
        rec = {"underwriters": [under("신영증권", 1000, 1300)]}
        got = run.my_limit(rec, {"no_account": ["유진투자증권"]},
                           {"유진증권": lim(20000), "신영증권": lim(1000)})
        self.assertEqual((got["broker"], got["needs_account"]), ("유진증권", True))

    def test_lines_name_the_source_and_38s_figure(self):
        rec = {"underwriters": [under("유안타증권", 5000, 6000)]}
        got = run.my_limit(rec, {"my_tier_pct": {"유안타증권": 200}},
                           {"유안타증권": lim(10000, {50: 5000, 100: 10000, 200: 20000})})
        lines = report.my_limit_lines(got)
        self.assertIn("우대 200%, 투자설명서", lines[0])
        self.assertIn("38 표기 5,000~6,000주", lines[1])


class ListingCacheTest(unittest.TestCase):
    def test_listing_saved_inside_its_window_is_not_reused(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as fh:
            saved = time.mktime((2026, 11, 10, 12, 0, 0, 0, 0, -1))
            os.utime(fh.name, (saved, saved))
            # Saved on 11-10 for a window ending 11-12: a later 정정 is missing.
            self.assertFalse(dart._listing_fresh(fh.name, "2026-11-12"))
            self.assertFalse(dart._listing_fresh(fh.name, "2026-11-10"))
            self.assertTrue(dart._listing_fresh(fh.name, "2026-11-09"))
        self.assertFalse(dart._listing_fresh("/nonexistent/list.json", "2026-01-01"))

    def test_live_limits_without_a_key(self):
        self.assertEqual(dart.live_limits({"subscription_start": "2026-11-20"}, None), (None, "DART 키 없음"))


if __name__ == "__main__":
    unittest.main()
