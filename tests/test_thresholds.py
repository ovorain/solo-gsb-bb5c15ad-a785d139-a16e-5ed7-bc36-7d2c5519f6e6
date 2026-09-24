"""Tests for the advancement threshold check (read-only pure transform)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scoring import analyze
from app.thresholds import (
    STATUS_FAIL,
    STATUS_INSUFFICIENT,
    STATUS_LABELS,
    STATUS_PASS,
    ThresholdValidationError,
    threshold_check,
)


def make_records(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows)
    ]


# 单一评委 → 最终均分 == 原始分，便于手算断言。
# C1: I1=8, I2=6（总分 14）；C2: I1=5, I2=9（总分 14）；C3: 仅 I1=7（总分 7）。
BASE = [
    ("C1", "J", "I1", 8.0), ("C2", "J", "I1", 5.0), ("C3", "J", "I1", 7.0),
    ("C1", "J", "I2", 6.0), ("C2", "J", "I2", 9.0),
]


def run(item_thresholds, total=None, rows=BASE, decisions=None, scope="judge"):
    analysis = analyze(make_records(rows), decisions or {}, scope)
    return threshold_check(analysis, item_thresholds, total)


def by_name(result):
    return {c["contestant"]: c for c in result["contestants"]}


class TestThresholdCheck(unittest.TestCase):
    def test_pass_fail_and_insufficient(self):
        r = run({"I1": 5.0, "I2": 6.0})
        rows = by_name(r)
        # C1：8≥5、6≥6 → 达标；C2：5≥5、9≥6 → 达标；C3：缺 I2 → 数据不足。
        self.assertEqual(rows["C1"]["status"], STATUS_PASS)
        self.assertEqual(rows["C2"]["status"], STATUS_PASS)
        self.assertEqual(rows["C3"]["status"], STATUS_INSUFFICIENT)
        self.assertEqual(rows["C3"]["missing_items"], ["I2"])
        self.assertEqual(r["summary"], {"contestants": 3, "pass": 2, "fail": 0, "insufficient": 1})
        # 未设总分线时 total_met 为 None，total_threshold 回显为 None。
        self.assertIsNone(r["total_threshold"])
        for c in r["contestants"]:
            self.assertIsNone(c["total_met"])

    def test_fail_reasons_for_items_and_total(self):
        r = run({"I1": 6.0, "I2": 7.0}, total=15.0)
        rows = by_name(r)
        # C1：I2 6<7 且总分 14<15 → 两条未达标原因。
        self.assertEqual(rows["C1"]["status"], STATUS_FAIL)
        self.assertEqual(len(rows["C1"]["reasons"]), 2)
        self.assertIn("「I2」", rows["C1"]["reasons"][0])
        self.assertIn("低于门槛", rows["C1"]["reasons"][0])
        self.assertIn("低于总分线", rows["C1"]["reasons"][1])
        # C2：I1 5<6 且总分 14<15 → 未达标。
        self.assertEqual(rows["C2"]["status"], STATUS_FAIL)
        self.assertTrue(any("「I1」" in x for x in rows["C2"]["reasons"]))
        # C3：缺 I2 → 数据不足（即使总分线也不满足，也只列缺失原因）。
        self.assertEqual(rows["C3"]["status"], STATUS_INSUFFICIENT)
        self.assertEqual(len(rows["C3"]["reasons"]), 1)
        self.assertIn("缺少项目", rows["C3"]["reasons"][0])
        self.assertIn("「I2」", rows["C3"]["reasons"][0])
        self.assertEqual(r["summary"]["fail"], 2)
        self.assertEqual(r["summary"]["insufficient"], 1)

    def test_missing_item_is_not_treated_as_zero(self):
        # 门槛 0 分：若缺失项目按 0 分计，0≥0 会判达标；必须判为数据不足。
        r = run({"I2": 0.0})
        rows = by_name(r)
        self.assertEqual(rows["C3"]["status"], STATUS_INSUFFICIENT)
        self.assertEqual(rows["C3"]["missing_items"], ["I2"])
        item_row = next(x for x in rows["C3"]["items"] if x["item"] == "I2")
        self.assertIsNone(item_row["eff_avg"])
        self.assertIsNone(item_row["met"])
        # 数据齐全的选手不受缺失逻辑影响。
        self.assertEqual(rows["C1"]["status"], STATUS_PASS)
        self.assertEqual(rows["C2"]["status"], STATUS_PASS)

    def test_total_threshold_optional(self):
        r = run({"I1": 5.0}, total=15.0)
        rows = by_name(r)
        # C1/C2 总分 14 < 15 → 未达标；C3 缺 I2 但 I1 有数据 → 数据不足。
        self.assertEqual(rows["C1"]["status"], STATUS_FAIL)
        self.assertFalse(rows["C1"]["total_met"])
        self.assertTrue(any("总分线" in x for x in rows["C1"]["reasons"]))
        r2 = run({"I1": 5.0}, total=14.0)
        rows2 = by_name(r2)
        self.assertEqual(rows2["C1"]["status"], STATUS_PASS)
        self.assertTrue(rows2["C1"]["total_met"])

    def test_threshold_boundary_is_inclusive(self):
        # 均分恰好等于门槛 → 达标（闭区间）。
        r = run({"I1": 8.0, "I2": 6.0})
        self.assertEqual(by_name(r)["C1"]["status"], STATUS_PASS)

    def test_item_thresholds_follow_session_item_order(self):
        # 传入顺序与会话项目顺序无关，输出按会话项目顺序排列。
        r = run({"I2": 6.0, "I1": 5.0})
        self.assertEqual([t["item"] for t in r["item_thresholds"]], ["I1", "I2"])
        for c in r["contestants"]:
            self.assertEqual([x["item"] for x in c["items"]], ["I1", "I2"])

    def test_output_structure_and_labels(self):
        r = run({"I1": 5.0})
        self.assertEqual(r["scope"], "judge")
        self.assertIn("scope_label", r)
        self.assertEqual(set(STATUS_LABELS), {"pass", "fail", "insufficient"})
        self.assertEqual(r["status_labels"], STATUS_LABELS)
        # 选手按最终名次排列，且带回最终名次与最终总分。
        ranks = [c["eff_rank"] for c in r["contestants"]]
        self.assertEqual(ranks, sorted(ranks))
        for c in r["contestants"]:
            self.assertIsInstance(c["eff_total"], float)
            self.assertIsInstance(c["eff_rank"], int)

    def test_reflects_decisions(self):
        # 两位评委使校准分 ≠ 原分；翻转裁决后最终均分改变 → 达标状态随之翻转。
        rows = [
            ("C1", "J1", "I1", 10.0), ("C1", "J2", "I1", 6.0),
            ("C2", "J1", "I1", 7.0), ("C2", "J2", "I1", 7.0),
        ]
        a0 = analyze(make_records(rows), {}, "judge")
        eff_c1 = next(
            ib["eff_avg"] for b in a0["contestants"] if b["contestant"] == "C1"
            for ib in b["items"] if ib["item"] == "I1"
        )
        line = (eff_c1 + 8.0) / 2.0  # 介于校准均分与「保留原分」均分之间的门槛
        before = threshold_check(a0, {"I1": line})
        # 把 C1 的两条评分都裁为保留原分 → 最终均分变为 8.0。
        ids = [
            s["id"] for b in a0["contestants"] if b["contestant"] == "C1"
            for ib in b["items"] for s in ib["scores"]
        ]
        a1 = analyze(make_records(rows), {i: (True, "保留") for i in ids}, "judge")
        after = threshold_check(a1, {"I1": line})
        self.assertNotEqual(
            by_name(before)["C1"]["status"], by_name(after)["C1"]["status"]
        )
        self.assertEqual(by_name(after)["C1"]["status"], STATUS_PASS)

    def test_reflects_scope_switch(self):
        # 校准范围变化会改变最终均分，结果随之变化。
        rows = [
            ("C1", "J1", "I1", 10.0), ("C1", "J2", "I1", 6.0),
            ("C2", "J1", "I1", 7.0), ("C2", "J2", "I1", 7.0),
            ("C1", "J1", "I2", 9.0), ("C1", "J2", "I2", 9.0),
            ("C2", "J1", "I2", 8.0), ("C2", "J2", "I2", 4.0),
        ]
        a_j = analyze(make_records(rows), {}, "judge")
        a_ji = analyze(make_records(rows), {}, "judge_item")
        eff_j = {
            b["contestant"]: next(ib["eff_avg"] for ib in b["items"] if ib["item"] == "I1")
            for b in a_j["contestants"]
        }
        eff_ji = {
            b["contestant"]: next(ib["eff_avg"] for ib in b["items"] if ib["item"] == "I1")
            for b in a_ji["contestants"]
        }
        self.assertNotEqual(eff_j, eff_ji)
        line = (eff_j["C1"] + eff_ji["C1"]) / 2.0
        r_j = threshold_check(a_j, {"I1": line})
        r_ji = threshold_check(a_ji, {"I1": line})
        self.assertEqual(r_j["scope"], "judge")
        self.assertEqual(r_ji["scope"], "judge_item")
        self.assertNotEqual(
            by_name(r_j)["C1"]["status"], by_name(r_ji)["C1"]["status"]
        )

    def test_validation_errors(self):
        analysis = analyze(make_records(BASE), {}, "judge")
        # 未设置任何项目门槛。
        with self.assertRaises(ThresholdValidationError):
            threshold_check(analysis, {})
        with self.assertRaises(ThresholdValidationError):
            threshold_check(analysis, None)
        # 项目不存在。
        with self.assertRaises(ThresholdValidationError):
            threshold_check(analysis, {"不存在": 1.0})
        # 空白项目名。
        with self.assertRaises(ThresholdValidationError):
            threshold_check(analysis, {"   ": 1.0})
        # 门槛不是有限数字。
        for bad in (float("nan"), float("inf"), True, "7", None):
            with self.assertRaises(ThresholdValidationError, msg=f"value={bad!r}"):
                threshold_check(analysis, {"I1": bad})
        # 总分线不是有限数字。
        for bad in (float("nan"), float("-inf"), "20", True):
            with self.assertRaises(ThresholdValidationError, msg=f"total={bad!r}"):
                threshold_check(analysis, {"I1": 1.0}, bad)
        # 项目名去空白后重复。
        with self.assertRaises(ThresholdValidationError):
            threshold_check(analysis, {"I1": 1.0, " I1 ": 2.0})

    def test_item_names_are_stripped(self):
        r = run({" I1 ": 5.0})
        self.assertEqual([t["item"] for t in r["item_thresholds"]], ["I1"])
        self.assertEqual(r["item_thresholds"][0]["threshold"], 5.0)


if __name__ == "__main__":
    unittest.main()
