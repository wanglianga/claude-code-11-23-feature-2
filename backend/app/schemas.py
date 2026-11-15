from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------- 数据契约 ----------------
class ContractIn(BaseModel):
    code: str
    name: str
    source_kind: str = Field(pattern="^(device|manual|plan|system)$")
    reading_type: str = Field(pattern="^(cumulative|instantaneous|plan)$")
    unit: str
    device_id_pattern: str
    collection_interval_sec: int = Field(gt=0)
    payload_schema: dict[str, Any]
    source_version: str
    active_from: datetime
    active_to: datetime | None = None


class ContractOut(ContractIn):
    id: int
    class Config: from_attributes = True


# ---------------- 报文接入 ----------------
class RawMessageIn(BaseModel):
    message_uid: str
    contract_code: str
    device_id: str
    payload: dict[str, Any]
    event_time: datetime
    received_at: datetime
    source_version: str
    supersedes_uid: str | None = None


class ManualRevisionIn(BaseModel):
    revision_uid: str
    contract_code: str
    device_id: str
    event_time: datetime
    value: float
    unit: str
    basis: str = ""
    operator: str
    received_at: datetime
    supersedes_uid: str | None = None


class IngestResult(BaseModel):
    accepted: bool
    stored_uid: str
    is_late: bool = False
    revision_no: int = 1
    violations: list[str] = []
    message: str = ""


# ---------------- 主数据 ----------------
class ChannelIn(BaseModel):
    code: str
    name: str
    parent_code: str | None = None
    level: int = 1
    normal_loss_min_ratio: float = Field(default=0.005, ge=0)
    normal_loss_max_ratio: float = Field(default=0.03, ge=0)
    travel_minutes: int = Field(default=60, ge=0)


class MeterPointIn(BaseModel):
    code: str
    name: str
    channel_code: str
    point_role: str = Field(pattern="^(head|branch|field|transfer)$")
    accuracy_ratio: float = Field(default=0.02, gt=0)


class PlotIn(BaseModel):
    code: str
    name: str
    area_mu: float
    channel_code: str


class BatchIn(BaseModel):
    code: str
    name: str
    planned_start: datetime
    planned_end: datetime


class DeviceMappingIn(BaseModel):
    device_id: str
    meter_point_code: str
    effective_from: datetime
    effective_to: datetime | None = None
    change_reason: str = "initial"


class AllocationIn(BaseModel):
    plot_code: str
    batch_code: str
    planned_volume: float = Field(ge=0)
    effective_from: datetime
    effective_to: datetime | None = None


class MeterChangeIn(BaseModel):
    device_id: str
    meter_point_code: str
    change_time: datetime
    old_last_reading: float | None = None
    new_first_reading: float | None = None
    voucher_present: bool = False
    voucher_ref: str | None = None
    note: str = ""


# ---------------- 裁定 ----------------
class AdjudicationIn(BaseModel):
    decision: str = Field(pattern="^(accept_device|accept_manual|reject_both)$")
    rationale: str = ""
    operator: str = "admin"


# ---------------- 对账处理 ----------------
class ReconciliationResolveIn(BaseModel):
    resolution: str
    operator: str = "admin"


# ---------------- 异常损耗诊断 ----------------
class DiagnosisRunIn(BaseModel):
    channel_code: str
    window_start: datetime
    window_end: datetime
    observation_cutoff: datetime
    trigger: str = "manual"
    slot_minutes: int = Field(default=60, gt=0)
    note: str = ""


class RetestObservation(BaseModel):
    device_id: str
    observed_value: float                 # 便携设备实测真值流量 m3/h
    meter_reading: float | None = None    # 观测时刻该表 concurrent 读数流量（缺省取窗口均值）
    observed_at: datetime | None = None


class RetestReportIn(BaseModel):
    observations: list[RetestObservation]
    operator: str = "站长"
    equipment: str = ""
    note: str = ""


class CorrectionProposeIn(BaseModel):
    operator: str = "站长"


class CorrectionApproveIn(BaseModel):
    operator: str = "admin"


class CorrectionRejectIn(BaseModel):
    operator: str = "admin"
    reason: str = ""


# ---------------- 通用输出 ----------------
class LineageNode(BaseModel):
    kind: str
    id: str
    label: str
    detail: dict[str, Any] = {}


class LineageEdge(BaseModel):
    source: str
    target: str
    label: str = ""


class LineageGraph(BaseModel):
    nodes: list[LineageNode]
    edges: list[LineageEdge]
