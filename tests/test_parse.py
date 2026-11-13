"""Regression tests for the 38.co.kr parsers.

Every case here is a shape that once failed SILENTLY on the live site -- an
empty or short broker list, a None that read as "not published yet". The
inputs are synthetic cell sequences in 38's layout, not copies of its pages:
the repo is public and the pages are theirs.

    python3 -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ipo_radar import parse  # noqa: E402


def page(cells):
    """A detail page reduced to what the parsers read: a flat run of cells."""
    return "<table><tr>%s</tr></table>" % "".join("<td>%s</td>" % c for c in cells)


def brokers(cells):
    warnings = []
    out = parse._parse_underwriters(parse._Fields(cells + ["주요일정"]), warnings)
    return [(u["name"], u["limit_low"], u["limit_high"]) for u in out], warnings


class RatioTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse._ratio("743.49:1"), 743.49)
        self.assertEqual(parse._ratio("1,051.38 : 1"), 1051.38)
        # 엠비디, 2026-11: 38 dropped the ':1'.
        self.assertEqual(parse._ratio("187.10"), 187.10)

    def test_not_a_ratio(self):
        for s in ("-", "", None, "187.10 (미달)"):
            self.assertIsNone(parse._ratio(s))

    def test_subscription_ratios(self):
        self.assertEqual(parse.subscription_ratios("청약경쟁률 666.78:1 (비례 1334:1)"),
                         {"total": 666.78, "prop": 1334.0})
        # 동방메디컬: spaces around the colon.
        self.assertEqual(parse.subscription_ratios("청약경쟁률 \xa0 106.30 : 1 (비례 213:1)"),
                         {"total": 106.30, "prop": 213.0})
        # REITs: 통합 only.
        self.assertEqual(parse.subscription_ratios("청약경쟁률 120.37:1 확정공모가"),
                         {"total": 120.37, "prop": None})
        self.assertEqual(parse.subscription_ratios("청약경쟁률 확정공모가 41,200 원"),
                         {"total": None, "prop": None})


class UnderwriterShapeTest(unittest.TestCase):
    def test_single_underwriter_summary_only(self):
        got, w = brokers(["주간사", "대신증권", "주식수: 227,500 주 / 청약한도: 11,000~13,000 주"])
        self.assertEqual(got, [("대신증권", 11000, 13000)])
        self.assertEqual(w, [])

    def test_multi_underwriter_table(self):
        got, w = brokers(["주간사", "KB증권,신한투자증권", "주식수: 1,000,000 주 / 청약한도: - 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "KB증권", "700,000 주", "30,000~40,000 주", "대표주관",
                          "신한투자증권", "300,000 주", "13,000~16,000 주", "공동주관"])
        self.assertEqual(got, [("KB증권", 30000, 40000), ("신한투자증권", 13000, 16000)])
        self.assertEqual(w, [])

    def test_three_cell_rows(self):
        # An empty 기타 is dropped. A fixed 4-cell stride read 'NH투자증권' as
        # 한투's role and lost NH -- the brokerage with the higher limit.
        got, _ = brokers(["주간사", "한국투자증권,NH투자증권", "주식수: 900,000 주 / 청약한도: - 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "한국투자증권", "425,000 주", "14,000~17,000 주",
                          "NH투자증권", "750,000 주", "26,000~30,000 주"])
        self.assertEqual(got, [("한국투자증권", 14000, 17000), ("NH투자증권", 26000, 30000)])

    def test_table_without_limits_summary_with_one(self):
        # 에이치엘지노믹스: rows read '- 주', the range is on the summary line.
        got, _ = brokers(["주간사", "KB증권,IBK투자증권", "주식수: 641250 주 / 청약한도: 19,000~22,500 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "KB증권", "384,750 ~ 461,700 주", "- 주", "대표",
                          "IBK투자증권", "42,750 ~ 51,300 주", "- 주", "공동"])
        self.assertEqual(got, [("KB증권", 19000, 22500), ("IBK투자증권", 19000, 22500)])

    def test_table_lists_only_junior_underwriter(self):
        # The lead is on the summary line alone (신영 27,000~36,000).
        got, _ = brokers(["주간사", "신영증권", "주식수: 362,500~435,000 주 / 청약한도: 27,000~36,000 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "유진투자증권", "72,500 주", "7,000 주"])
        self.assertEqual(got, [("유진투자증권", 7000, 7000), ("신영증권", 27000, 36000)])

    def test_limits_not_published_yet(self):
        # 에스에프씨 before its limits were filled in: brokers, no limits, no warning.
        got, w = brokers(["주간사", "미래에셋증권,삼성증권", "주식수: 1,250,000~1,500,000 주 / 청약한도: - 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "미래에셋증권", "809,880 ~ 971,855 주", "- 주", "대표주관",
                          "삼성증권", "53,992 ~ 64,790 주", "- 주", "인수"])
        self.assertEqual(got, [("미래에셋증권", None, None), ("삼성증권", None, None)])
        self.assertEqual(w, [])

    def test_broker_name_without_jeungkwon(self):
        # DB금융투자 has no '증권'; its row was dropped and the lower limit won.
        got, w = brokers(["주간사", "DB금융투자,NH투자증권", "주식수: 500,000 주 / 청약한도: - 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "DB금융투자", "300,000 주", "13,000~15,750 주", "공동대표",
                          "NH투자증권", "200,000 주", "9,000~10,500 주", "공동대표"])
        self.assertEqual(got, [("DB금융투자", 13000, 15750), ("NH투자증권", 9000, 10500)])
        self.assertEqual(w, [])

    def test_dot_separated_summary(self):
        # HD현대마린솔루션: "KB증권,신한.하나증권" -- dots, abbreviated names.
        got, w = brokers(["주간사", "KB증권,신한.하나증권", "주식수: 1,000,000 주 / 청약한도: - 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "KB증권", "600,000 주", "30,000~40,000 주",
                          "신한투자증권", "250,000 주", "13,000~16,000 주",
                          "하나증권", "150,000 주", "20,000~24,000 주"])
        self.assertEqual([g[0] for g in got], ["KB증권", "신한투자증권", "하나증권"])
        self.assertEqual(w, [])

    def test_named_broker_missing_from_table_warns(self):
        _, w = brokers(["주간사", "KB증권,신한투자증권", "주식수: 1,000,000 주 / 청약한도: - 주",
                        "인수회사", "주식수", "청약한도", "기타",
                        "KB증권", "700,000 주", "30,000~40,000 주", "대표주관"])
        self.assertEqual(len(w), 1)
        self.assertIn("신한투자증권", w[0])

    def test_truncated_name_does_not_warn(self):
        _, w = brokers(["주간사", "신한투자", "주식수: 1,000,000 주 / 청약한도: - 주",
                        "인수회사", "주식수", "청약한도", "기타",
                        "신한투자증권", "700,000 주", "30,000~40,000 주", "대표주관"])
        self.assertEqual(w, [])


    def test_limit_at_or_above_allotment_is_dropped(self):
        # 파로스아이바이오: 38 printed its 주식수 as the 청약한도. 산일전기: 삼성
        # (65,000~78,000주) carried 미래's 90,000~105,000.
        got, w = brokers(["주간사", "한국투자증권", "주식수: 350,000~420,000 주 / 청약한도: 350,000~420,000 주"])
        self.assertEqual(got, [("한국투자증권", None, None)])
        self.assertEqual(len(w), 1)
        got, w = brokers(["주간사", "미래에셋증권,삼성증권", "주식수: 1,900,000 주 / 청약한도: - 주",
                          "인수회사", "주식수", "청약한도", "기타",
                          "미래에셋증권", "1,560,000 ~ 1,872,000 주", "90,000 ~ 105,000 주",
                          "삼성증권", "65,000 ~ 78,000 주", "90,000 ~ 105,000 주"])
        self.assertEqual(got, [("미래에셋증권", 90000, 105000), ("삼성증권", None, None)])
        self.assertIn("삼성증권", w[0])


class EstimateLimitTest(unittest.TestCase):
    def test_uses_largest_allotment(self):
        est = parse.estimate_limit([
            {"name": "미래에셋증권", "shares": {"low": 809880, "high": 971855}},
            {"name": "삼성증권", "shares": {"low": 53992, "high": 64790}},
        ])
        self.assertEqual(est["broker"], "미래에셋증권")
        self.assertEqual(est["shares"], round(971855 * parse.LIMIT_SHARE))
        self.assertEqual(est["low"], round(971855 * parse.LIMIT_SHARE_RANGE["multi"][0]))
        self.assertGreater(est["high"], est["shares"])
        self.assertIn("우대 미적용", est["note"])
        self.assertNotIn("과대", est["note"])

    def test_summary_only_multi_flags_overestimate(self):
        # 세나테크놀로지: both brokerages carry the deal's combined 140,000.
        est = parse.estimate_limit([
            {"name": "KB증권", "shares": {"low": 140000, "high": 140000}},
            {"name": "신한투자증권", "shares": {"low": 140000, "high": 140000}},
        ])
        self.assertIn("과대추정", est["note"])

    def test_nothing_to_go_on(self):
        self.assertIsNone(parse.estimate_limit([{"name": "KB증권", "shares": {"low": None, "high": None}}]))
        self.assertIsNone(parse.estimate_limit([]))


class ParseWarningTest(unittest.TestCase):
    def test_unreadable_ratio_on_detail_page(self):
        rec = parse.parse_detail(page(["확정공모가", "7,000 원", "기관경쟁률", "187.10배"]), "1")
        self.assertIsNone(rec["institutional_ratio"])
        self.assertTrue(any("기관경쟁률" in w for w in rec["parse_warnings"]))

    def test_unpublished_is_not_a_warning(self):
        rec = parse.parse_detail(page(["확정공모가", "-", "기관경쟁률", "-", "의무보유확약", ""]), "1")
        self.assertEqual(rec["parse_warnings"], [])

    def test_demand_results_row(self):
        row = lambda ratio: ("<tr>" + "".join("<td>%s</td>" % c for c in [
            "테스트", "2026.11.02", "9,600~11,700", "7,000", "14,000", ratio, "-", "하나증권"]) + "</tr>")
        warnings = []
        got = parse.parse_demand_results("<table>%s%s</table>" % (row("187.10배"), row("-")), warnings)
        self.assertEqual(got, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("187.10배", warnings[0])


if __name__ == "__main__":
    unittest.main()
