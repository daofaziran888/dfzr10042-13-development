"""洞察工坊 · 样本更正批次

覆盖成组纠错全流程：
- 值班员一次提交多个地区的对照/实验观测更正，样本核算逐条关联地区、分组、证明材料；
- 甲区实验组 40 改为 30 后均值从 30 变为 25，重复回执只更正一次；
- 乙区引用不存在的观测给出失败原因，待补证观测不得进入重算；
- 未受影响指标沿用原统计，已发旧结论按历史口径重算；
- 替代结论列出旧报告号和用于复算的原观测；
- 多个地区并发提交同一更正回执时只能发布一次；
- 运营从发布清单决定今天是否替换甲区结论，并复查乙区未补证时的替换指令。
"""
from __future__ import annotations

import copy
import threading

import pytest

from app.seed import SEED_ROWS
from app.services.correction_batch import CorrectionBatchService
from app.store import store

DOMAIN_TABLES = [
    "correction_batch",
    "correction_entry",
    "replacement_conclusion",
    "experiment_observation",
    "published_report",
]


@pytest.fixture(autouse=True)
def fresh_correction_state():
    """每个用例前后都还原更正域数据，保证用例互不影响。"""
    snapshot = {name: copy.deepcopy(store.rows(name)) for name in DOMAIN_TABLES}
    for name in DOMAIN_TABLES:
        store.rows(name).clear()
        store.rows(name).extend(copy.deepcopy(SEED_ROWS.get(name, [])))
    yield
    for name in DOMAIN_TABLES:
        store.rows(name).clear()
        store.rows(name).extend(snapshot[name])


@pytest.fixture()
def service() -> CorrectionBatchService:
    return CorrectionBatchService()


def _submit_default(service: CorrectionBatchService):
    """甲区改实验组 40→30；乙区一条引用不存在的观测、一条缺证明材料。"""
    return service.submit_batch(
        receipt_no="RCPT-20261124-01",
        operator="值班员小赵",
        entries=[
            {"地区": "甲区", "分组": "实验组", "观测编号": "OBS-JA-T2",
             "新观测值": 30, "证明材料": "工单JW-101"},
            {"地区": "乙区", "分组": "实验组", "观测编号": "OBS-YI-T9",
             "新观测值": 35, "证明材料": "工单JW-102"},
            {"地区": "乙区", "分组": "实验组", "观测编号": "OBS-YI-T1",
             "新观测值": 35, "证明材料": ""},
        ],
    )


def _verified_batch(service: CorrectionBatchService):
    batch, created, _ = _submit_default(service)
    assert created
    service.verify_entries(batch["id"])
    return batch


def _recalced_batch(service: CorrectionBatchService):
    batch = _verified_batch(service)
    service.recalculate(batch["id"])
    return batch


def _conclusion(service: CorrectionBatchService, batch_id: int, region: str):
    for row in service.batch_conclusions(batch_id):
        if row["地区"] == region:
            return row
    return None


class TestSubmitBatch:
    def test_submit_links_region_group_proof_per_entry(self, service):
        """一次提交后，样本核算明细逐条关联地区、分组和证明材料。"""
        batch, created, message = _submit_default(service)
        assert created, message
        assert batch["status"] == "待核算"
        assert batch["更正条目数"] == 3

        entries = service.batch_entries(batch["id"])
        assert len(entries) == 3
        ja = next(row for row in entries if row["观测编号"] == "OBS-JA-T2")
        assert ja["地区"] == "甲区" and ja["分组"] == "实验组"
        assert ja["证明材料"] == "工单JW-101"
        assert ja["原观测值"] == 40.0
        assert ja["核算状态"] == "待核算"

    def test_duplicate_receipt_corrects_only_once(self, service):
        """重复回执只更正一次：第二次提交不落新批次、不重复条目。"""
        batch, created, _ = _submit_default(service)
        assert created
        again, created_again, message = _submit_default(service)
        assert not created_again
        assert again["id"] == batch["id"]
        assert "只更正一次" in message
        assert len(store.rows("correction_batch")) == 1
        assert len(service.batch_entries(batch["id"])) == 3

    def test_concurrent_submit_same_receipt_creates_one_batch(self, service):
        """多个地区并发提交同一更正回执：只建一个批次。"""
        results: list[tuple[dict, bool, str]] = []

        def submit():
            results.append(_submit_default(service))

        threads = [threading.Thread(target=submit) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for _, created, _ in results if created) == 1
        assert len(store.rows("correction_batch")) == 1
        batch_ids = {batch["id"] for batch, _, _ in results}
        assert len(batch_ids) == 1
        only = store.rows("correction_batch")[0]
        assert len(service.batch_entries(only["id"])) == 3


class TestVerifyEntries:
    def test_verified_entry_records_original_value(self, service):
        batch, _, _ = _submit_default(service)
        _, message = service.verify_entries(batch["id"])
        ja = next(row for row in service.batch_entries(batch["id"])
                  if row["观测编号"] == "OBS-JA-T2")
        assert ja["核算状态"] == "已核清"
        assert ja["原观测值"] == 40.0
        assert "已核清1条" in message

    def test_missing_observation_rejected_with_reason(self, service):
        """乙区引用不存在的观测：样本核算给出失败原因（拒绝记录）。"""
        batch, _, _ = _submit_default(service)
        service.verify_entries(batch["id"])
        yi = next(row for row in service.batch_entries(batch["id"])
                  if row["观测编号"] == "OBS-YI-T9")
        assert yi["核算状态"] == "已拒绝"
        assert "OBS-YI-T9" in yi["失败原因"]
        assert "不存在" in yi["失败原因"]

    def test_entry_without_proof_waits_for_original_observation(self, service):
        """缺证明材料的条目标记待补证：乙区仍在等原始观测。"""
        batch, _, _ = _submit_default(service)
        service.verify_entries(batch["id"])
        yi = next(row for row in service.batch_entries(batch["id"])
                  if row["观测编号"] == "OBS-YI-T1")
        assert yi["核算状态"] == "待补证"
        assert "补证" in yi["失败原因"]
        assert service.get_entry(batch["id"])["待补证"] == 1

    def test_supplement_proof_allows_reverify(self, service):
        batch, _, _ = _submit_default(service)
        service.verify_entries(batch["id"])
        yi = next(row for row in service.batch_entries(batch["id"])
                  if row["观测编号"] == "OBS-YI-T1")
        service.supplement_proof(yi["id"], "补交工单JW-109")
        service.verify_entries(batch["id"])
        yi = next(row for row in service.batch_entries(batch["id"])
                  if row["观测编号"] == "OBS-YI-T1")
        assert yi["核算状态"] == "已核清"


class TestRecalculate:
    def test_ja_treatment_mean_moves_from_30_to_25(self, service):
        """甲区实验组 40 改为 30：实验组均值从 30 变为 25，对照组重算仍为 15。"""
        batch = _recalced_batch(service)
        ja = _conclusion(service, batch["id"], "甲区")
        assert ja is not None
        assert ja["旧实验组均值"] == 30.0
        assert ja["新实验组均值"] == 25.0
        assert ja["旧对照组均值"] == 15.0
        assert ja["新对照组均值"] == 15.0

    def test_pending_proof_observation_excluded_from_recalculate(self, service):
        """待补证的观测不得进入重算：乙区实验组均值沿用 40，不按 35 重算。"""
        batch = _recalced_batch(service)
        yi = _conclusion(service, batch["id"], "乙区")
        assert yi is not None
        assert yi["新实验组均值"] == 40.0
        assert "OBS-YI-T1=30" in yi["采用原观测"]
        assert "→" not in yi["采用原观测"]

    def test_unaffected_metric_keeps_original_statistics(self, service):
        """未受影响的指标沿用原统计：甲区客单价不生成替代结论、旧报告不动。"""
        batch = _recalced_batch(service)
        regions_metrics = {
            (row["地区"], row["指标名称"]) for row in service.batch_conclusions(batch["id"])
        }
        assert ("甲区", "客单价") not in regions_metrics
        report = next(row for row in store.rows("published_report")
                      if row["报告编号"] == "RPT-JA-002")
        assert report["状态"] == "已发布"
        assert report["实验组均值"] == 125.0

    def test_issued_report_recalculated_with_historical_caliber(self, service):
        """已发旧结论按历史口径重算：替代结论沿用旧报告的口径版本。"""
        batch = _recalced_batch(service)
        ja = _conclusion(service, batch["id"], "甲区")
        assert ja["采用口径"] == "均值口径v1"
        # 历史口径 = 分组均值：(20+30)/2 = 25
        assert ja["新实验组均值"] == 25.0
        assert ja["新提升幅度"] == round((25.0 - 15.0) / 15.0, 4)

    def test_conclusion_lists_old_report_and_adopted_observations(self, service):
        """替代结论列出旧报告号和用于复算的原观测。"""
        batch = _recalced_batch(service)
        ja = _conclusion(service, batch["id"], "甲区")
        assert ja["旧报告号"] == "RPT-JA-001"
        adopted = ja["采用原观测"]
        for obs_no in ("OBS-JA-C1", "OBS-JA-C2", "OBS-JA-T1", "OBS-JA-T2"):
            assert obs_no in adopted
        assert "OBS-JA-T2=40→30" in adopted

    def test_recalculate_is_repeatable_before_publish(self, service):
        """重算可重复执行：结论不翻倍、结果一致。"""
        batch = _verified_batch(service)
        service.recalculate(batch["id"])
        first = copy.deepcopy(service.batch_conclusions(batch["id"]))
        service.recalculate(batch["id"])
        second = service.batch_conclusions(batch["id"])
        assert len(first) == len(second) == 2
        ja = _conclusion(service, batch["id"], "甲区")
        assert ja["新实验组均值"] == 25.0


class TestPublish:
    def test_publish_list_guides_operations(self, service):
        """发布清单：甲区已核清今天可替换，乙区待补证给出暂缓原因。"""
        batch = _recalced_batch(service)
        items = {row["地区"]: row for row in service.publish_list()}
        assert items["甲区"]["可替换"] is True
        assert "今日可替换甲区结论" in items["甲区"]["替换指令"]
        assert items["乙区"]["可替换"] is False
        assert "待补证" in items["乙区"]["替换指令"]

    def test_publish_replaces_only_ready_conclusions(self, service):
        """实际发出报告：甲区替代旧结论，乙区未补证继续暂缓。"""
        batch = _recalced_batch(service)
        _, message, published = service.publish(batch["id"])
        assert len(published) == 1
        assert published[0]["地区"] == "甲区"
        assert "暂缓" in message

        ja = _conclusion(service, batch["id"], "甲区")
        assert ja["状态"] == "已发布"
        yi = _conclusion(service, batch["id"], "乙区")
        assert yi["状态"] == "暂缓替换"

        old = next(row for row in store.rows("published_report")
                   if row["报告编号"] == "RPT-JA-001")
        assert old["状态"] == "已被替代"
        untouched = next(row for row in store.rows("published_report")
                         if row["报告编号"] == "RPT-YI-001")
        assert untouched["状态"] == "已发布"

    def test_repeated_publish_only_once(self, service):
        """同一回执重复发布：第二次忽略，结论不重复发出。"""
        batch = _recalced_batch(service)
        service.publish(batch["id"])
        _, message, published_again = service.publish(batch["id"])
        assert published_again == []
        assert "已发布过" in message
        ja = _conclusion(service, batch["id"], "甲区")
        assert ja["状态"] == "已发布"

    def test_concurrent_publish_same_receipt_only_once(self, service):
        """多个地区并发提交同一更正回执的发布请求：只能发布一次。"""
        batch = _recalced_batch(service)
        outcomes: list[tuple[dict, str, list]] = []

        def publish():
            outcomes.append(service.publish(batch["id"]))

        threads = [threading.Thread(target=publish) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        total_published = sum(len(published) for _, _, published in outcomes)
        assert total_published == 1
        assert sum(1 for _, message, _ in outcomes if "已发布过" in message) == 7
        ja = _conclusion(service, batch["id"], "甲区")
        assert ja["状态"] == "已发布"


class TestFullCorrectionFlow:
    def test_batch_submit_verify_publish_and_review(self, service):
        """批量提交地区更正 → 样本核算查实均值和拒绝记录 → 发出报告 → 复查乙区替换指令。"""
        # 1. 批量提交地区更正
        batch, created, _ = _submit_default(service)
        assert created

        # 2. 样本核算：查实均值（甲区已核清、原观测值 40）和拒绝记录（乙区观测不存在）
        service.verify_entries(batch["id"])
        entries = service.batch_entries(batch["id"])
        assert any(row["核算状态"] == "已核清" and row["原观测值"] == 40.0
                   for row in entries)
        assert any(row["核算状态"] == "已拒绝" and "不存在" in row["失败原因"]
                   for row in entries)

        # 3. 批量重算并实际发出报告
        service.recalculate(batch["id"])
        _, _, published = service.publish(batch["id"])
        assert [row["地区"] for row in published] == ["甲区"]
        assert published[0]["旧报告号"] == "RPT-JA-001"
        assert published[0]["新实验组均值"] == 25.0

        # 4. 复查：乙区未补证时，运营看到的替换指令仍是暂缓
        items = {row["地区"]: row for row in service.publish_list()}
        assert items["甲区"]["状态"] == "已发布"
        assert items["甲区"]["可替换"] is False
        assert items["乙区"]["可替换"] is False
        assert "暂缓替换" in items["乙区"]["替换指令"]
        assert "待补证" in items["乙区"]["替换指令"]
