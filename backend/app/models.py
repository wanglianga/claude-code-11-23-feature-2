"""ORM 模型：全部核心账本采用追加式（append-only）或 SCD2 生效区间结构。

设计要点：
- 暂存区(raw_messages/manual_revisions)永不更新，补传以新行 + supersedes_uid 留痕；
- 主数据映射带 effective_from/effective_to 与 mapping_version；
- 事实账(supply_facts)带状态、证据指针、版本链；
- 指标(metric_versions)每次发布/重算生成新版本，旧版本保留并附差异说明。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# ---- 枚举常量（字符串存储，避免数据库枚举迁移成本） ----
SRC_DEVICE = "device"
SRC_MANUAL = "manual"
SRC_PLAN = "plan"
SRC_SYSTEM = "system"

READ_CUMULATIVE = "cumulative"      # 累计读数
READ_INSTANT = "instantaneous"      # 瞬时流量
READ_PLAN = "plan"                  # 计划目标

FACT_SUPPLY = "supply"              # 供水事实（实灌候选）
FACT_TRANSFER = "transfer"          # 交接计量
FACT_PLAN = "plan"                  # 计划目标事实（只作比对，不作实灌替身）

ST_CANDIDATE = "candidate"          # 候选（证据冲突未裁定）
ST_CONFIRMED = "confirmed"          # 已确认，可进入正式指标
ST_PENDING = "pending_verification"  # 待核实（如换表缺证截断）
ST_REJECTED = "rejected"
ST_TARGET = "target"

METRIC_TENTATIVE = "tentative"
METRIC_STABLE = "stable"
METRIC_FROZEN = "frozen"

# ---- 异常损耗诊断 ----
FACT_ADJUSTMENT = "adjustment"        # 校正调整事实（批准后的校正参数生成，不改原读数）

HYP_METER_OFFSET = "meter_offset"     # 仪表固定偏移
HYP_METER_GAIN = "meter_gain"         # 仪表倍率异常
HYP_SEGMENT_LOSS = "segment_loss"     # 渠段额外损耗（真实渗漏等）
HYP_CLOCK_SKEW = "clock_skew"         # 设备时钟偏差

HYP_CANDIDATE = "candidate"           # 候选（完全相容，未被排除）
HYP_SUPPORTED = "supported"           # 复测观测支持
HYP_REFUTED = "refuted"               # 复测观测排除
HYP_ADOPTED = "adopted"               # 已采纳（转化为校正参数/工程处置）

RUN_OPEN = "open"                     # 候选集合未收敛
RUN_CONVERGED = "converged"           # 复测后收敛到唯一候选
RUN_INCONCLUSIVE = "inconclusive"     # 复测后仍多候选，证据不足
RUN_ADOPTED = "adopted"               # 已有假设被采纳（不被后续新分析覆盖）

CORR_PENDING = "pending_approval"     # 待批准（设备资料变更时失效）
CORR_APPROVED = "approved"
CORR_REJECTED = "rejected"
CORR_INVALIDATED = "invalidated"


class DataContract(Base):
    __tablename__ = "data_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    source_kind: Mapped[str] = mapped_column(String(16))          # device/manual/plan/system
    reading_type: Mapped[str] = mapped_column(String(16))         # cumulative/instantaneous/plan
    unit: Mapped[str] = mapped_column(String(16))                 # m3 / m3_h
    device_id_pattern: Mapped[str] = mapped_column(String(128))   # 设备编号约束（前缀正则）
    collection_interval_sec: Mapped[int] = mapped_column(Integer)  # 采集区间
    payload_schema: Mapped[dict] = mapped_column(JSONB)           # 必填字段与取值规则
    source_version: Mapped[str] = mapped_column(String(32))
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RawMessage(Base):
    """设备报文暂存区：原样保存，补传不覆盖旧行。"""
    __tablename__ = "raw_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    contract_code: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_version: Mapped[str] = mapped_column(String(32))
    ingestion_batch: Mapped[str] = mapped_column(String(64), index=True)
    supersedes_uid: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ManualRevision(Base):
    """人工修订/补录暂存区：原样保存，必须带依据。"""
    __tablename__ = "manual_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    contract_code: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(Text, default="")          # 补录依据
    operator: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    supersedes_uid: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    parent_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)        # 1=干渠/渠首 2=支渠
    # ---- 诊断参数：渠段正常损耗区间与输送时长（异常损耗诊断引擎输入） ----
    normal_loss_min_ratio: Mapped[float] = mapped_column(Float, default=0.005)  # 正常损耗率下限
    normal_loss_max_ratio: Mapped[float] = mapped_column(Float, default=0.03)   # 正常损耗率上限
    travel_minutes: Mapped[int] = mapped_column(Integer, default=60)  # 上游到本渠道进口输送时长
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MeterPoint(Base):
    __tablename__ = "meter_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    channel_code: Mapped[str] = mapped_column(String(64), index=True)
    point_role: Mapped[str] = mapped_column(String(24))  # head 渠首 / branch 支渠 / field 田间 / transfer 交接
    accuracy_ratio: Mapped[float] = mapped_column(Float, default=0.02)  # 仪表精度（相对误差）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Plot(Base):
    __tablename__ = "plots"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    area_mu: Mapped[float] = mapped_column(Float)
    channel_code: Mapped[str] = mapped_column(String(64), index=True)  # 供水支渠
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RotationBatch(Base):
    __tablename__ = "rotation_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    planned_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MappingVersion(Base):
    """映射版本登记：每次主数据/换表关系发布生成一个版本。"""
    __tablename__ = "mapping_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    note: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class DeviceMapping(Base):
    """设备 -> 计量点 的 SCD2 映射，含换表关系与生效区间。"""
    __tablename__ = "device_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    meter_point_code: Mapped[str] = mapped_column(String(64), index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mapping_version: Mapped[int] = mapped_column(Integer, index=True)
    change_reason: Mapped[str] = mapped_column(String(128), default="initial")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class PlotBatchAllocation(Base):
    """地块参与轮灌批次的生效区间映射。"""
    __tablename__ = "plot_batch_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    plot_code: Mapped[str] = mapped_column(String(64), index=True)
    batch_code: Mapped[str] = mapped_column(String(64), index=True)
    planned_volume: Mapped[float] = mapped_column(Float)  # 计划水量 m3（目标，非实灌）
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mapping_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MeterChange(Base):
    """换表记录：voucher_present=False 即换表缺证。"""
    __tablename__ = "meter_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    meter_point_code: Mapped[str] = mapped_column(String(64), index=True)
    change_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    old_last_reading: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_first_reading: Mapped[float | None] = mapped_column(Float, nullable=True)
    voucher_present: Mapped[bool] = mapped_column(Boolean, default=False)
    voucher_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="missing_voucher")  # certified / missing_voucher
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SupplyFact(Base):
    """供水事实账（追加式）。计划事实 status=target，永不充当实灌。"""
    __tablename__ = "supply_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    fact_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    fact_kind: Mapped[str] = mapped_column(String(16), index=True)   # supply/transfer/plan
    status: Mapped[str] = mapped_column(String(24), index=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meter_point_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    channel_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    plot_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    batch_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reading_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    reading_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float] = mapped_column(Float)                      # 水量 m3
    unit: Mapped[str] = mapped_column(String(16), default="m3")
    method: Mapped[str] = mapped_column(String(32))
    # cumulative_delta / flow_integration / manual / handover / plan
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)  # 换表缺证截断
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_flag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    evidence_manual_ids: Mapped[list] = mapped_column(JSONB, default=list)
    mapping_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meter_change_id: Mapped[int | None] = mapped_column(ForeignKey("meter_changes.id"), nullable=True)
    adjudication_id: Mapped[int | None] = mapped_column(ForeignKey("adjudications.id"), nullable=True)
    correction_id: Mapped[int | None] = mapped_column(ForeignKey("correction_params.id"), nullable=True)
    supersedes_fact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 一个新事实可能取代多条旧事实（如证据冲突的双候选），用列表完整记录取代链
    replaces_fact_ids: Mapped[list] = mapped_column(JSONB, default=list)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    meter_change: Mapped["MeterChange"] = relationship()


class QualityEvent(Base):
    __tablename__ = "quality_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    rule_code: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16))               # info/warning/critical
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meter_point_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    interval_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)  # open/adjudicated/frozen/closed
    related_fact_ids: Mapped[list] = mapped_column(JSONB, default=list)
    reconciliation_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_tasks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Adjudication(Base):
    """人工裁定：证据冲突时选择采信哪一份证据。"""
    __tablename__ = "adjudications"
    id: Mapped[int] = mapped_column(primary_key=True)
    conflict_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(String(24), default="pending")
    # pending / accept_device / accept_manual / reject_both
    selected_evidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    quality_event_id: Mapped[int | None] = mapped_column(ForeignKey("quality_events.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconciliationTask(Base):
    """渠道守恒失衡对账任务；冻结期间损耗指标不得用计划量补平。"""
    __tablename__ = "reconciliation_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    channel_code: Mapped[str] = mapped_column(String(64), index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    upstream_value: Mapped[float] = mapped_column(Float)
    downstream_explained: Mapped[float] = mapped_column(Float)
    diff: Mapped[float] = mapped_column(Float)
    diff_ratio: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)  # open/frozen/resolved
    frozen_metric_keys: Mapped[list] = mapped_column(JSONB, default=list)
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecomputeRun(Base):
    """重算任务登记：每次迟到数据/换表/裁定/对账触发。"""
    __tablename__ = "recompute_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed
    affected_keys: Mapped[list] = mapped_column(JSONB, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MetricVersion(Base):
    """版本化指标：同一(日期,粒度,键)每次发布追加新版本，可回放。"""
    __tablename__ = "metric_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, index=True)
    metric_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    grain_type: Mapped[str] = mapped_column(String(16), index=True)   # plot/channel/batch
    grain_key: Mapped[str] = mapped_column(String(64), index=True)
    batch_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    planned_volume: Mapped[float] = mapped_column(Float, default=0.0)
    confirmed_volume: Mapped[float] = mapped_column(Float, default=0.0)
    pending_volume: Mapped[float] = mapped_column(Float, default=0.0)
    correction_volume: Mapped[float] = mapped_column(Float, default=0.0)  # 已批准校正调整量（单列）
    loss_lower: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_upper: Mapped[float | None] = mapped_column(Float, nullable=True)
    missing_rate: Mapped[float] = mapped_column(Float, default=0.0)
    over_allocation_risk: Mapped[float] = mapped_column(Float, default=0.0)
    plan_completion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)       # tentative/stable/frozen
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stability_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diff_note: Mapped[str] = mapped_column(Text, default="")
    input_fact_ids: Mapped[list] = mapped_column(JSONB, default=list)
    recompute_run_id: Mapped[int | None] = mapped_column(ForeignKey("recompute_runs.id"), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ---------------------------------------------------------------- 异常损耗诊断
class DiagnosticRun(Base):
    """一次异常损耗诊断分析（快照式）。

    观测截止点 observation_cutoff 在创建时固定：只使用 cutoff 之前接收的读数；
    截止后到达的迟到数据形成新的诊断运行，不覆盖本运行（已采用诊断更不被覆盖）。
    """
    __tablename__ = "diagnostic_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    channel_code: Mapped[str] = mapped_column(String(64), index=True)   # 被诊断的汇合（父）渠道
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(24), default="manual")  # manual/late_data/retest_followup
    status: Mapped[str] = mapped_column(String(16), default=RUN_OPEN, index=True)
    # open / converged / inconclusive / adopted
    upstream_total: Mapped[float] = mapped_column(Float, default=0.0)
    downstream_total: Mapped[float] = mapped_column(Float, default=0.0)
    residual_total: Mapped[float] = mapped_column(Float, default=0.0)        # 上游-下游 总残差
    unexplained_residual: Mapped[float] = mapped_column(Float, default=0.0)  # 容差外残差合计
    slot_minutes: Mapped[int] = mapped_column(Integer, default=60)           # 对齐分桶粒度
    inputs_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 快照：损耗区间/仪表精度/输送时长/逐时段残差明细
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class DiagnosticHypothesis(Base):
    """候选假设试算结果：固定偏移/倍率异常/渠段额外损耗/时钟偏差。

    多种解释同样成立时并列保存，绝不凭最小残差自动判定故障；
    explained/unexplained 分别记录该假设能解释的观测与仍不相容部分。
    """
    __tablename__ = "diagnostic_hypotheses"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_runs.id"), index=True)
    hypothesis_kind: Mapped[str] = mapped_column(String(24), index=True)
    # meter_offset / meter_gain / segment_loss / clock_skew
    target_kind: Mapped[str] = mapped_column(String(16))      # device / segment
    target: Mapped[str] = mapped_column(String(64), index=True)  # device_id 或 channel_code
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    # {offset_m3_per_slot} / {gain_ratio} / {extra_loss_m3_per_slot} / {shift_minutes}
    explained: Mapped[list] = mapped_column(JSONB, default=list)    # 应用后转为相容的时段
    unexplained: Mapped[list] = mapped_column(JSONB, default=list)  # 应用后仍超差的时段
    residual_after: Mapped[float] = mapped_column(Float, default=0.0)
    is_candidate: Mapped[bool] = mapped_column(Boolean, default=False)  # 完全相容=候选
    status: Mapped[str] = mapped_column(String(16), default=HYP_CANDIDATE, index=True)
    # candidate / supported / refuted / adopted
    refute_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    run: Mapped["DiagnosticRun"] = relationship()


class RetestPlan(Base):
    """复测建议：在某位置补一段读数可区分多少候选假设。"""
    __tablename__ = "retest_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_runs.id"), index=True)
    position_kind: Mapped[str] = mapped_column(String(16))    # meter_point / segment
    position: Mapped[str] = mapped_column(String(64))         # 计量点/渠道 code
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method: Mapped[str] = mapped_column(String(32))           # portable_flow_meter / clock_check
    equipment: Mapped[str] = mapped_column(String(128))       # 所需设备
    suggested_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    suggested_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 固定观测截止点
    discriminates: Mapped[list] = mapped_column(JSONB, default=list)
    # [{hypothesis_id, rival_id, reason}] 该位置可区分的假设对
    discriminating_score: Mapped[int] = mapped_column(Integer, default=0)  # 可区分假设对数
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    # proposed / reported / closed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RetestReport(Base):
    """现场回报：一次复测观测。回报后重算，未支持的假设被排除。"""
    __tablename__ = "retest_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("retest_plans.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_runs.id"), index=True)
    observed_value: Mapped[float] = mapped_column(Float)      # 实测真值（m3/h）
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    equipment: Mapped[str] = mapped_column(String(128), default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    candidates_before: Mapped[list] = mapped_column(JSONB, default=list)  # 回报前候选假设 id
    candidates_after: Mapped[list] = mapped_column(JSONB, default=list)   # 回报后候选假设 id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class CorrectionParam(Base):
    """校正参数：诊断估计转化为核算调整的唯一入口。

    必须批准才能作用于核算；批准时按 affected_intervals 生成追加式
    adjustment 事实（不直接改原读数）；设备资料变更使待批准校正失效。
    """
    __tablename__ = "correction_params"
    id: Mapped[int] = mapped_column(primary_key=True)
    correction_uid: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    hypothesis_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_hypotheses.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))             # meter_offset / meter_gain
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    meter_point_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    affected_intervals: Mapped[list] = mapped_column(JSONB, default=list)
    # [{interval_start, interval_end, delta_m3}] 全部受影响区间及差额
    status: Mapped[str] = mapped_column(String(20), default=CORR_PENDING, index=True)
    # pending_approval / approved / rejected / invalidated
    proposed_by: Mapped[str] = mapped_column(String(64), default="")
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidate_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    hypothesis: Mapped["DiagnosticHypothesis"] = relationship()
