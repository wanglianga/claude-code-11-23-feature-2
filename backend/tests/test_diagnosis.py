"""异常损耗诊断硬规则测试。

覆盖：
1. 输送时长对齐与容差外残差定位（平衡内不产生假设）；
2. 多假设并列，不凭最小残差独断；
3. 复测回报真正改变候选集合，未支持假设被排除；
4. 证据不足时保持 inconclusive，不下结论；
5. 校正参数必须批准才作用于核算，且不直接改原读数；
6. 渠段渗漏假设不可转化为计量校正参数；
7. 设备资料变更（换表/映射调整）使待批准校正失效；
8. 迟到数据形成新诊断运行，不覆盖已采用诊断；
9. 未批准校正不进入指标，批准后校正量单列（不进确认实灌）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    CorrectionParam, DiagnosticHypothesis, DiagnosticRun, RawMessage,
    RetestPlan, SupplyFact,
)
from app.schemas import (
    ChannelIn, ContractIn, DeviceMappingIn, MeterChangeIn, MeterPointIn,
    RawMessageIn,
)
from app.services import master_data as md
from app.services.diagnosis import (
    approve_correction, propose_correction, run_diagnosis, submit_retest_report,
)
from app.services.ingestion import ingest_raw, register_contract
from app.services.metrics import publish_metrics

UTC = timezone.utc
T0 = datetime(2026, 11, 11, tzinfo=UTC)


def t(h, m=0, day=11):
    return datetime(2026, 11, day, h, m, tzinfo=UTC)


def _setup_west(db):
    register_contract(db, ContractIn(
        code="CT-CUM", name="累计", source_kind="device", reading_type="cumulative",
        unit="m3", device_id_pattern=r"D-.*", collection_interval_sec=3600,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="v1", active_from=T0))
    md.create_channel(db, ChannelIn(
        code="WEST", name="西干渠", level=1,
        normal_loss_min_ratio=0.005, normal_loss_max_ratio=0.03, travel_minutes=60))
    md.create_channel(db, ChannelIn(
        code="W1", name="西一", level=2, parent_code="WEST", travel_minutes=60))
    md.create_channel(db, ChannelIn(
        code="W2", name="西二", level=2, parent_code="WEST", travel_minutes=60))
    md.create_meter_point(db, MeterPointIn(
        code="MP-WH", name="渠首", channel_code="WEST", point_role="head",
        accuracy_ratio=0.02))
    md.create_meter_point(db, MeterPointIn(
        code="MP-W1", name="支一", channel_code="W1", point_role="branch",
        accuracy_ratio=0.02))
    md.create_meter_point(db, MeterPointIn(
        code="MP-W2", name="支二", channel_code="W2", point_role="branch",
        accuracy_ratio=0.02))
    v = md.publish_mapping_version(db, "init")
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-WH-01", meter_point_code="MP-WH", effective_from=t(0)), v)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-W1-01", meter_point_code="MP-W1", effective_from=t(0)), v)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-W2-01", meter_point_code="MP-W2", effective_from=t(0)), v)
    db.commit()


def _series(db, dev, readings, day=11, uid_prefix="M"):
    """readings: 列表（从 00:00 起逐小时）或 {小时: 读数} 字典。"""
    pairs = readings.items() if isinstance(readings, dict) else enumerate(readings)
    for h, r in pairs:
        ingest_raw(db, RawMessageIn(
            message_uid=f"{uid_prefix}-{dev}-{day}-{h}", contract_code="CT-CUM",
            device_id=dev, payload={"reading": r, "unit": "m3"},
            event_time=t(h, day=day), received_at=t(h, day=day) + timedelta(minutes=5),
            source_version="v1"), batch="test")
    db.commit()


def _leak_readings(db):
    """渠段渗漏场景：渠首 [225,225,112x4]，W1 恒定 90，W2 [100x3,0x3]。"""
    _series(db, "D-WH-01", [1000, 1225, 1450, 1562, 1674, 1786, 1898])
    _series(db, "D-W1-01", [500, 590, 680, 770, 860, 950, 1040])
    _series(db, "D-W2-01", [2000, 2100, 2200, 2300, 2300, 2300, 2300])


def _drift_readings(db):
    """W2 表固定偏移场景：W2 真值 [100x3,30x3]，表少计 12/h -> [88x3,18x3]。"""
    _series(db, "D-WH-01", [1000, 1194, 1388, 1510, 1632, 1754, 1876])
    _series(db, "D-W1-01", [500, 590, 680, 770, 860, 950, 1040])
    _series(db, "D-W2-01", [2000, 2088, 2176, 2264, 2282, 2300, 2318])


def _cands(db, run_id):
    return db.query(DiagnosticHypothesis).filter_by(
        run_id=run_id, status="candidate").all()


# ---------------------------------------------------------------- 1. 对齐与平衡
def test_balanced_window_produces_no_hypothesis(db):
    """残差全部落在允许区间内：不产生任何候选假设。"""
    _setup_west(db)
    # 渠首 = (W1+W2) + 2% 正常损耗，完全相容
    _series(db, "D-WH-01", [1000, 1194, 1388, 1582, 1776, 1970, 2164])  # 194/h
    _series(db, "D-W1-01", [0, 90, 180, 270, 360, 450, 540])
    _series(db, "D-W2-01", [0, 100, 200, 300, 400, 500, 600])
    run = run_diagnosis(db, "WEST", t(0), t(6), t(8))
    db.commit()
    assert run.unexplained_residual == 0
    assert db.query(DiagnosticHypothesis).filter_by(run_id=run.id).count() == 0
    assert "无不可解释残差" in run.note


def test_residual_localized_outside_tolerance(db):
    """超差残差被定位到具体时段，且生成并列假设。"""
    _setup_west(db)
    _leak_readings(db)
    run = run_diagnosis(db, "WEST", t(0), t(6), t(8))
    db.commit()
    assert run.unexplained_residual > 0
    slots = run.inputs_snapshot["slots"]
    bad = [s for s in slots if abs(s["excess"]) > 1e-9]
    assert len(bad) == 5  # 5 个对齐时段全部超差
    assert all(s["residual"] > s["allow_hi"] for s in bad)


# ---------------------------------------------------------------- 2. 并列不独断
def test_parallel_hypotheses_no_argmin(db):
    """渠段渗漏与仪表偏移/倍率数学上同样成立时必须全部并列候选。"""
    _setup_west(db)
    _leak_readings(db)
    run = run_diagnosis(db, "WEST", t(0), t(6), t(8))
    db.commit()
    cands = _cands(db, run.id)
    kinds = {(h.hypothesis_kind, h.target) for h in cands}
    assert ("segment_loss", "WEST") in kinds
    assert ("meter_offset", "D-WH-01") in kinds
    assert ("meter_offset", "D-W1-01") in kinds
    assert ("meter_gain", "D-WH-01") in kinds
    assert ("meter_gain", "D-W1-01") in kinds
    # W2 后段停灌：停灌时段残差仍在 -> W2 仪表假设被数据排除
    assert not any(h.target == "D-W2-01" for h in cands)
    # 每个候选都完整解释全部超差时段；系统不选定任何"最优"
    assert all(h.is_candidate for h in cands)
    assert run.status == "open"


# ---------------------------------------------------------------- 3. 复测改变候选集合
def test_retest_report_shrinks_candidate_set(db):
    """现场回报后未支持的假设被排除，候选集合真正缩小并收敛。"""
    _setup_west(db)
    _leak_readings(db)
    run = run_diagnosis(db, "WEST", t(0), t(6), t(8))
    db.commit()
    before = {h.id for h in _cands(db, run.id)}
    assert len(before) == 5
    plan = db.query(RetestPlan).filter_by(run_id=run.id, status="proposed")\
        .order_by(RetestPlan.discriminating_score.desc()).first()
    assert plan and plan.discriminating_score > 0  # 建议确实具备区分能力
    rep = submit_retest_report(db, plan.id, [
        {"device_id": "D-WH-01", "observed_value": 224.5, "meter_reading": 225.0},
        {"device_id": "D-W1-01", "observed_value": 90.8, "meter_reading": 90.0},
    ], operator="站长")
    db.commit()
    after = {h.id for h in _cands(db, run.id)} | {
        h.id for h in db.query(DiagnosticHypothesis).filter_by(
            run_id=run.id, status="supported")}
    assert len(rep.candidates_before) == 5
    assert len(rep.candidates_after) == 1
    assert rep.candidates_after != rep.candidates_before  # 复测真正改变候选集合
    db.refresh(run)
    assert run.status == "converged"
    seg = db.query(DiagnosticHypothesis).filter_by(
        run_id=run.id, hypothesis_kind="segment_loss").one()
    assert seg.status == "supported"
    # 被排除假设留有排除原因
    refuted = db.query(DiagnosticHypothesis).filter_by(
        run_id=run.id, status="refuted").all()
    assert len(refuted) == 4
    assert all("复测实测" in h.refute_reason for h in refuted)


# ---------------------------------------------------------------- 4. 证据不足
def test_insufficient_evidence_stays_inconclusive(db):
    """稀疏读数下多个假设同样成立；一次复测不能唯一确定时明确不下结论。"""
    _setup_west(db)
    # 各表仅首尾两个读数（中间缺测，稀疏均摊）
    _series(db, "D-WH-01", {0: 1000, 6: 2080})
    _series(db, "D-W1-01", {0: 0, 6: 540})
    _series(db, "D-W2-01", {0: 0, 6: 420})
    run = run_diagnosis(db, "WEST", t(0), t(6), t(8))
    db.commit()
    assert run.inputs_snapshot["sparse_readings"] is True
    cands = _cands(db, run.id)
    assert len(cands) >= 5  # 恒定流量下偏移/倍率/渗漏不可区分
    plan = db.query(RetestPlan).filter_by(
        run_id=run.id, status="proposed", device_id="D-W1-01").first()
    rep = submit_retest_report(db, plan.id, [
        {"device_id": "D-W1-01", "observed_value": 89.7, "meter_reading": 90.0},
    ], operator="站长")
    db.commit()
    db.refresh(run)
    # 复测排除了 W1 相关假设，但仍有多个候选 -> 不能下结论
    assert len(rep.candidates_after) >= 2
    assert len(rep.candidates_after) < len(rep.candidates_before)
    assert run.status == "inconclusive"
    assert "不能下结论" in run.note


# ---------------------------------------------------------------- 5. 校正批准与核算
def _converge_to_w2_offset(db):
    _drift_readings(db)
    run = run_diagnosis(db, "WEST", t(0), t(6), t(8))
    db.commit()
    plan = db.query(RetestPlan).filter_by(
        run_id=run.id, status="proposed", device_id="D-WH-01").first()
    submit_retest_report(db, plan.id, [
        {"device_id": "D-WH-01", "observed_value": 193.5, "meter_reading": 194.0},
        {"device_id": "D-W1-01", "observed_value": 90.1, "meter_reading": 90.0},
        {"device_id": "D-W2-01", "observed_value": 99.8, "meter_reading": 88.0},
    ], operator="站长")
    db.commit()
    db.refresh(run)
    assert run.status == "converged"
    hyp = db.query(DiagnosticHypothesis).filter_by(
        run_id=run.id, hypothesis_kind="meter_offset", target="D-W2-01").one()
    assert hyp.status == "supported"
    return run, hyp


def test_correction_requires_approval_and_preserves_raw(db):
    """校正参数批准前不进指标；批准后生成追加式调整事实，原读数不变。"""
    _setup_west(db)
    run, hyp = _converge_to_w2_offset(db)
    # 编译事实账（诊断只读暂存区，指标核算基于事实账）
    from app.services.fact_compiler import compile_device
    for dev in ("D-WH-01", "D-W1-01", "D-W2-01"):
        compile_device(db, dev, "cumulative")
    db.commit()
    raw_before = [(m.message_uid, m.payload.get("reading"))
                  for m in db.query(RawMessage).order_by(RawMessage.id).all()]

    corr = propose_correction(db, hyp.id, "站长")
    db.commit()
    assert corr.status == "pending_approval"
    assert len(corr.affected_intervals) == 5  # 全部受影响区间及差额
    assert all(iv["delta_m3"] > 0 for iv in corr.affected_intervals)

    # 未批准：指标不含任何校正量
    publish_metrics(db, trigger="test", as_of=t(10))
    db.commit()
    from app.models import MetricVersion
    mv = db.query(MetricVersion).filter_by(
        grain_type="channel", grain_key="W2", superseded_by=None).one()
    assert mv.correction_volume == 0

    # 批准：生成追加式 adjustment 事实；原读数与原事实不变
    approve_correction(db, corr.id, "admin")
    db.commit()
    raw_after = [(m.message_uid, m.payload.get("reading"))
                 for m in db.query(RawMessage).order_by(RawMessage.id).all()]
    assert raw_before == raw_after  # 不直接改原读数
    adjustments = db.query(SupplyFact).filter_by(
        fact_kind="adjustment", correction_id=corr.id).all()
    assert len(adjustments) == 5
    assert all(f.status == "confirmed" for f in adjustments)
    total_adj = sum(f.value for f in adjustments)
    assert total_adj == pytest.approx(59.325, abs=0.01)

    # 重发布：校正量单列，不混入确认实灌
    publish_metrics(db, trigger="correction", as_of=t(11))
    db.commit()
    mv2 = db.query(MetricVersion).filter_by(
        grain_type="channel", grain_key="W2", superseded_by=None).one()
    assert mv2.correction_volume == pytest.approx(59.325, abs=0.01)
    assert mv2.confirmed_volume == mv.confirmed_volume  # 确认实灌不受校正影响
    db.refresh(run)
    assert run.status == "adopted"


def test_segment_loss_cannot_become_correction(db):
    """渠段额外损耗是真实水损失，不可转化为计量校正参数。"""
    _setup_west(db)
    _leak_readings(db)
    run = run_diagnosis(db, "WEST", t(0), t(6), t(8))
    db.commit()
    seg = db.query(DiagnosticHypothesis).filter_by(
        run_id=run.id, hypothesis_kind="segment_loss").one()
    with pytest.raises(ValueError, match="工程处置"):
        propose_correction(db, seg.id, "站长")


def test_refuted_hypothesis_cannot_propose_correction(db):
    """被复测排除的假设不得再提出校正参数。"""
    _setup_west(db)
    run, hyp = _converge_to_w2_offset(db)
    refuted = db.query(DiagnosticHypothesis).filter_by(
        run_id=run.id, status="refuted").first()
    assert refuted is not None
    with pytest.raises(ValueError):
        propose_correction(db, refuted.id, "站长")


# ---------------------------------------------------------------- 6. 设备资料变更
def test_meter_change_invalidates_pending_correction(db):
    """换表登记使该设备待批准校正失效；已批准的不受影响。"""
    _setup_west(db)
    run, hyp = _converge_to_w2_offset(db)
    corr = propose_correction(db, hyp.id, "站长")
    db.commit()
    md.register_meter_change(db, MeterChangeIn(
        device_id="D-W2-01", meter_point_code="MP-W2", change_time=t(0, 30, day=12),
        old_last_reading=2318, new_first_reading=2318, voucher_present=True,
        voucher_ref="JC-1", note="漂移表更换"))
    db.commit()
    db.refresh(corr)
    assert corr.status == "invalidated"
    assert "换表" in corr.invalidate_reason
    # 失效后可重新提出
    corr2 = propose_correction(db, hyp.id, "站长")
    db.commit()
    assert corr2.status == "pending_approval"


def test_device_mapping_change_invalidates_pending_correction(db):
    """设备映射变更同样使待批准校正失效。"""
    _setup_west(db)
    run, hyp = _converge_to_w2_offset(db)
    corr = propose_correction(db, hyp.id, "站长")
    db.commit()
    v2 = md.publish_mapping_version(db, "设备调整")
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-W2-01", meter_point_code="MP-W1",
        effective_from=t(0, day=12), change_reason="计量点调整"), v2)
    db.commit()
    db.refresh(corr)
    assert corr.status == "invalidated"
    assert "映射变更" in corr.invalidate_reason


# ---------------------------------------------------------------- 7. 迟到数据
def test_late_data_forms_new_run_without_overwriting(db):
    """迟到数据形成新诊断运行；已采用诊断的状态与假设不被覆盖。"""
    _setup_west(db)
    run, hyp = _converge_to_w2_offset(db)
    corr = propose_correction(db, hyp.id, "站长")
    approve_correction(db, corr.id, "admin")
    db.commit()
    db.refresh(run)
    assert run.status == "adopted"
    hyp_status_before = hyp.status

    # 迟到补传（received 晚于原观测截止点）-> 新诊断运行
    ingest_raw(db, RawMessageIn(
        message_uid="M-D-W2-01-11-6-REV", contract_code="CT-CUM",
        device_id="D-W2-01", payload={"reading": 2310, "unit": "m3"},
        event_time=t(6), received_at=t(10),
        source_version="v1", supersedes_uid="M-D-W2-01-11-6"), batch="late")
    db.commit()
    run2 = run_diagnosis(db, "WEST", t(0), t(6), t(12), trigger="late_data")
    db.commit()
    assert run2.id != run.id
    assert run2.trigger == "late_data"
    db.refresh(run)
    db.refresh(hyp)
    assert run.status == "adopted"          # 旧结果不被新分析覆盖
    assert hyp.status == hyp_status_before
    # 新运行使用新截止点，独立给出自己的候选集合
    assert run2.observation_cutoff > run.observation_cutoff
    assert db.query(DiagnosticHypothesis).filter_by(run_id=run2.id).count() > 0


def test_retest_on_adopted_run_rejected(db):
    """已采用诊断闭环后，不再接受其复测回报（补充观测走新运行）。"""
    _setup_west(db)
    run, hyp = _converge_to_w2_offset(db)
    corr = propose_correction(db, hyp.id, "站长")
    approve_correction(db, corr.id, "admin")
    db.commit()
    plan = db.query(RetestPlan).filter_by(run_id=run.id).first()
    with pytest.raises(ValueError, match="新的诊断运行"):
        submit_retest_report(db, plan.id, [
            {"device_id": "D-W2-01", "observed_value": 100.0, "meter_reading": 88.0},
        ], operator="站长")
