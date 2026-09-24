"""晋级门槛检查（只读纯函数）。

在当前校准范围与逐项裁决形成的**最终项目均分 / 最终总分**基础上，检查每名选手
是否达到用户设定的晋级门槛：

- 为一个或多个项目设定「最终项目均分最低线」，并可选择性设定「最终总分线」；
- 缺少任一被设门槛项目评分的选手标记为「数据不足」并列出缺失项目，
  缺失项目**不按 0 分**参与判断；
- 结果逐人给出各门槛项目的最终均分、最终总分与未达标原因。

整个计算只读，不触碰评分、裁决或会话；评分、裁决或校准范围变化后由调用方
重新计算。
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from .scoring import SCOPE_LABELS, TIE_EPS

# 检查结果的三类状态。
STATUS_PASS = "pass"                # 达标：全部门槛均满足
STATUS_FAIL = "fail"                # 未达标：数据齐全但至少一条门槛不满足
STATUS_INSUFFICIENT = "insufficient"  # 数据不足：缺少任一被设门槛项目的评分
STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_INSUFFICIENT)
STATUS_LABELS = {
    STATUS_PASS: "达标",
    STATUS_FAIL: "未达标",
    STATUS_INSUFFICIENT: "数据不足",
}


class ThresholdValidationError(ValueError):
    """门槛参数不合法（未设项目门槛、项目不存在、门槛不是有限数字等）。"""


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def threshold_check(
    analysis: Mapping[str, Any],
    item_thresholds: Mapping[str, Any],
    total_threshold: Any = None,
) -> dict[str, Any]:
    """检查每名选手是否达到晋级门槛。

    ``analysis`` 为 :func:`app.scoring.analyze` 的输出（只使用其中的
    ``scope``、``items`` 与每位选手的 ``eff_total`` / 逐项目 ``eff_avg`` /
    ``eff_rank``）。``item_thresholds`` 为「项目名 → 最终项目均分最低线」，
    至少包含一个项目；``total_threshold`` 为可选的最终总分线（None 表示不设）。
    """
    if not isinstance(item_thresholds, Mapping) or not item_thresholds:
        raise ThresholdValidationError("请至少为一个项目设置最终均分门槛。")

    items = list(analysis["items"])
    normalized: dict[str, float] = {}
    for raw_item, raw_value in item_thresholds.items():
        item = raw_item.strip() if isinstance(raw_item, str) else ""
        if not item:
            raise ThresholdValidationError("门槛项目的名称不能为空。")
        if item not in items:
            avail = "、".join(items) if items else "（当前没有项目）"
            raise ThresholdValidationError(f"项目「{item}」不存在，可选：{avail}。")
        if not _is_finite_number(raw_value):
            raise ThresholdValidationError(f"项目「{item}」的门槛必须是有限数字。")
        if item in normalized:
            raise ThresholdValidationError(f"项目「{item}」的门槛被重复设置。")
        normalized[item] = float(raw_value)

    if total_threshold is None:
        total_line = None
    elif _is_finite_number(total_threshold):
        total_line = float(total_threshold)
    else:
        raise ThresholdValidationError("最终总分线必须是有限数字。")

    # 门槛项目按会话项目顺序输出，保证结果稳定、与排名页展示一致。
    ordered_items = [it for it in items if it in normalized]
    threshold_list = [{"item": it, "threshold": normalized[it]} for it in ordered_items]

    contestants: list[dict[str, Any]] = []
    counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_INSUFFICIENT: 0}
    for block in analysis["contestants"]:  # 已按最终名次排序
        name = block["contestant"]
        item_map = {ib["item"]: ib for ib in block["items"]}
        item_rows: list[dict[str, Any]] = []
        missing: list[str] = []
        unmet_items: list[tuple[str, float, float]] = []
        for it in ordered_items:
            line = normalized[it]
            ib = item_map.get(it)
            if ib is None:
                # 缺失项目不按 0 分计：标记缺失，整体判为「数据不足」。
                missing.append(it)
                item_rows.append(
                    {"item": it, "threshold": line, "eff_avg": None, "met": None}
                )
                continue
            avg = float(ib["eff_avg"])
            met = avg >= line - TIE_EPS
            if not met:
                unmet_items.append((it, avg, line))
            item_rows.append(
                {"item": it, "threshold": line, "eff_avg": avg, "met": met}
            )

        eff_total = float(block["eff_total"])
        total_met = None if total_line is None else eff_total >= total_line - TIE_EPS

        reasons: list[str] = []
        if missing:
            status = STATUS_INSUFFICIENT
            reasons.append(
                "缺少项目"
                + "、".join(f"「{m}」" for m in missing)
                + "的评分数据（缺失项目不按 0 分计），无法判定是否达标"
            )
        else:
            for it, avg, line in unmet_items:
                reasons.append(f"项目「{it}」最终均分 {avg:.2f} 低于门槛 {line:.2f}")
            if total_met is False:
                reasons.append(f"最终总分 {eff_total:.2f} 低于总分线 {total_line:.2f}")
            status = STATUS_PASS if not reasons else STATUS_FAIL
        counts[status] += 1
        contestants.append(
            {
                "contestant": name,
                "status": status,
                "eff_rank": block["eff_rank"],
                "eff_total": eff_total,
                "items": item_rows,
                "missing_items": missing,
                "total_met": total_met,
                "reasons": reasons,
            }
        )

    scope = analysis.get("scope", "")
    return {
        "scope": scope,
        "scope_label": analysis.get("scope_label") or SCOPE_LABELS.get(scope, scope),
        "item_thresholds": threshold_list,
        "total_threshold": total_line,
        "status_labels": dict(STATUS_LABELS),
        "summary": {
            "contestants": len(contestants),
            "pass": counts[STATUS_PASS],
            "fail": counts[STATUS_FAIL],
            "insufficient": counts[STATUS_INSUFFICIENT],
        },
        "contestants": contestants,
    }
