"""Regression tests for the OpenDART 증권발행실적보고서 parser.

Synthetic filings in DART's XML table layout -- not copies of real ones. Each
case is a shape seen in the wild that was once read wrong or not at all; the
filing that showed it is named in the comment.

    python3 -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ipo_radar import dart  # noqa: E402
import net_returns  # noqa: E402


def _cell(c, tag="TD"):
    """'text', or (text, rowspan, colspan)."""
    if isinstance(c, tuple):
        text, rs, cs = c
        attrs = (' ROWSPAN="%d"' % rs if rs > 1 else "") + (' COLSPAN="%d"' % cs if cs > 1 else "")
    else:
        text, attrs = c, ""
    return "<%s%s>%s</%s>" % (tag, attrs, text, tag)


def table(rows):
    # Numbers go in <TE>, as DART writes many of them; reading <TD> alone drops them.
    return "<TABLE><TBODY>%s</TBODY></TABLE>" % "".join(
        "<TR>%s</TR>" % "".join(_cell(c, "TE" if str(c).replace(",", "").isdigit() else "TD") for c in r)
        for r in rows)


def p(text):
    return "<P>%s</P>" % text


def doc(*parts, **kw):
    return ("<DOCUMENT><BODY>%s<COVER><COVER-TITLE>증권발행실적보고서</COVER-TITLE></COVER>%s</BODY></DOCUMENT>"
            % (kw.get("preamble", ""), "".join(parts)))


HEADER = [
    [("청약현황", 2, 3), ("배정현황", 1, 10), ("총배정수량(D=B+C)", 3, 1), ("배정비율(D/A)", 3, 1)],
    [("균등방식", 1, 5), ("비례방식", 1, 5)],
    ["청약단위", "청약건수", "청약수량(A)", "배정건수", "1건당배정수량", "추가배정건수", "1건당추가배정수량",
     "균등배정수량(B)", "배정건수", "1건당배정수량", "추가배정건수", "1건당추가배정수량", "비례배정수량(C)"],
]


def f(n):
    return "{:,}".format(int(n)) if n else "-"


def row(unit, apps, b, c, qty=None, d=None):
    """A main row in the usual 15-column layout; 청약수량 = 단위 x 건수 unless given."""
    qty = unit * apps if qty is None else qty
    d = b + c if d is None else d
    return [f(unit), f(apps), f(qty), f(apps), "1", "-", "-", f(b), f(apps) if c else "-", "-", "-", "-",
            f(c), f(d), "%.2f%%" % (100.0 * d / max(qty, 1))]


def alloc_table(rows, closer=True):
    body = HEADER + rows
    if closer:
        body = body + [["합계", f(sum(int(r[1].replace(",", "")) for r in rows)), "-"]]
    return table(body)


def summary(alloc, apps, qty, amount, final, final_amount):
    return p("3. 청약 및 배정현황") + table([
        [("구 분", 2, 1), ("최초 배정", 1, 2), ("청약 현황", 1, 4), ("최종 배정 현황", 1, 4)],
        ["수량", "비율", "건수", "수량", "금액", "비율", "건수", "수량", "금액", "비율"],
        ["기관투자자", "750,000", "75", "1,000", "750,000", "1", "1", "1,000", "750,000", "1", "75"],
        ["일반투자자", f(alloc), "25", f(apps), f(qty), f(amount), "99", f(apps), f(final), f(final_amount), "25"],
    ])


class ParseReportTest(unittest.TestCase):
    def test_single_brokerage(self):
        # 동원파츠-like: 32,500주 -> 균등 1 (+21 lottery top-ups over 354) + 비례 13.
        xml = doc(p("- 일반투자자 세부 배정현황[삼성증권]"), alloc_table([
            row(15000, 6000, 6300, 36000),
            row(32500, 354, 375, 4602),
        ]))
        rep = dart.parse_report(xml)
        self.assertEqual(len(rep["tables"]), 1)
        self.assertEqual(rep["tables"][0]["broker"], "삼성증권")
        self.assertEqual(rep["apps"], 6354)
        a = dart.allocation_at(rep, "삼성증권", 32500)
        self.assertTrue(a["exact"])
        self.assertEqual(a["shares_prop"], 13.0)
        self.assertAlmostEqual(a["shares_equal"], 375 / 354.0)

    def test_row_that_does_not_add_up_is_rejected(self):
        bad = row(6500, 414, 828, 1799, d=2218)
        rep = dart.parse_report(doc(alloc_table([bad, row(20000, 100, 100, 500)])))
        self.assertEqual([r["unit"] for r in rep["tables"][0]["rows"]], [20000])
        self.assertEqual(rep["tables"][0]["rejected"], 1)

    def test_no_tables(self):
        self.assertEqual(dart.parse_report(doc(p("증권발행실적보고서")))["tables"], [])

    def test_correction_preamble_is_ignored(self):
        # 유라클: a 정정 filing opens with 정정 전/후 copies of its tables, before
        # the cover. Read, they tripled every total.
        t = alloc_table([row(20000, 100, 100, 500)])
        rep = dart.parse_report(doc(t, preamble=p("3. 정정사항") + t + t))
        self.assertEqual(len(rep["tables"]), 1)

    def test_tables_outside_section_four(self):
        # 두산로보틱스: the brokerage tables follow section 5, not section 4.
        rep = dart.parse_report(doc(p("4. 일반투자자 배정방식별 배정현황"),
                                    p("5. 기관투자자 의무보유확약기간별 배정현황"),
                                    p("[미래에셋증권]"), alloc_table([row(20000, 100, 100, 500)])))
        self.assertEqual(rep["tables"][0]["broker"], "미래에셋증권")


class HeadingTest(unittest.TestCase):
    def test_heading_forms_and_aliases(self):
        t = alloc_table([row(20000, 100, 100, 500)])
        for heading, broker in (("1) 대신증권(주)- 일반투자자 세부 배정 현황", "대신증권"),
                                ("2) (주)아이엠증권", "아이엠증권"),
                                ("- (주)BNK투자증권 일반투자자 세부 배정 현황", "BNK투자증권"),
                                ("[케이비증권]", "KB증권"),
                                # 세나테크놀로지: bracket at the end of a 배정방식별 line.
                                ("※ 일반투자자 배정방식별 배정현황[KB증권]", "KB증권")):
            rep = dart.parse_report(doc(p(heading), t))
            self.assertIsNotNone(dart.allocation_at(rep, broker, 20000), heading)

    def test_prose_note_is_not_a_heading(self):
        rep = dart.parse_report(doc(p("주1) 한국투자증권(주)는 일반배정물량 237,500주를 균등배정"),
                                    alloc_table([row(20000, 100, 100, 500)])))
        self.assertIsNone(rep["tables"][0]["broker"])

    def test_caption_table_between_heading_and_table(self):
        # 엠앤씨솔루션: '[삼성증권]', then a '(단위: 주)' table, then the table.
        rep = dart.parse_report(doc(
            p("[KB증권]"), alloc_table([row(20000, 100, 100, 500)]), table([["주1) 당사는 ..."]]),
            p("[삼성증권]"), table([["(단위: 주)"]]), alloc_table([row(5000, 10, 10, 20)])))
        self.assertEqual([t["broker"] for t in rep["tables"]], ["KB증권", "삼성증권"])

    def test_heading_closes_a_table_without_a_closer(self):
        # 시프트업: no 합계 before '[NH투자증권]'; NH's rows were filed under 한투.
        rep = dart.parse_report(doc(
            p("[한국투자증권]"), alloc_table([row(17000, 100, 100, 500)], closer=False),
            p("[NH투자증권]"), alloc_table([row(30000, 50, 50, 900)])))
        self.assertEqual([t["broker"] for t in rep["tables"]], ["한국투자증권", "NH투자증권"])
        self.assertEqual(dart.allocation_at(rep, "NH투자증권", 30000)["shares_prop"], 18.0)


class ColumnTest(unittest.TestCase):
    """Columns are found by their header labels; each variant once broke."""

    def test_three_addends_and_continuation_line(self):
        # 닷밀: 균등 in two groups (B, C), 비례 D, total E=B+C+D; the 20주 row's
        # 비례 is on a second line (313 apps x 1) under ROWSPAN=2 cells.
        header = [
            [("청약단위", 2, 1), ("청약건수", 2, 1), ("청약수량(A)", 2, 1), ("균등배정수량", 1, 6),
             ("비례배정수량", 1, 3), "총배정수량", "배정비율"],
            ["배정건수", "1건당배정수량", "균등배정수량(B)", "배정건수", "1건당배정수량", "균등배정수량ⓒ",
             "배정건수", "건당배정수량", "비례배수량(D)", "(E=B+C+D)", "(E/A)"],
        ]
        r20 = [("20", 2, 1), ("29,598", 2, 1), ("591,960", 2, 1), ("21,685", 2, 1), ("4", 2, 1),
               ("86,740", 2, 1), ("7,913", 2, 1), ("5", 2, 1), ("39,565", 2, 1), "29,285", "0", "0",
               ("126,618", 2, 1), ("21.39", 2, 1)]
        r20b = ["313", "1", "313"]
        r30 = ["30", "542", "16,260", "388", "4", "1,552", "154", "5", "770", "542", "1", "542", "2,864", "17.61"]
        rep = dart.parse_report(doc(table(header + [r20, r20b, r30])))
        rows = rep["tables"][0]["rows"]
        self.assertEqual([(r["unit"], r["eq_total"], r["prop_total"]) for r in rows],
                         [(20, 126305, 313), (30, 2322, 542)])

    def test_sub_lines_that_split_the_main_lines_total(self):
        # 래블업: a unit's second line breaks its 균등 down again (기본 + 추가)
        # in the 균등 column. Summed over both lines it counts twice and the
        # row fails its own total, so the main line alone is tried first.
        main = [("10,000", 2, 1), ("200", 2, 1), ("2,000,000", 2, 1), "200", "1", "-", "-", "260",
                "200", "-", "-", "-", "1,500", ("1,760", 2, 1), ("0.09%", 2, 1)]
        sub = ["60", "1", "-", "-", "60", "-", "-", "-", "-", "-"]
        rep = dart.parse_report(doc(table(HEADER + [main, sub, row(20000, 100, 100, 500)])))
        rows = rep["tables"][0]["rows"]
        self.assertEqual([(r["unit"], r["eq_total"], r["prop_total"]) for r in rows],
                         [(10000, 260, 1500), (20000, 100, 500)])
        self.assertEqual(rep["tables"][0]["rejected"], 0)

    def test_group_decides_what_an_addend_is(self):
        # 데이원: the 비례 total is labelled '균등배정수량(D)' under '비례방식'.
        header = [
            [("청약현황", 1, 3), ("균등방식", 1, 6), ("비례방식", 1, 3), ("총배정수량 (E=B+C+D)", 2, 1),
             ("배정비율 (E/A)", 2, 1)],
            ["청약단위", "청약건수", "청약수량(A)", "배정건수", "1건당 배정수량", "균등배정수량(B)", "배정건수",
             "1건당 배정수량", "균등배정수량(C)", "배정건수", "1건당배정수량", "균등배정수량(D)"],
        ]
        r = ["700", "52", "36,400", "50", "3", "150", "2", "4", "8", "52", "3", "156", "314", "0.01"]
        row0 = dart.parse_report(doc(table(header + [r])))["tables"][0]["rows"][0]
        self.assertEqual((row0["eq_total"], row0["prop_total"]), (158, 156))

    def test_split_equal_and_proportional_tables(self):
        # 큐리옥스: 균등 columns in one table, 비례 and the total in the next,
        # the same 청약단위 rows in both.
        eq = table([
            [("청약현황", 1, 3), ("균등방식", 1, 3)],
            ["청약단위", "청약건수", "청약수량(A)", "배정건수", "1차 균등배정수량(a)", "최종균등배정수량(B=c-d)"],
            ["20", "10,709", "214,180", "10,709", "107,090", "111,240"],
            ["30", "680", "20,400", "680", "7,480", "8,136"],
        ])
        prop = table([
            [("청약현황", 1, 3), ("비례방식", 1, 2), ("총배정수량(D=B+C)", 2, 1), ("배정비율(D/A)", 2, 1)],
            ["청약단위", "청약건수", "청약수량(A)", "1차 비례배정수량(e)", "최종비례배정수량(g=e+f)"],
            ["20", "10,709", "214,180", "-", "-", "111,240", "51.94%"],
            ["30", "680", "20,400", "680", "2,040", "10,176", "49.88%"],
        ])
        rep = dart.parse_report(doc(p("- 일반투자자 세부 배정현황"), eq, prop))
        self.assertEqual(len(rep["tables"]), 1)
        self.assertEqual([(r["eq_total"], r["prop_total"]) for r in rep["tables"][0]["rows"]],
                         [(111240, 0), (8136, 2040)])

    def test_label_breaks_typos_and_late_letters(self):
        # 채비 KB: '청약' / '건수' on two header lines. 비아이매트릭스: '쳥악건수'.
        # 전진건설로봇: '균등배정수량C)'. 엠앤씨 키움: total '(F=C+E)'.
        header = [
            [("청약현황", 1, 3), ("균등방식", 1, 2), ("비례방식", 1, 2), ("총배정수량(F=C+E)", 3, 1),
             ("배정비율", 3, 1)],
            ["청약단위", "쳥악", "청약수량", "1건당배정수량(B)", "균등배정수량C)", "1건당배정수량(D)",
             "비례배정수량(E)"],
            ["", "건수", "(A)", "", "", "", ""],
        ]
        r = ["1,000", "10", "10,000", "1", "10", "3", "30", "40", "0.40%"]
        row0 = dart.parse_report(doc(table(header + [r])))["tables"][0]["rows"][0]
        self.assertEqual((row0["apps"], row0["eq_total"], row0["prop_total"]), (10, 10, 30))

    def test_nh_half_net_quantity(self):
        # 루미르, 유진테크놀로지: NH prints 청약수량 as 단위 x 건수 / 2 - B, the
        # 증거금 basis left after 균등 (rounded down: LGCNS 3,525 x 171).
        r = row(3525, 171, 147, 2394, qty=301240)
        row0 = dart.parse_report(doc(alloc_table([r])))["tables"][0]["rows"][0]
        self.assertEqual((row0["qty"], row0["qty_form"]), (3525 * 171, "half_net"))

    def test_typo_in_quantity_cell(self):
        # 위너스: 50,472,000 printed for 42,000 x 10,187. The row adds up, so it
        # is kept at 단위 x 건수; the summary reconciliation absorbs the rest.
        r = row(42000, 10187, 14678, 122244, qty=50472000)
        row0 = dart.parse_report(doc(alloc_table([r])))["tables"][0]["rows"][0]
        self.assertEqual((row0["qty"], row0["qty_form"]), (427854000, "typo"))


class AllocationTest(unittest.TestCase):
    def rep(self, *rows):
        return dart.parse_report(doc(p("[유진투자증권]"), alloc_table(list(rows))))

    def test_exact_row(self):
        a = dart.allocation_at(self.rep(row(5500, 50, 50, 100)), "유진증권", 5500)   # 38's '유진증권'
        self.assertEqual((a["exact"], a["shares_prop"]), (True, 2.0))
        self.assertIsNone(dart.allocation_at(self.rep(row(5500, 50, 50, 100)), "삼성증권", 5500))

    def test_interpolated_between_two_rows(self):
        # 동인기연: rows at 16,000 and 20,000, none at 38's 18,000.
        rep = self.rep(row(16000, 1, 31, 2559), row(20000, 1, 32, 3201))
        a = dart.allocation_at(rep, "유진", 18000)
        self.assertFalse(a["exact"])
        self.assertEqual(a["interpolated"], (16000, 20000))
        self.assertAlmostEqual(a["shares_prop"], 2880.0)
        self.assertIsNone(dart.allocation_at(rep, "유진", 18000, interpolate=False))

    def test_one_neighbour_is_not_enough(self):
        # A nearby unit is not the same application: 44주 was once read for a
        # 420,000주 "limit" that way (파로스: 38 had copied its share count).
        self.assertIsNone(dart.allocation_at(self.rep(row(5500, 50, 50, 100)), "유진", 6500))

    def test_neighbours_that_disagree(self):
        # A 비례 threshold between the two rows: no straight line through them.
        rep = self.rep(row(2000, 10, 10, 0), row(3000, 10, 10, 60))
        self.assertIsNone(dart.allocation_at(rep, "유진", 2500))


class SummaryTest(unittest.TestCase):
    def summ(self, *values):
        return dart.parse_report(doc(summary(*values)))["summary"]

    def test_columns_by_label(self):
        s = self.summ(400000, 248338, 523112490, 7846687350000, 400000, 6000000000)
        self.assertEqual((s["alloc"], s["apps"], s["qty"], s["final"]), (400000, 248338, 523112490, 400000))

    def test_amount_at_half_price_fixes_a_typo(self):
        # 바이오비쥬: 884,413,080 printed; 금액 = 884,143,080 x 9,100 / 2.
        s = self.summ(750000, 182859, 884413080, 4022851014000, 780000, 7098000000)
        self.assertIn(884143080, dart.summary_qty(s, 9100))

    def test_shifted_row(self):
        # 이지스: 청약수량 under 건수, 배정 under 수량; the 금액 still says which.
        s = self.summ(375000, 204801460, 375000, 3072021900000, 375000, 5625000000)
        self.assertIn(204801460, dart.summary_qty(s, 15000))
        self.assertNotIn(375000, dart.summary_qty(s, 15000))

    def test_copied_column_gives_nothing(self):
        # 엑셀세라퓨틱스: 청약수량 = 최종배정 = 410,173, 금액 computed from it.
        s = self.summ(404500, 212850, 410173, 2050865000, 410173, 4101730000)
        self.assertEqual(dart.summary_qty(s, 10000), [])


class ReconcileTest(unittest.TestCase):
    EC = {"final_price": 10000, "retail_pool": 100000, "total_ratio": 500.0}

    def check(self, qty, summ, apps=1000, allotted=100000, printed=None):
        return net_returns.reconcile({"qty": qty, "qty_printed": qty if printed is None else printed,
                                      "apps": apps, "allotted": allotted, "summary": summ}, self.EC)

    SUMM = {"alloc": 100000, "apps": 1000, "qty": 50000000, "amount": None, "final": 100000}

    def test_tables_match_summary(self):
        self.assertIsNone(self.check(50000000, self.SUMM))
        self.assertIsNone(self.check(45000000, self.SUMM))     # a table unread: that table's rows only

    def test_too_many_rows(self):
        self.assertIn("≠", self.check(60000000, self.SUMM))

    def test_more_allotted_than_final(self):
        self.assertIn(">", self.check(50000000, self.SUMM, allotted=120000))

    def test_summary_counts_nh_at_half_net(self):
        # LGCNS: the filer's own total adds NH's printed (half-net) A.
        self.assertIsNone(self.check(50400000, self.SUMM, printed=50000000))

    def test_copied_column_falls_back_to_counts_and_38(self):
        copied = dict(self.SUMM, qty=100000)
        self.assertIsNone(self.check(50000000, copied))
        self.assertIn("38", self.check(5000000, copied))
        self.assertIn("건수", self.check(50000000, copied, apps=500))


def limits_doc(rows, *notes):
    head = ["구 분", "일반청약자배정물량", "최고청약한도", "청약증거금율"]
    return doc(p("【 일반청약자 배정물량, 최고청약한도 및 청약증거금율 】"), table([head] + rows), *notes)


class ProspectusLimitTest(unittest.TestCase):
    """The 100% 청약한도 per brokerage, from the 투자설명서."""

    def test_tier_table(self):
        # 엠디바이스 (삼성): the table says '주1)'; the tiers are in a table below.
        xml = limits_doc([["삼성증권㈜", "315,000주", "주1)", "50%"]],
                         table([["주1)", "삼성증권㈜의 일반청약자 청약한도는 청약자격별로 상이합니다. 구분", "청약한도율",
                                 "최고 청약한도"], ["우대 청약자격", "200%", "30,000주"],
                                ["일반 청약자격", "100%", "15,000주"], ["온라인 전용 청약자격", "50%", "7,500주"]]))
        lim = dart.parse_limits(xml)["삼성증권"]
        self.assertEqual((lim["base"], lim["tiers"][200]), ((15000, 15000), (30000, 30000)))

    def test_note_split_by_inline_tags_and_two_brokerages(self):
        # 케이엔알: NH's table figure is its 300% tier; DB has one limit for all.
        # Inline tags split '8,600주' from '(100%)'.
        xml = limits_doc([["DB금융투자㈜", "263,000주", "26,300주", "50%"],
                          ["NH투자증권㈜", "263,000주", "25,800주", "50%"]],
                         table([["주)", "DB금융투자㈜의 최고 청약한도는 일반고객 및 우대고객에게 동일하게 적용됩니다."],
                                ["주)", "NH투자증권㈜의 일반청약자 최고청약한도는 <SPAN>8,600주</SPAN> (100%)이나 "
                                        "12,900주 (150%), 25,800주 (300%)까지 청약이 가능합니다."]]))
        lim = dart.parse_limits(xml)
        self.assertEqual(lim["DB금융투자"]["base"], (26300, 26300))
        self.assertEqual(lim["NH투자증권"]["base"], (8600, 8600))

    def test_range_before_pricing(self):
        # 인제니아: before the final size, each figure is a range (~ upsized).
        xml = limits_doc([["삼성증권㈜", "1,250,000DR ~ 1,500,000DR", "주1)", "50%"]],
                         p("주1) 일반 청약자격 100% 60,000DR ~ 75,000DR 온라인 전용 청약자격 50% 30,000DR ~ 37,500DR"))
        lim = dart.parse_limits(xml)["삼성증권"]
        self.assertEqual((lim["pool"], lim["base"]), ((1250000, 1500000), (60000, 75000)))

    def test_base_derived_from_another_tier(self):
        # Tiers are multiples of the base, so 200% halved -- labelled derived.
        lim = dart.parse_limits(limits_doc([["키움증권㈜", "200,000주", "주1)", "50%"]], p("우대 200% 20,000주")))
        self.assertEqual((lim["키움증권"]["base"], lim["키움증권"]["derived"]), ((10000, 10000), True))

    def test_other_wordings(self):
        cases = [
            # 신영: percentage before the amount; the table figure is the 300% tier.
            ("신영증권㈜", "21,000주", "□ 우대(300%)그룹의 청약한도: 21,000주 □ 일반(100%)그룹의 청약한도: 7,000주", 7000),
            # IBK
            ("아이비케이투자증권㈜", "(주1)", "□ 일반고객(100%)의 청약한도 : 15,000주 □ 우대고객(200%)의 청약한도 : 30,000주", 15000),
            # 유안타: channel inside the parentheses.
            ("유안타증권㈜", "주)", "□ 일반A그룹의 청약한도 : 18,000 주 (온라인, 100%), 12,000 주 (오프라인, 70%)", 18000),
            # 하나: no percentage at all, or a sentence.
            ("하나증권㈜", "(주1)", "- 우대고객의 청약한도 : 20,000주 - 일반고객의 청약한도 : 10,000주", 10000),
            ("하나증권㈜", "주1)", "하나증권㈜의 일반청약자 최고청약한도는 20,000주 이나 40,000주 (200%)까지", 20000),
            # 에스오에스랩: tags inside the digits.
            ("한국투자증권㈜", "15, 0 00주", "준우대 등급 고객 최고청약한도는 15, 0 00주 (10 0%)이며, 3 0 , 0 00주 (200%)", 15000),
        ]
        for broker, cell, note, base in cases:
            lim = dart.parse_limits(limits_doc([[broker, "300,000주", cell, "50%"]], p(note)))
            self.assertEqual(list(lim.values())[0]["base"], (base, base), note)

    def test_broker_from_caption_and_plain_limit_header(self):
        # 로킷 SK: the row says '일반청약자'; the caption names the brokerage.
        # 하이투자증권: the header says '청약한도', one limit for every customer.
        xml = doc(p("【SK증권㈜의 일반청약자 배정물량, 최고청약한도 및 청약증거금률】"),
                  table([["구분", "일반청약자 배정물량", "청약한도", "청약증거금률"], ["일반청약자", "351,000주", "33,000주", "50%"]]),
                  p("일반 고객(100%) : 11,000주"))
        self.assertEqual(dart.parse_limits(xml)["SK증권"]["base"], (11000, 11000))


class ProspectusChoiceTest(unittest.TestCase):
    def lim(self, base, top=None, pool=(400000, 480000)):
        return {"pool": pool, "table": None, "base": base, "derived": False,
                "tiers": {100: base, 300: top} if top else {100: base}}

    def report(self, units):
        return {"tables": [{"broker": "NH투자증권", "rows": [{"unit": u, "apps": a} for u, a in units.items()]}]}

    def test_ranged_end_chosen_by_rows(self):
        # 씨케이솔루션: 12,355 applications at the high end's 15,000.
        lim = {"NH투자증권": self.lim((12000, 15000), (36000, 45000))}
        got = dart.prospectus_limit(lim, 400000, report=self.report({12000: 1055, 15000: 12355, 36000: 1152}))
        self.assertEqual((got["limit"], got["end"]), (15000, "rows"))
        got = dart.prospectus_limit(lim, 400000, report=self.report({12000: 2217, 15000: 292, 36000: 11327}))
        self.assertEqual(got["limit"], 12000)

    def test_no_rows_falls_back_to_allotment(self):
        lim = {"NH투자증권": self.lim((12000, 15000))}
        self.assertEqual(dart.prospectus_limit(lim, 400000, report=self.report({}))["limit"], 12000)
        self.assertEqual(dart.prospectus_limit(lim, 480000, report=self.report({}))["limit"], 15000)

    def test_best_brokerage_and_unread_ones(self):
        lim = {"NH투자증권": self.lim((8600, 8600)), "DB금융투자": self.lim((26300, 26300))}
        self.assertEqual(dart.prospectus_limit(lim, None)["broker"], "DB금융투자")
        # A brokerage left unread may be the best one: only 38's is trusted then.
        lim["DB금융투자"]["base"] = None
        self.assertIsNone(dart.prospectus_limit(lim, None, prefer="DB금융투자"))
        self.assertEqual(dart.prospectus_limit(lim, None, prefer="NH투자증권")["limit"], 8600)


class NameTest(unittest.TestCase):
    def test_norm_name(self):
        self.assertEqual(dart.norm_name("디티에스(구.동양티에스)"), "디티에스")
        self.assertEqual(dart.norm_name("산일전기 (유가)"), "산일전기")
        self.assertEqual(dart.norm_name("㈜동원파츠"), "동원파츠")


if __name__ == "__main__":
    unittest.main()
