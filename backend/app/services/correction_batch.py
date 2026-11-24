"""样本更正批次领域服务：值班员一次纠正多个地区的对照/实验观测。

业务流程：
1. 提交更正批次（一次提交多条更正条目，按回执号幂等，重复回执只更正一次）；
2. 样本核算：逐条关联地区、分组和证明材料，得出 已核清/待补证/已拒绝（含失败原因）；
3. 批量重算：只用已核清条目重算受影响指标，待补证观测不得进入重算，
   未受影响的指标沿用原统计，已发旧结论按历史口径重算并生成替代结论
   （列出旧报告号和用于复算的原观测）；
4. 发布：运营从发布清单决定替换哪些地区结论，同一回执并发提交只能发布一次。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 更正条目的核算状态
ENTRY_PENDING = "待核算"
ENTRY_VERIFIED = "已核清"
ENTRY_WAITING_PROOF = "待补证"
ENTRY_REJECTED = "已拒绝"

# 替代结论的发布状态
CONCLUSION_READY = "待发布"
CONCLUSION_PUBLISHED = "已发布"
CONCLUSION_HELD = "暂缓替换"

# 批次状态
BATCH_PENDING, BATCH_VERIFIED, BATCH_RECALCED, BATCH_PUBLISHED = (
    "待核算", "已核算", "已重算", "已发布",
)


class CorrectionBatchService(BaseModuleService):
    MODULE = "correction_batch"
    ENTITY = "样本更正批次"
    OPERATION_TERM = "更正"
    LIST_FIELDS = [
        "批次编号", "回执号", "值班员", "更正条目数",
        "已核清", "待补证", "已拒绝", "受影响指标", "提交时间",
    ]
    REQUIRED_FIELDS = ["批次编号", "回执号", "值班员"]
    STATUS_ORDER = [BATCH_PENDING, BATCH_VERIFIED, BATCH_RECALCED, BATCH_PUBLISHED]
    ACTION_RULES = {"样本核算": BATCH_VERIFIED, "批量重算": BATCH_RECALCED, "发布结论": BATCH_PUBLISHED}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        # 提交与发布各用一把锁：保证同一回执并发时只建一批、只发一次
        self._submit_lock = threading.Lock()
        self._publish_lock = threading.Lock()

    # ----------------------------------------------------------
    # 表访问辅助
    # ----------------------------------------------------------
    @staticmethod
    def _observations() -> list[dict[str, Any]]:
        return store.rows("experiment_observation")

    @staticmethod
    def _reports() -> list[dict[str, Any]]:
        return store.rows("published_report")

    @staticmethod
    def _entries() -> list[dict[str, Any]]:
        return store.rows("correction_entry")

    @staticmethod
    def _conclusions() -> list[dict[str, Any]]:
        return store.rows("replacement_conclusion")

    @staticmethod
    def _find_observation(obs_no: str) -> dict[str, Any] | None:
        for obs in store.rows("experiment_observation"):
            if obs.get("观测编号") == obs_no:
                return obs
        return None

    @staticmethod
    def _find_report(region: str, metric: str) -> dict[str, Any] | None:
        """取该地区该指标当前生效（已发布）的旧结论。"""
        for report in store.rows("published_report"):
            if (report.get("地区") == region and report.get("指标名称") == metric
                    and report.get("状态") == "已发布"):
                return report
        return None

    def batch_entries(self, batch_id: int) -> list[dict[str, Any]]:
        return [row for row in self._entries() if int(row.get("批次id", 0)) == batch_id]

    def batch_conclusions(self, batch_id: int) -> list[dict[str, Any]]:
        return [row for row in self._conclusions() if int(row.get("批次id", 0)) == batch_id]

    # ----------------------------------------------------------
    # 1. 提交批次（回执号幂等：重复回执只更正一次）
    # ----------------------------------------------------------
    def submit_batch(
        self,
        *,
        receipt_no: str,
        operator: str,
        entries: list[dict[str, Any]],
        submit_time: str = "",
    ) -> tuple[dict[str, Any], bool, str]:
        """值班员一次提交多个地区的对照/实验观测更正。

        Returns:
            (批次, 是否新建, 提示信息)。同一回执号重复提交时返回已有批次，
            不重复落条目 —— 重复回执只更正一次。
        """
        receipt_no = str(receipt_no or "").strip()
        if not receipt_no:
            return {}, False, "缺少回执号，无法登记更正批次"
        if not entries:
            return {}, False, "更正批次至少需要一条更正条目"
        with self._submit_lock:
            for batch in store.rows(self.MODULE):
                if batch.get("回执号") == receipt_no:
                    return batch, False, f"回执{receipt_no}已受理过，重复回执只更正一次"

            batches = store.rows(self.MODULE)
            batch_id = max((int(row.get("id", 0)) for row in batches), default=0) + 1
            batch = {
                "id": batch_id,
                "批次编号": f"CORR-{batch_id:04d}",
                "回执号": receipt_no,
                "值班员": operator,
                "更正条目数": len(entries),
                "已核清": 0,
                "待补证": 0,
                "已拒绝": 0,
                "受影响指标": "",
                "提交时间": submit_time or "2026-11-24",
                "status": BATCH_PENDING,
                "pending": True,
                "abnormal": False,
            }
            batches.append(batch)

            entry_rows = self._entries()
            next_entry_id = max((int(row.get("id", 0)) for row in entry_rows), default=0)
            for item in entries:
                next_entry_id += 1
                obs_no = str(item.get("观测编号") or "").strip()
                obs = self._find_observation(obs_no)
                entry_rows.append({
                    "id": next_entry_id,
                    "批次id": batch_id,
                    "批次编号": batch["批次编号"],
                    "回执号": receipt_no,
                    "地区": str(item.get("地区") or "").strip(),
                    "分组": str(item.get("分组") or "").strip(),
                    "观测编号": obs_no,
                    "原观测值": obs.get("观测值") if obs else "",
                    "新观测值": item.get("新观测值"),
                    "证明材料": str(item.get("证明材料") or "").strip(),
                    "核算状态": ENTRY_PENDING,
                    "失败原因": "",
                })
        return batch, True, f"{self.ENTITY}{batch['批次编号']}已登记，共{len(entries)}条更正待核算"

    # ----------------------------------------------------------
    # 2. 样本核算：逐条关联地区、分组和证明材料
    # ----------------------------------------------------------
    def verify_entries(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        batch = self.get_entry(batch_id)
        if batch is None:
            return None, f"{self.ENTITY} {batch_id} 不存在或已归档"
        if batch.get("status") == BATCH_PUBLISHED:
            return batch, f"批次{batch['批次编号']}已发布，不能再核算"
        entries = self.batch_entries(batch_id)
        if not entries:
            return batch, f"批次{batch['批次编号']}没有更正条目"

        for entry in entries:
            if entry.get("核算状态") == ENTRY_VERIFIED:
                continue  # 已核清的条目不重复核算
            obs = self._find_observation(entry.get("观测编号", ""))
            if obs is None:
                entry["核算状态"] = ENTRY_REJECTED
                entry["失败原因"] = f"观测{entry.get('观测编号')}不存在，无法更正"
            elif obs.get("地区") != entry.get("地区") or obs.get("分组") != entry.get("分组"):
                entry["核算状态"] = ENTRY_REJECTED
                entry["失败原因"] = (
                    f"观测{obs['观测编号']}属{obs['地区']}{obs['分组']}，"
                    f"与条目登记的{entry.get('地区')}{entry.get('分组')}不符"
                )
            elif not str(entry.get("证明材料") or "").strip():
                entry["核算状态"] = ENTRY_WAITING_PROOF
                entry["失败原因"] = "证明材料缺失，等待原始观测补证"
            else:
                entry["核算状态"] = ENTRY_VERIFIED
                entry["失败原因"] = ""
                entry["原观测值"] = obs.get("观测值")

        self._refresh_counts(batch)
        batch["status"] = BATCH_VERIFIED
        batch["pending"] = True
        return batch, (
            f"样本核算完成：已核清{batch['已核清']}条、"
            f"待补证{batch['待补证']}条、已拒绝{batch['已拒绝']}条"
        )

    def supplement_proof(self, entry_id: int, proof: str) -> tuple[dict[str, Any] | None, str]:
        """补录证明材料：补证后条目可重新参加样本核算。"""
        proof = str(proof or "").strip()
        if not proof:
            return None, "证明材料不能为空"
        for entry in self._entries():
            if int(entry.get("id", 0)) == entry_id:
                entry["证明材料"] = proof
                if entry.get("核算状态") == ENTRY_WAITING_PROOF:
                    entry["核算状态"] = ENTRY_PENDING
                    entry["失败原因"] = ""
                return entry, f"条目{entry_id}已补录证明材料，可重新核算"
        return None, f"更正条目 {entry_id} 不存在"

    # ----------------------------------------------------------
    # 3. 批量重算：只采用已核清条目，生成替代结论
    # ----------------------------------------------------------
    def recalculate(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        batch = self.get_entry(batch_id)
        if batch is None:
            return None, f"{self.ENTITY} {batch_id} 不存在或已归档"
        if batch.get("status") == BATCH_PUBLISHED:
            return batch, f"批次{batch['批次编号']}已发布，不能再重算"
        if batch.get("status") == BATCH_PENDING:
            return batch, f"批次{batch['批次编号']}尚未样本核算，不能重算"
        entries = self.batch_entries(batch_id)
        if not entries:
            return batch, f"批次{batch['批次编号']}没有更正条目"

        # 只有已核清的更正进入重算；待补证、已拒绝一律不采用
        verified = [row for row in entries if row.get("核算状态") == ENTRY_VERIFIED]
        corrections = {row["观测编号"]: float(row["新观测值"]) for row in verified}
        pending_regions = {
            row.get("地区") for row in entries if row.get("核算状态") == ENTRY_WAITING_PROOF
        }

        # 受影响地区/指标：由能定位到观测的条目决定；待补证条目也要列出结论（暂缓）
        affected: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in entries:
            obs = self._find_observation(row.get("观测编号", ""))
            if obs is None:
                continue
            key = (obs.get("地区", ""), obs.get("指标名称", ""))
            affected.setdefault(key, []).append(row)

        # 重算前清掉本批次上次生成的结论，保证重算可重复执行且结果一致
        remaining = [row for row in self._conclusions() if int(row.get("批次id", 0)) != batch_id]
        store.rows("replacement_conclusion")[:] = remaining

        next_id = max((int(row.get("id", 0)) for row in self._conclusions()), default=0)
        affected_labels: list[str] = []
        for (region, metric) in sorted(affected):
            report = self._find_report(region, metric)
            if report is None:
                continue  # 没有已发旧结论的指标不需要替代
            observations = [
                obs for obs in self._observations()
                if obs.get("地区") == region and obs.get("指标名称") == metric
            ]
            control_values: list[float] = []
            treatment_values: list[float] = []
            adopted: list[str] = []
            for obs in observations:
                original = float(obs.get("观测值", 0.0))
                corrected = corrections.get(obs.get("观测编号"))
                value = original if corrected is None else corrected
                label = f"{obs['观测编号']}={original:g}"
                if corrected is not None:
                    label += f"→{corrected:g}"
                adopted.append(label)
                if obs.get("分组") == "对照组":
                    control_values.append(value)
                else:
                    treatment_values.append(value)

            # 按旧报告的历史口径（分组均值）重算，而不是套用新口径
            new_control = sum(control_values) / len(control_values) if control_values else 0.0
            new_treatment = sum(treatment_values) / len(treatment_values) if treatment_values else 0.0
            new_lift = (
                round((new_treatment - new_control) / new_control, 4) if new_control else 0.0
            )
            held = region in pending_regions
            next_id += 1
            conclusion = {
                "id": next_id,
                "结论编号": f"SUB-{batch['批次编号']}-{next_id:03d}",
                "批次id": batch_id,
                "批次编号": batch["批次编号"],
                "回执号": batch["回执号"],
                "地区": region,
                "指标名称": metric,
                "旧报告号": report.get("报告编号"),
                "采用口径": report.get("口径版本"),
                "旧对照组均值": report.get("对照组均值"),
                "旧实验组均值": report.get("实验组均值"),
                "新对照组均值": round(new_control, 4),
                "新实验组均值": round(new_treatment, 4),
                "新提升幅度": new_lift,
                "采用原观测": "、".join(adopted),
                "状态": CONCLUSION_HELD if held else CONCLUSION_READY,
                "暂缓原因": (
                    f"{region}存在待补证更正，原始观测未送达，暂缓替换" if held else ""
                ),
            }
            self._conclusions().append(conclusion)
            affected_labels.append(f"{region}/{metric}")

        batch["受影响指标"] = "；".join(affected_labels)
        batch["status"] = BATCH_RECALCED
        batch["pending"] = True
        self._refresh_counts(batch)
        return batch, (
            f"批量重算完成：影响{len(affected_labels)}个地区指标，"
            f"采用已核清{len(verified)}条，未受影响指标沿用原统计"
        )

    # ----------------------------------------------------------
    # 4. 发布清单与发布（同一回执只能发布一次）
    # ----------------------------------------------------------
    def publish_list(self) -> list[dict[str, Any]]:
        """运营发布清单：列出每条替代结论今天是否可替换及原因。"""
        items: list[dict[str, Any]] = []
        for row in self._conclusions():
            ready = row.get("状态") == CONCLUSION_READY
            items.append({
                "结论编号": row.get("结论编号"),
                "批次编号": row.get("批次编号"),
                "回执号": row.get("回执号"),
                "地区": row.get("地区"),
                "指标名称": row.get("指标名称"),
                "旧报告号": row.get("旧报告号"),
                "采用口径": row.get("采用口径"),
                "旧实验组均值": row.get("旧实验组均值"),
                "新实验组均值": row.get("新实验组均值"),
                "采用原观测": row.get("采用原观测"),
                "状态": row.get("状态"),
                "可替换": ready,
                "替换指令": (
                    f"今日可替换{row.get('地区')}结论" if ready
                    else row.get("暂缓原因") or f"结论{row.get('状态')}，不可重复替换"
                ),
            })
        return items

    def publish(self, batch_id: int) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """发布替代结论：同一回执并发提交只能发布一次。"""
        with self._publish_lock:
            batch = self.get_entry(batch_id)
            if batch is None:
                return None, f"{self.ENTITY} {batch_id} 不存在或已归档", []
            if batch.get("status") == BATCH_PUBLISHED:
                return batch, f"回执{batch['回执号']}的结论已发布过，本次忽略", []
            if batch.get("status") != BATCH_RECALCED:
                return batch, f"批次{batch['批次编号']}尚未完成重算，不能发布", []

            published: list[dict[str, Any]] = []
            for conclusion in self.batch_conclusions(batch_id):
                if conclusion.get("状态") != CONCLUSION_READY:
                    continue  # 暂缓替换的结论留在清单里，等补证后再发
                conclusion["状态"] = CONCLUSION_PUBLISHED
                report = self._find_report(conclusion.get("地区", ""), conclusion.get("指标名称", ""))
                if report is not None and report.get("报告编号") == conclusion.get("旧报告号"):
                    report["状态"] = "已被替代"
                published.append(conclusion)

            batch["status"] = BATCH_PUBLISHED
            batch["pending"] = False
            batch["abnormal"] = False
            self._refresh_counts(batch)
            held = sum(
                1 for row in self.batch_conclusions(batch_id)
                if row.get("状态") == CONCLUSION_HELD
            )
            message = f"已发布{len(published)}条替代结论"
            if held:
                message += f"，{held}条因待补证暂缓替换"
            return batch, message, published

    # ----------------------------------------------------------
    # 通用动作入口：让工作台按钮直接驱动领域流程
    # ----------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "样本核算":
            return self.verify_entries(entry_id)
        if action == "批量重算":
            return self.recalculate(entry_id)
        if action == "发布结论":
            batch, message, _ = self.publish(entry_id)
            return batch, message
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    # ----------------------------------------------------------
    # 内部工具
    # ----------------------------------------------------------
    def _refresh_counts(self, batch: dict[str, Any]) -> None:
        entries = self.batch_entries(int(batch.get("id", 0)))
        batch["更正条目数"] = len(entries)
        batch["已核清"] = sum(1 for row in entries if row.get("核算状态") == ENTRY_VERIFIED)
        batch["待补证"] = sum(1 for row in entries if row.get("核算状态") == ENTRY_WAITING_PROOF)
        batch["已拒绝"] = sum(1 for row in entries if row.get("核算状态") == ENTRY_REJECTED)
        batch["abnormal"] = batch["已拒绝"] > 0
