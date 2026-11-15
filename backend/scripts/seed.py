"""样例种子：实际触发题面要求的全部治理场景。

场景叙事（2026-11-10，UTC）：
- 干渠 MAIN 渠首 D-HEAD-01（累计表，1h 区间）：00:00-06:00 每小时 +100m3；
  次日设备补传修正 04:00 读数（400 -> 420），旧报文保留、事实与指标产生新版本与差异说明；
  其中 03:00 报文存在时钟漂移（接收滞后 45 分钟）。
- 支渠 B1 D-B1-01（累计表）：02:30 换表但无换表凭证，新表首读数仅 5（相对旧表 320 看似倒退）；
  事实编译器截断区间、关联量列待核实，绝不产生负供水。
- 支渠 B2 D-B2-01（瞬时表）：梯形积分；05:00 缺测（2h 空窗 > 1.5 倍采集区间）；
  一条报文单位错填 L 触发单位不一致；01:00-02:00 区间同时出现设备补传与人工补录，
  两份证据并存生成 candidate，未裁定前不进正式指标。
- 守恒：渠首确认量远大于两支渠可解释量（B1 大量待核实 + B2 候选未裁定），
  触发 CHANNEL_IMBALANCE，冻结 MAIN 损耗指标并生成对账任务，禁止计划量补平。
- 计划量只作为 target 事实与比对口径。

异常损耗诊断叙事（西干渠 WEST，2026-11-11 ~ 11-13，UTC）：
- 11-11 渠段额外渗漏：渠首 220/110 m³/h 两档流量，W2 后段停灌；残差超出允许区间。
  诊断试算并列 5 个候选（渠段渗漏 + 渠首/W1 表偏移与倍率），复测渠首与 W1 后收敛到
  「西干渠汇合段额外渗漏」，走工程处置闭环对账任务（真实水损失，不生成校正参数）。
- 11-12 单表漂移：W2 表过水时每小时少计约 12 m³（固定偏移），流量变化使倍率假设被
  数据排除；复测三块表后收敛到 W2 固定偏移。校正参数「待批准」期间遇换表登记而失效，
  重新提出并经管理员批准后生成追加式 adjustment 事实作用于核算（不改原读数）。
  随后一条迟到补传形成新的诊断运行（late_data），已采用的旧诊断不被覆盖。
- 11-13 证据不足：各表仅首尾两个读数（中间缺测，稀疏均摊），7 个候选同样成立；
  复测 W1 后排除 2 个仍剩 5 个，系统明确「证据不足，不能下结论」，对账任务保持冻结。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.db import Base, SessionLocal, engine
from app.models import (
    Adjudication, CorrectionParam, DiagnosticHypothesis, DiagnosticRun,
    MetricVersion, QualityEvent, ReconciliationTask, RetestPlan, SupplyFact,
)
from app.schemas import (
    AllocationIn, BatchIn, ChannelIn, ContractIn, DeviceMappingIn,
    ManualRevisionIn, MeterChangeIn, MeterPointIn, PlotIn, RawMessageIn,
)
from app.services import master_data as md
from app.services.diagnosis import (
    approve_correction, propose_correction, run_diagnosis, submit_retest_report,
)
from app.services.fact_compiler import compile_device, compile_plan_facts
from app.services.ingestion import ingest_manual, ingest_raw, register_contract
from app.services.metrics import publish_metrics
from app.services.quality import run_all_rules
from app.services.workflow import resolve_reconciliation

UTC = timezone.utc
D = datetime(2026, 11, 10, tzinfo=UTC)


def t(hour, minute=0, day=10):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def reset(db):
    db.rollback()
    db.execute(text(
        "TRUNCATE TABLE metric_versions, recompute_runs, reconciliation_tasks, "
        "adjudications, quality_events, supply_facts, meter_changes, "
        "correction_params, retest_reports, retest_plans, "
        "diagnostic_hypotheses, diagnostic_runs, "
        "plot_batch_allocations, device_mappings, mapping_versions, "
        "rotation_batches, plots, meter_points, channels, "
        "manual_revisions, raw_messages, data_contracts RESTART IDENTITY CASCADE"))
    db.commit()


def seed():
    db = SessionLocal()
    reset(db)

    # ---------------- 1. 数据契约 ----------------
    register_contract(db, ContractIn(
        code="CT-CUM-01", name="累计表契约", source_kind="device",
        reading_type="cumulative", unit="m3", device_id_pattern=r"D-[A-Z0-9-]+",
        collection_interval_sec=3600,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="fw-1.4", active_from=D))
    register_contract(db, ContractIn(
        code="CT-FLOW-01", name="瞬时流量契约", source_kind="device",
        reading_type="instantaneous", unit="m3_h", device_id_pattern=r"D-[A-Z0-9-]+",
        collection_interval_sec=3600,
        payload_schema={"reading": {"required": True}, "unit": {"required": True}},
        source_version="fw-1.4", active_from=D))
    register_contract(db, ContractIn(
        code="CT-MAN-01", name="人工抄表契约", source_kind="manual",
        reading_type="cumulative", unit="m3", device_id_pattern=r"D-[A-Z0-9-]+",
        collection_interval_sec=3600,
        payload_schema={"value": {"required": True}, "unit": {"required": True}},
        source_version="manual-form-v2", active_from=D))
    register_contract(db, ContractIn(
        code="CT-PLAN-01", name="轮灌计划契约", source_kind="plan",
        reading_type="plan", unit="m3", device_id_pattern=r"PLAN-.*",
        collection_interval_sec=86400,
        payload_schema={"planned_volume": {"required": True}},
        source_version="plan-2026.11", active_from=D))

    # ---------------- 2. 水系与地块主数据 ----------------
    md.create_channel(db, ChannelIn(code="MAIN", name="东干渠", level=1))
    md.create_channel(db, ChannelIn(code="B1", name="一支渠", level=2, parent_code="MAIN"))
    md.create_channel(db, ChannelIn(code="B2", name="二支渠", level=2, parent_code="MAIN"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-HEAD", name="渠首计量点", channel_code="MAIN", point_role="head"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-B1", name="一支渠计量点", channel_code="B1", point_role="branch"))
    md.create_meter_point(db, MeterPointIn(
        code="MP-B2", name="二支渠计量点", channel_code="B2", point_role="branch"))
    md.create_plot(db, PlotIn(code="PLOT-B1", name="一号地块", area_mu=120, channel_code="B1"))
    md.create_plot(db, PlotIn(code="PLOT-B2", name="二号地块", area_mu=180, channel_code="B2"))
    md.create_batch(db, BatchIn(
        code="ROT-2026-01", name="11月上旬第一轮灌",
        planned_start=t(0), planned_end=t(6)))

    v1 = md.publish_mapping_version(db, "初始映射版本")
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-HEAD-01", meter_point_code="MP-HEAD",
        effective_from=t(0)), v1)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-B1-01", meter_point_code="MP-B1",
        effective_from=t(0)), v1)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-B2-01", meter_point_code="MP-B2",
        effective_from=t(0)), v1)
    md.upsert_allocation(db, AllocationIn(
        plot_code="PLOT-B1", batch_code="ROT-2026-01", planned_volume=500,
        effective_from=t(0)), v1)
    md.upsert_allocation(db, AllocationIn(
        plot_code="PLOT-B2", batch_code="ROT-2026-01", planned_volume=600,
        effective_from=t(0)), v1)
    db.commit()

    # ---------------- 3. 换表缺证（02:30，无凭证） ----------------
    md.register_meter_change(db, MeterChangeIn(
        device_id="D-B1-01", meter_point_code="MP-B1", change_time=t(2, 30),
        old_last_reading=320, new_first_reading=5, voucher_present=False,
        note="巡检口头反映换表，未提供检定凭证"))
    db.commit()

    # ---------------- 4. 第一批设备报文（当日准点接收） ----------------
    # 渠首累计表：每小时 +100
    head_readings = {0: 0, 1: 100, 2: 200, 3: 300, 4: 400, 5: 500, 6: 600}
    for h, r in head_readings.items():
        recv = t(h) + timedelta(minutes=5)
        if h == 3:  # 时钟漂移：设备时钟偏快，事件时间比接收时间还靠前 35 分钟
            recv = t(h) - timedelta(minutes=35)
        ingest_raw(db, RawMessageIn(
            message_uid=f"MSG-HEAD-{h:02d}", contract_code="CT-CUM-01",
            device_id="D-HEAD-01", payload={"reading": r, "unit": "m3"},
            event_time=t(h), received_at=recv, source_version="fw-1.4"),
            batch="seed-day1")

    # 一支渠累计表：02:30 换表无凭证，新表从小读数重新累计（表面倒退）；
    # 换表后增量刻意偏小，与渠首量形成守恒缺口
    b1_series = [(0, 100), (1, 210), (2, 320), (3, 5), (4, 25), (5, 45), (6, 65)]
    for h, r in b1_series:
        ingest_raw(db, RawMessageIn(
            message_uid=f"MSG-B1-{h:02d}", contract_code="CT-CUM-01",
            device_id="D-B1-01", payload={"reading": r, "unit": "m3"},
            event_time=t(h), received_at=t(h) + timedelta(minutes=5),
            source_version="fw-1.4"), batch="seed-day1")

    # 二支渠瞬时表（m3/h）：05:00 缺测；02:00 报文单位错填 L
    b2_series = {0: 40, 1: 44, 2: 48, 3: 50, 4: 46, 6: 42}
    for h, r in b2_series.items():
        unit = "L" if h == 2 else "m3_h"
        ingest_raw(db, RawMessageIn(
            message_uid=f"MSG-B2-{h:02d}", contract_code="CT-FLOW-01",
            device_id="D-B2-01", payload={"reading": r, "unit": unit},
            event_time=t(h), received_at=t(h) + timedelta(minutes=5),
            source_version="fw-1.4"), batch="seed-day1")
    db.commit()

    # ---------------- 5. 首次编译 + 首次发布 ----------------
    compile_device(db, "D-HEAD-01", "cumulative")
    compile_device(db, "D-B1-01", "cumulative")
    compile_device(db, "D-B2-01", "instantaneous")
    compile_plan_facts(db)
    run_all_rules(db)
    db.commit()
    v1_metrics = publish_metrics(db, trigger="initial", note="首次发布", as_of=t(7))
    db.commit()
    print(f"[首次发布] 指标版本数={len(v1_metrics)}")

    # ---------------- 6. 次日：迟到补传 + 人工补录（证据冲突） ----------------
    # 6.1 渠首末日读数被设备补传修正 600 -> 620（旧报文保留不覆盖，累计总量随之变化）
    ingest_raw(db, RawMessageIn(
        message_uid="MSG-HEAD-06-REV2", contract_code="CT-CUM-01",
        device_id="D-HEAD-01", payload={"reading": 620, "unit": "m3"},
        event_time=t(6), received_at=t(10, day=12),
        source_version="fw-1.4", supersedes_uid="MSG-HEAD-06"),
        batch="seed-backfill")

    # 6.2 二支渠 02:00 同时收到设备补传（修正瞬时值）与人工补录（同一区间）
    ingest_raw(db, RawMessageIn(
        message_uid="MSG-B2-02-REV2", contract_code="CT-FLOW-01",
        device_id="D-B2-01", payload={"reading": 47, "unit": "m3_h"},
        event_time=t(2), received_at=t(11, day=12),
        source_version="fw-1.4", supersedes_uid="MSG-B2-02"),
        batch="seed-backfill")
    ingest_manual(db, ManualRevisionIn(
        revision_uid="MAN-B2-0130", contract_code="CT-MAN-01",
        device_id="D-B2-01", event_time=t(1, 30), value=55.0, unit="m3",
        basis="管水员现场比对便携式超声流量计读数", operator="张工",
        received_at=t(11, day=12)), batch="seed-manual")

    # 6.3 一条无依据人工补录（落在读数序列外，只触发 MANUAL_WITHOUT_BASIS，不生成事实）
    ingest_manual(db, ManualRevisionIn(
        revision_uid="MAN-B1-NOBASIS", contract_code="CT-MAN-01",
        device_id="D-B1-01", event_time=t(6, 30), value=30.0, unit="m3",
        basis="", operator="临时工",
        received_at=t(11, day=12)), batch="seed-manual")
    db.commit()

    # ---------------- 7. 重编译 + 重发布（新版本与差异说明） ----------------
    compile_device(db, "D-HEAD-01", "cumulative")
    compile_device(db, "D-B1-01", "cumulative")
    compile_device(db, "D-B2-01", "instantaneous")
    run_all_rules(db)
    db.commit()
    v2_metrics = publish_metrics(
        db, trigger="late_backfill",
        note="迟到补传/换表缺证/证据冲突后的重算发布", as_of=t(12, day=12))
    db.commit()
    print(f"[重算发布] 指标版本数={len(v2_metrics)}")

    # ================= 异常损耗诊断叙事（西干渠 WEST，11-11 ~ 11-13） =================
    def t2(day, hour, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=UTC)

    def west_raw(dev, day, h, reading, uid_suffix=""):
        ingest_raw(db, RawMessageIn(
            message_uid=f"MSG-{dev}-{day:02d}-{h:02d}{uid_suffix}",
            contract_code="CT-CUM-01", device_id=dev,
            payload={"reading": reading, "unit": "m3"},
            event_time=t2(day, h), received_at=t2(day, h) + timedelta(minutes=5),
            source_version="fw-1.4"), batch=f"seed-west-{day:02d}")

    # ---------------- 9. WEST 渠系主数据（含诊断参数） ----------------
    md.create_channel(db, ChannelIn(
        code="WEST", name="西干渠", level=1,
        normal_loss_min_ratio=0.005, normal_loss_max_ratio=0.03, travel_minutes=60))
    md.create_channel(db, ChannelIn(
        code="W1", name="西一支渠", level=2, parent_code="WEST", travel_minutes=60))
    md.create_channel(db, ChannelIn(
        code="W2", name="西二支渠", level=2, parent_code="WEST", travel_minutes=60))
    md.create_meter_point(db, MeterPointIn(
        code="MP-WH", name="西渠首计量点", channel_code="WEST", point_role="head",
        accuracy_ratio=0.02))
    md.create_meter_point(db, MeterPointIn(
        code="MP-W1", name="西一支渠计量点", channel_code="W1", point_role="branch",
        accuracy_ratio=0.02))
    md.create_meter_point(db, MeterPointIn(
        code="MP-W2", name="西二支渠计量点", channel_code="W2", point_role="branch",
        accuracy_ratio=0.02))
    md.create_plot(db, PlotIn(code="PLOT-W1", name="西一地块", area_mu=150,
                              channel_code="W1"))
    md.create_plot(db, PlotIn(code="PLOT-W2", name="西二地块", area_mu=200,
                              channel_code="W2"))
    md.create_batch(db, BatchIn(
        code="ROT-W-01", name="西渠 11 月中旬轮灌",
        planned_start=t2(11, 0), planned_end=t2(13, 6)))
    vw = md.publish_mapping_version(db, "WEST 渠系映射")
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-WH-01", meter_point_code="MP-WH",
        effective_from=t2(11, 0)), vw)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-W1-01", meter_point_code="MP-W1",
        effective_from=t2(11, 0)), vw)
    md.upsert_device_mapping(db, DeviceMappingIn(
        device_id="D-W2-01", meter_point_code="MP-W2",
        effective_from=t2(11, 0)), vw)
    md.upsert_allocation(db, AllocationIn(
        plot_code="PLOT-W1", batch_code="ROT-W-01", planned_volume=3000,
        effective_from=t2(11, 0)), vw)
    md.upsert_allocation(db, AllocationIn(
        plot_code="PLOT-W2", batch_code="ROT-W-01", planned_volume=2500,
        effective_from=t2(11, 0)), vw)
    db.commit()

    # ---------------- 10. 11-11：渠段额外渗漏 ----------------
    # 渠首两档流量（W2 停灌提前 1h 反映到渠首，输送时长 1h）；W1 恒定 90；W2 后段停灌
    for h, r in enumerate([1000, 1225, 1450, 1562, 1674, 1786, 1898]):
        west_raw("D-WH-01", 11, h, r)
    for h, r in enumerate([500, 590, 680, 770, 860, 950, 1040]):
        west_raw("D-W1-01", 11, h, r)
    for h, r in enumerate([2000, 2100, 2200, 2300, 2300, 2300, 2300]):
        west_raw("D-W2-01", 11, h, r)
    db.commit()
    for dev in ("D-WH-01", "D-W1-01", "D-W2-01"):
        compile_device(db, dev, "cumulative")
    run_all_rules(db)
    db.commit()
    publish_metrics(db, trigger="scheduled", as_of=t2(11, 8), note="11-11 西渠发布")
    db.commit()

    # 诊断 A：5 个候选并列（渠段渗漏 + 渠首/W1 表偏移与倍率；W2 被停灌时段排除）
    run_a = run_diagnosis(db, "WEST", t2(11, 0), t2(11, 6), t2(11, 8),
                          trigger="manual",
                          note="西干渠对账任务触发的异常损耗诊断（11-11 窗口）")
    db.commit()
    plan_a = db.scalar(select(RetestPlan).where(
        RetestPlan.run_id == run_a.id, RetestPlan.status == "proposed",
        RetestPlan.device_id == "D-WH-01"))
    # 现场回报：渠首表读 225 实测 224.5、W1 表读 90 实测 90.8（两表均正常）
    submit_retest_report(db, plan_a.id, [
        {"device_id": "D-WH-01", "observed_value": 224.5, "meter_reading": 225.0,
         "observed_at": t2(11, 10)},
        {"device_id": "D-W1-01", "observed_value": 90.8, "meter_reading": 90.0,
         "observed_at": t2(11, 10)},
    ], operator="王站长", equipment="便携式超声流量计", note="两块表均在精度内")
    db.commit()
    # 渗漏为真实水损失：不生成校正参数，走工程处置闭环对账任务
    rec_a = db.scalar(select(ReconciliationTask).where(
        ReconciliationTask.channel_code == "WEST",
        ReconciliationTask.status.in_(["open", "frozen"])))
    resolve_reconciliation(
        db, rec_a.id,
        "诊断确认为西干渠汇合段额外渗漏（约 22.7 m³/h），复测排除全部仪表假设；"
        "已安排停水堵漏维修（工程处置，不作计量校正）", "王站长")
    db.commit()
    print(f"[诊断A] 渠段渗漏：运行#{run_a.id} 收敛 -> segment_loss，对账任务#{rec_a.id} 已闭环")

    # ---------------- 11. 11-12：W2 表固定偏移（单表漂移） ----------------
    # W2 真值 100x3 + 30x3，表过水时每小时少计约 12 -> 88x3 + 18x3；
    # 渠首按输送时长提前 1 个 slot 反映 W2 降量（194x2 + 122x4）
    for h, r in enumerate([1898, 2092, 2286, 2408, 2530, 2652, 2774]):
        west_raw("D-WH-01", 12, h, r)
    for h, r in enumerate([1040, 1130, 1220, 1310, 1400, 1490, 1580]):
        west_raw("D-W1-01", 12, h, r)
    for h, r in enumerate([2300, 2388, 2476, 2564, 2582, 2600, 2618]):
        west_raw("D-W2-01", 12, h, r)
    db.commit()
    for dev in ("D-WH-01", "D-W1-01", "D-W2-01"):
        compile_device(db, dev, "cumulative")
    run_all_rules(db)
    db.commit()
    publish_metrics(db, trigger="scheduled", as_of=t2(12, 8), note="11-12 西渠发布")
    db.commit()

    # 诊断 B：6 个候选（W2 倍率被流量变化排除）；复测三块表后收敛到 W2 固定偏移
    run_b = run_diagnosis(db, "WEST", t2(12, 0), t2(12, 6), t2(12, 8),
                          trigger="manual",
                          note="11-12 窗口：W2 表读数系统性偏小")
    db.commit()
    plan_b = db.scalar(select(RetestPlan).where(
        RetestPlan.run_id == run_b.id, RetestPlan.status == "proposed",
        RetestPlan.device_id == "D-WH-01"))
    submit_retest_report(db, plan_b.id, [
        {"device_id": "D-WH-01", "observed_value": 193.5, "meter_reading": 194.0,
         "observed_at": t2(12, 10)},
        {"device_id": "D-W1-01", "observed_value": 90.1, "meter_reading": 90.0,
         "observed_at": t2(12, 10)},
        {"device_id": "D-W2-01", "observed_value": 99.8, "meter_reading": 88.0,
         "observed_at": t2(12, 10)},
    ], operator="王站长", equipment="便携式超声流量计",
        note="渠首/W1 正常；W2 表读 88 实测 99.8，确认固定偏移")
    db.commit()
    w2_offset_hyp = db.scalar(select(DiagnosticHypothesis).where(
        DiagnosticHypothesis.run_id == run_b.id,
        DiagnosticHypothesis.hypothesis_kind == "meter_offset",
        DiagnosticHypothesis.target == "D-W2-01"))
    corr_c1 = propose_correction(db, w2_offset_hyp.id, "王站长")
    db.commit()
    print(f"[诊断B] 单表漂移：运行#{run_b.id} 收敛 -> meter_offset@D-W2-01，"
          f"校正参数#{corr_c1.id} 待批准")

    # 迟到补传（11-12 10:00 到达，晚于诊断截止 08:00）：W2 06:00 读数 2618 -> 2610
    ingest_raw(db, RawMessageIn(
        message_uid="MSG-D-W2-01-12-06-REV2", contract_code="CT-CUM-01",
        device_id="D-W2-01", payload={"reading": 2610, "unit": "m3"},
        event_time=t2(12, 6), received_at=t2(12, 10),
        source_version="fw-1.4", supersedes_uid="MSG-D-W2-01-12-06"),
        batch="seed-west-late")
    db.commit()
    compile_device(db, "D-W2-01", "cumulative")
    run_all_rules(db)
    db.commit()
    # 迟到数据形成新分析；旧诊断已采用（校正待批准），不被覆盖
    run_b2 = run_diagnosis(db, "WEST", t2(12, 0), t2(12, 6), t2(12, 12),
                           trigger="late_data",
                           note="迟到补传（W2 06:00 修订）形成的新分析")
    publish_metrics(db, trigger="late_backfill", as_of=t2(12, 12),
                    note="W2 迟到补传后重发布")
    db.commit()
    print(f"[迟到数据] 新诊断运行#{run_b2.id}（旧运行#{run_b.id} 状态保持 "
          f"{db.get(DiagnosticRun, run_b.id).status}）")

    # ---------------- 12. 设备资料变更 -> 待批准校正失效 -> 重新提出 -> 批准 ----------------
    md.register_meter_change(db, MeterChangeIn(
        device_id="D-W2-01", meter_point_code="MP-W2", change_time=t2(13, 0, 30),
        old_last_reading=2618, new_first_reading=2618, voucher_present=True,
        voucher_ref="检定证书 JC-2026-118",
        note="漂移表更换为检定合格表（无缝换表）"))
    db.commit()
    db.refresh(corr_c1)
    print(f"[设备变更] 换表登记后校正参数#{corr_c1.id} 状态 -> {corr_c1.status}")
    assert corr_c1.status == "invalidated"
    corr_c2 = propose_correction(db, w2_offset_hyp.id, "王站长")
    approve_correction(db, corr_c2.id, "admin")
    db.commit()
    print(f"[校正批准] 校正参数#{corr_c2.id} 批准，"
          f"生成 {len(corr_c2.affected_intervals)} 条 adjustment 事实")

    # ---------------- 13. 11-13：证据不足（稀疏读数） ----------------
    west_raw("D-WH-01", 13, 0, 2774)
    west_raw("D-WH-01", 13, 6, 3854)
    west_raw("D-W1-01", 13, 0, 1580)
    west_raw("D-W1-01", 13, 6, 2120)
    west_raw("D-W2-01", 13, 0, 2618)
    west_raw("D-W2-01", 13, 6, 3038)
    db.commit()
    for dev in ("D-WH-01", "D-W1-01", "D-W2-01"):
        compile_device(db, dev, "cumulative")
    run_all_rules(db)
    db.commit()
    publish_metrics(db, trigger="scheduled", as_of=t2(13, 8),
                    note="11-13 西渠发布（含 W2 校正调整量）")
    db.commit()

    # 诊断 C：仅首尾读数（稀疏均摊），7 个候选；复测 W1 排除 2 个仍剩 5 个
    run_c = run_diagnosis(db, "WEST", t2(13, 0), t2(13, 6), t2(13, 8),
                          trigger="manual",
                          note="11-13 窗口：各表仅首尾读数，中间缺测")
    db.commit()
    plan_c = db.scalar(select(RetestPlan).where(
        RetestPlan.run_id == run_c.id, RetestPlan.status == "proposed",
        RetestPlan.device_id == "D-W1-01"))
    submit_retest_report(db, plan_c.id, [
        {"device_id": "D-W1-01", "observed_value": 89.7, "meter_reading": 90.0,
         "observed_at": t2(13, 10)},
    ], operator="王站长", equipment="便携式超声流量计", note="W1 表正常")
    db.commit()
    db.refresh(run_c)
    remaining_c = db.scalars(select(DiagnosticHypothesis).where(
        DiagnosticHypothesis.run_id == run_c.id,
        DiagnosticHypothesis.status.in_(["candidate", "supported"]))).all()
    print(f"[诊断C] 证据不足：运行#{run_c.id} 复测后仍剩 {len(remaining_c)} 个候选，"
          f"状态={run_c.status}（不能下结论）")

    # ---------------- 8. 场景自检摘要 ----------------
    rules = {}
    for ev in db.query(QualityEvent).all():
        rules.setdefault(ev.rule_code, []).append(ev)
    print("\n=== 质量事件触发情况 ===")
    for code, evs in sorted(rules.items()):
        print(f"  {code}: {len(evs)} 条")

    print("\n=== 事实账状态分布（活动事实） ===")
    from app.services.ledger import superseded_fact_ids
    dead = superseded_fact_ids(db)
    status_cnt = {}
    for f in db.query(SupplyFact).all():
        if f.id in dead:
            continue
        status_cnt[f"{f.fact_kind}/{f.status}"] = status_cnt.get(
            f"{f.fact_kind}/{f.status}", 0) + 1
    for k, v in sorted(status_cnt.items()):
        print(f"  {k}: {v}")

    print("\n=== 待裁定冲突 ===")
    for a in db.query(Adjudication).all():
        print(f"  #{a.id} {a.conflict_key} -> {a.decision}")

    print("\n=== 对账任务（冻结） ===")
    for r in db.query(ReconciliationTask).all():
        print(f"  #{r.id} {r.channel_code} 上游={r.upstream_value} "
              f"下游可解释={r.downstream_explained} 差异率={r.diff_ratio} [{r.status}]")

    print("\n=== 异常损耗诊断运行 ===")
    for r in db.query(DiagnosticRun).order_by(DiagnosticRun.id).all():
        hyps = db.scalars(select(DiagnosticHypothesis).where(
            DiagnosticHypothesis.run_id == r.id)).all()
        cands = [h for h in hyps if h.status in ("candidate", "supported", "adopted")]
        print(f"  #{r.id} {r.channel_code} {r.window_start.date()} "
              f"[{r.status}] 触发={r.trigger} 容差外残差={r.unexplained_residual} "
              f"候选={len(cands)}/{len(hyps)}")
        for h in hyps:
            mark = {"candidate": "候选", "supported": "支持",
                    "refuted": "排除", "adopted": "采纳"}.get(h.status, h.status)
            print(f"      [{mark}] {h.hypothesis_kind}@{h.target} "
                  f"参数={h.params} 可解释时段={len(h.explained)} "
                  f"不相容={len(h.unexplained)}")

    print("\n=== 复测建议与回报 ===")
    from app.models import RetestReport
    for p in db.query(RetestPlan).order_by(RetestPlan.id).all():
        print(f"  运行#{p.run_id} {p.position}({p.device_id}) "
              f"可区分假设对={p.discriminating_score} [{p.status}]")
    for rep in db.query(RetestReport).order_by(RetestReport.id).all():
        print(f"  回报@运行#{rep.run_id} 候选 {len(rep.candidates_before)} -> "
              f"{len(rep.candidates_after)} ({rep.operator})")

    print("\n=== 校正参数 ===")
    for c in db.query(CorrectionParam).order_by(CorrectionParam.id).all():
        total = round(sum(iv["delta_m3"] for iv in c.affected_intervals), 3)
        print(f"  #{c.id} {c.kind}@{c.device_id} [{c.status}] "
              f"受影响区间={len(c.affected_intervals)} 段 合计差额={total} m³ "
              f"{c.invalidate_reason}")

    print("\n=== 指标版本（最新） ===")
    latest = [m for m in db.query(MetricVersion).all() if m.superseded_by is None]
    for m in sorted(latest, key=lambda x: (x.metric_date, x.grain_type, x.grain_key)):
        print(f"  v{m.version_no} {m.grain_type}:{m.grain_key} "
              f"计划={m.planned_volume} 确认={m.confirmed_volume} "
              f"待核实={m.pending_volume} 校正={m.correction_volume} "
              f"缺测率={m.missing_rate} "
              f"完成率={m.plan_completion_rate} 损耗=[{m.loss_lower},{m.loss_upper}] "
              f"[{m.status}] 差异说明: {m.diff_note}")

    db.close()
    print("\n种子数据写入完成。")


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    seed()
