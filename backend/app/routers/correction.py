"""样本更正批次专用接口：成组提交、样本核算、批量重算、发布清单与幂等发布。

这些路径挂在 /api/correction_batch 下，必须在通用资源路由之前注册，
否则 /api/{module}/{entry_id} 会抢先匹配 publish-list 等固定路径。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for
from app.services.correction_batch import CorrectionBatchService

router = APIRouter(prefix="/api/correction_batch", tags=["样本更正批次"])


class CorrectionEntryPayload(BaseModel):
    地区: str
    分组: str
    观测编号: str
    新观测值: float
    证明材料: str = ""


class SubmitBatchPayload(BaseModel):
    回执号: str
    值班员: str
    提交时间: str = ""
    entries: list[CorrectionEntryPayload] = Field(default_factory=list)


class ProofPayload(BaseModel):
    证明材料: str


def _service() -> CorrectionBatchService:
    service = service_for("correction_batch")
    assert isinstance(service, CorrectionBatchService)
    return service


def _batch_or_404(batch_id: int) -> dict[str, Any]:
    batch = _service().get_entry(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"样本更正批次 {batch_id} 不存在或已归档")
    return batch


@router.post("/submit", response_model=ActionResult)
def submit_batch(payload: SubmitBatchPayload) -> ActionResult:
    """值班员一次提交多个地区的对照/实验观测更正；同一回执号只受理一次。"""
    batch, created, message = _service().submit_batch(
        receipt_no=payload.回执号,
        operator=payload.值班员,
        entries=[item.model_dump() for item in payload.entries],
        submit_time=payload.提交时间,
    )
    return ActionResult(ok=created or bool(batch), message=message, entry=batch or None)


@router.get("/publish-list", response_model=dict)
def publish_list() -> dict[str, Any]:
    """运营发布清单：每条替代结论今天是否可替换及原因。"""
    items = _service().publish_list()
    return {"items": items, "total": len(items)}


@router.get("/{batch_id}/entries", response_model=dict)
def list_entries(batch_id: int) -> dict[str, Any]:
    """样本核算明细：逐条关联的地区、分组、证明材料与核算结论。"""
    _batch_or_404(batch_id)
    items = _service().batch_entries(batch_id)
    return {"items": items, "total": len(items)}


@router.get("/{batch_id}/conclusions", response_model=dict)
def list_conclusions(batch_id: int) -> dict[str, Any]:
    """本批次重算出的替代结论（含旧报告号和用于复算的原观测）。"""
    _batch_or_404(batch_id)
    items = _service().batch_conclusions(batch_id)
    return {"items": items, "total": len(items)}


@router.post("/{batch_id}/verify", response_model=ActionResult)
def verify(batch_id: int) -> ActionResult:
    batch, message = _service().verify_entries(batch_id)
    return ActionResult(ok=batch is not None, message=message, entry=batch)


@router.post("/{batch_id}/recalculate", response_model=ActionResult)
def recalculate(batch_id: int) -> ActionResult:
    batch, message = _service().recalculate(batch_id)
    return ActionResult(ok=batch is not None, message=message, entry=batch)


@router.post("/{batch_id}/publish", response_model=ActionResult)
def publish(batch_id: int) -> ActionResult:
    batch, message, published = _service().publish(batch_id)
    return ActionResult(
        ok=batch is not None,
        message=message,
        entry={"batch": batch, "published": published} if batch else None,
    )


@router.post("/entries/{entry_id}/proof", response_model=ActionResult)
def supplement_proof(entry_id: int, payload: ProofPayload) -> ActionResult:
    """补录证明材料，补证后条目重新进入样本核算。"""
    entry, message = _service().supplement_proof(entry_id, payload.证明材料)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
