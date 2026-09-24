"""API tests for the read-only advancement threshold check endpoint."""
import json as jsonlib
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_threshold_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "threshold-session"

# 单一评委 → 最终均分 == 原始分，便于手算断言。
# C1: I1=8, I2=6（总分 14）；C2: I1=5, I2=9（总分 14）；C3: 仅 I1=7（缺 I2）。
CSV_BODY = (
    "选手,评委,项目,分数\n"
    "C1,J,I1,8.0\nC2,J,I1,5.0\nC3,J,I1,7.0\n"
    "C1,J,I2,6.0\nC2,J,I2,9.0\n"
)


class TestThresholdCheckAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client.delete(f"/api/sessions/{SID}")
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("t.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        assert r.status_code == 200, r.text

    def test_01_basic_check(self):
        r = client.post(
            f"/api/sessions/{SID}/threshold-check",
            json={"item_thresholds": {"I1": 5.0, "I2": 6.0}, "total_threshold": 14.0},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["scope"], "judge")
        self.assertEqual(data["total_threshold"], 14.0)
        self.assertEqual(
            data["item_thresholds"],
            [{"item": "I1", "threshold": 5.0}, {"item": "I2", "threshold": 6.0}],
        )
        rows = {c["contestant"]: c for c in data["contestants"]}
        self.assertEqual(rows["C1"]["status"], "pass")
        self.assertEqual(rows["C2"]["status"], "pass")
        self.assertEqual(rows["C3"]["status"], "insufficient")
        self.assertEqual(rows["C3"]["missing_items"], ["I2"])
        self.assertEqual(
            data["summary"], {"contestants": 3, "pass": 2, "fail": 0, "insufficient": 1}
        )
        # 每行都带各门槛项目最终均分、最终总分与未达标原因字段。
        for c in data["contestants"]:
            self.assertEqual(len(c["items"]), 2)
            self.assertIn("eff_total", c)
            self.assertIn("eff_rank", c)
            self.assertIn("reasons", c)
        self.assertEqual(data["status_labels"]["insufficient"], "数据不足")

    def test_02_fail_reasons_and_no_zero_fill(self):
        r = client.post(
            f"/api/sessions/{SID}/threshold-check",
            json={"item_thresholds": {"I2": 0.0}, "total_threshold": 15.0},
        )
        self.assertEqual(r.status_code, 200, r.text)
        rows = {c["contestant"]: c for c in r.json()["contestants"]}
        # C3 缺 I2：即使门槛为 0 也标数据不足，绝不按 0 分计。
        self.assertEqual(rows["C3"]["status"], "insufficient")
        self.assertIn("不按 0 分", rows["C3"]["reasons"][0])
        # C1/C2 总分 14 < 15 → 未达标且原因提及总分线。
        self.assertEqual(rows["C1"]["status"], "fail")
        self.assertTrue(any("总分线" in x for x in rows["C1"]["reasons"]))
        self.assertEqual(rows["C2"]["status"], "fail")

    def test_03_readonly_does_not_mutate_session(self):
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        client.post(
            f"/api/sessions/{SID}/threshold-check",
            json={"item_thresholds": {"I1": 1.0}, "total_threshold": 1.0},
        )
        client.post(f"/api/sessions/{SID}/threshold-check", json={"item_thresholds": {"I2": 100.0}})
        after = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(
            [(b["contestant"], b["eff_total"]) for b in before["contestants"]],
            [(b["contestant"], b["eff_total"]) for b in after["contestants"]],
        )
        self.assertEqual(after["counts"]["decisions"], 0)

    def test_04_reflects_decisions(self):
        # 载入内置示例（存在校准分≠原分的评分），翻转一条裁决后检查结果随之变化。
        client.post(f"/api/sessions/{SID}/sample?scope=judge")
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        item = analysis["items"][0]
        line = 7.0
        before = client.post(
            f"/api/sessions/{SID}/threshold-check",
            json={"item_thresholds": {item: line}},
        ).json()
        target = next(
            s for b in analysis["contestants"] for ib in b["items"] for s in ib["scores"]
            if abs(s["calibrated"] - s["raw"]) > 1e-9
        )
        r = client.patch(
            f"/api/sessions/{SID}/scores/{target['id']}/decision",
            json={"use_raw": True, "reason": "门槛检查应反映裁决"},
        )
        self.assertEqual(r.status_code, 200)
        after = client.post(
            f"/api/sessions/{SID}/threshold-check",
            json={"item_thresholds": {item: line}},
        ).json()

        def snapshot(payload):
            return {
                c["contestant"]: (round(c["eff_total"], 9), c["status"])
                for c in payload["contestants"]
            }

        self.assertNotEqual(snapshot(before), snapshot(after))
        client.post(f"/api/sessions/{SID}/decisions/reset")

    def test_05_reflects_scope_switch(self):
        client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("t.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        r1 = client.post(
            f"/api/sessions/{SID}/threshold-check", json={"item_thresholds": {"I1": 5.0}}
        ).json()
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        r2 = client.post(
            f"/api/sessions/{SID}/threshold-check", json={"item_thresholds": {"I1": 5.0}}
        ).json()
        self.assertEqual(r1["scope"], "judge")
        self.assertEqual(r2["scope"], "judge_item")
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})

    def test_06_validation_errors(self):
        # 缺少 item_thresholds / 空对象 → 422（pydantic 校验）。
        r = client.post(f"/api/sessions/{SID}/threshold-check", json={})
        self.assertEqual(r.status_code, 422)
        r = client.post(f"/api/sessions/{SID}/threshold-check", json={"item_thresholds": {}})
        self.assertEqual(r.status_code, 422)
        # item_thresholds 类型错误 → 422。
        r = client.post(
            f"/api/sessions/{SID}/threshold-check", json={"item_thresholds": ["I1"]}
        )
        self.assertEqual(r.status_code, 422)
        # 未知项目 → 400，错误信息列出可选项目。
        r = client.post(
            f"/api/sessions/{SID}/threshold-check",
            json={"item_thresholds": {"不存在的项目": 1.0}},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("项目", r.json()["detail"])
        # 门槛为 NaN / 无穷 → 400（有限数字校验）。httpx 拒绝序列化非常量浮点，
        # 改为发送原始 JSON 文本（NaN/Infinity 是 Python json 模块的合法扩展）。
        r = client.post(
            f"/api/sessions/{SID}/threshold-check",
            content=jsonlib.dumps({"item_thresholds": {"I1": float("nan")}}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)
        r = client.post(
            f"/api/sessions/{SID}/threshold-check",
            content=jsonlib.dumps(
                {"item_thresholds": {"I1": 1.0}, "total_threshold": float("inf")}
            ),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)
        # 未知会话 → 404。
        r = client.post(
            "/api/sessions/nope/threshold-check", json={"item_thresholds": {"I1": 1.0}}
        )
        self.assertEqual(r.status_code, 404)

    def test_07_empty_session_returns_404(self):
        client.delete("/api/sessions/empty-th")
        r = client.post(
            "/api/sessions/empty-th/threshold-check", json={"item_thresholds": {"I1": 1.0}}
        )
        self.assertEqual(r.status_code, 404)
        # 会话已创建但没有任何评分 → 同样 404。
        client.post("/api/sessions/empty-th/scope", json={"scope": "judge"})
        r = client.post(
            "/api/sessions/empty-th/threshold-check", json={"item_thresholds": {"I1": 1.0}}
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
