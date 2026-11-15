"""异常损耗诊断与复测安排引擎。

职责与纪律（对应题面硬约束）：
1. 输入正常损耗区间（渠道参数）、仪表精度（计量点参数）、连续读数（暂存区，
   受观测截止点约束），按输送时长对齐上下游，在连接处建立水量平衡，
   只对误差允许区间之外的残差定位；
2. 分别试算候选仪表的固定偏移、倍率异常、候选渠段的额外损耗与时钟偏差，
   每个假设列出能解释的观测与仍不相容部分；多种解释同样成立时并列保存，
   绝不凭最小残差自动判定故障；
3. 复测建议按"在该位置补一段读数能区分多少候选假设对"排序，给出具体位置、
   时间窗与所需设备；现场回报后重新计算，未支持的假设被排除；
4. 诊断估计与正式实灌分离：引擎只读暂存区读数，不写事实账；
   校正参数必须批准才生成追加式 adjustment 事实作用于核算，不直接改原读数；
5. 诊断运行固定观测截止点；迟到数据形成新分析（新运行），旧结果不覆盖已采用诊断；
   设备资料变更（映射调整/换表）使待批准校正失效。
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    CORR_APPROVED, CORR_INVALIDATED, CORR_PENDING, CORR_REJECTED,
    HYP_ADOPTED, HYP_CANDIDATE, HYP_CLOCK_SKEW, HYP_METER_GAIN,
    HYP_METER_OFFSET, HYP_REFUTED, HYP_SEGMENT_LOSS, HYP_SUPPORTED,
    RUN_ADOPTED, RUN_CONVERGED, RUN_INCONCLUSIVE, RUN_OPEN,
    Channel, CorrectionParam, DeviceMapping, DiagnosticHypothesis,
    DiagnosticRun, MeterPoint, RawMessage, RetestPlan, RetestReport,
    SupplyFact,
)

PORTABLE_ACCURACY = 0.01   # 便携式流量计比对精度 1%
CLOCK_SHIFT_SLOTS = (-2, -1, 1, 2)  # 时钟偏差试算的时移候选（slot 数）


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 读数序列
def _device_series(db: Session, device_id: str, cutoff: datetime) -> list[tuple]:
    """观测截止点之前接收的有效读数序列（同 event_time 取最高 revision）。"""
    msgs = db.scalars(select(RawMessage).where(
        RawMessage.device_id == device_id,
        RawMessage.received_at <= cutoff).order_by(
        RawMessage.event_time, RawMessage.revision_no)).all()
    bucket: dict[datetime, tuple] = {}
    for m in msgs:
        cur = bucket.get(m.event_time)
        if cur is None or m.revision_no > cur[1]:
            bucket[m.event_time] = (float(m.payload.get("reading", 0.0)), m.revision_no)
    return [(t, v[0]) for t, v in sorted(bucket.items())]


def _slot_volumes(series: list[tuple], reading_type: str, window_start: datetime,
                  slot_minutes: int, n_slots: int) -> tuple[list, list]:
    """相邻读数差量/积分按时间跨度均摊到 slot 网格。

    返回 (vols, estimated)：vols[k] 为 slot k 的水量（无覆盖为 None）；
    estimated[k]=True 表示该 slot 来自大跨度均摊（读数稀疏，证据强度低）。
    """
    vols: list[float | None] = [None] * n_slots
    estimated = [False] * n_slots
    slot_sec = slot_minutes * 60.0
    for (t0, r0), (t1, r1) in zip(series, series[1:]):
        span = (t1 - t0).total_seconds()
        if span <= 0:
            continue
        if reading_type == "cumulative":
            total = r1 - r0
            if total < 0:
                continue  # 读数倒退段由质量规则管辖，诊断不解释
        else:
            total = (r0 + r1) / 2.0 * span / 3600.0
        rate = total / span
        sparse = span > 2 * slot_sec  # 跨度超过 2 个 slot：稀疏均摊
        for k in range(n_slots):
            s0 = window_start + timedelta(seconds=k * slot_sec)
            s1 = s0 + timedelta(seconds=slot_sec)
            overlap = (min(t1, s1) - max(t0, s0)).total_seconds()
            if overlap > 0:
                vols[k] = round((vols[k] or 0.0) + rate * overlap, 3)
                if sparse:
                    estimated[k] = True
    return vols, estimated


# ---------------------------------------------------------------- 拓扑与平衡
def _resolve_devices(db: Session, channel_code: str, at: datetime) -> dict:
    """解析汇合点拓扑：上游计量点设备 + 各子渠道下游计量点设备。"""
    points = {p.code: p for p in db.scalars(select(MeterPoint)).all()}
    channels = {c.code: c for c in db.scalars(select(Channel)).all()}
    parent = channels.get(channel_code)
    if not parent:
        raise ValueError(f"渠道 {channel_code} 不存在")

    def device_for(point_code: str) -> str | None:
        dm = db.scalar(select(DeviceMapping).where(
            DeviceMapping.meter_point_code == point_code,
            DeviceMapping.effective_from <= at,
            (DeviceMapping.effective_to.is_(None)) |
            (DeviceMapping.effective_to > at)).limit(1))
        return dm.device_id if dm else None

    upstream = None
    for p in points.values():
        if p.channel_code == channel_code and p.point_role in ("head", "transfer"):
            dev = device_for(p.code)
            if dev:
                upstream = {"point": p, "device_id": dev}
            break
    downstream = []
    for c in channels.values():
        if c.parent_code != channel_code:
            continue
        for p in points.values():
            if p.channel_code == c.code and p.point_role in ("branch", "field"):
                dev = device_for(p.code)
                if dev:
                    downstream.append({"channel": c, "point": p, "device_id": dev})
                break
    if not upstream:
        raise ValueError(f"渠道 {channel_code} 缺少渠首/交接计量点或设备映射")
    if not downstream:
        raise ValueError(f"渠道 {channel_code} 缺少下游支渠计量点")
    return {"parent": parent, "upstream": upstream, "downstream": downstream}


def _reading_types(db: Session) -> dict:
    from ..models import DataContract, RawMessage as RM
    out = {}
    for dev, ccode in db.execute(
            select(RM.device_id, RM.contract_code).distinct()).all():
        c = db.scalar(select(DataContract).where(DataContract.code == ccode))
        out[dev] = c.reading_type if c else "cumulative"
    return out


def _build_balance(db: Session, topo: dict, window_start: datetime,
                   slot_minutes: int, n_slots: int, cutoff: datetime,
                   reading_types: dict) -> dict:
    """分桶 + 输送时长对齐 + 逐时段水量平衡与允许区间。"""
    parent = topo["parent"]
    up = topo["upstream"]
    slot_sec = slot_minutes * 60.0

    def series_of(device_id):
        return _device_series(db, device_id, cutoff)

    up_vols, up_est = _slot_volumes(
        series_of(up["device_id"]), reading_types.get(up["device_id"], "cumulative"),
        window_start, slot_minutes, n_slots)

    down_series: list[dict] = []
    for d in topo["downstream"]:
        vols, est = _slot_volumes(
            series_of(d["device_id"]),
            reading_types.get(d["device_id"], "cumulative"),
            window_start, slot_minutes, n_slots)
        shift = max(round(d["channel"].travel_minutes / slot_minutes), 0)
        down_series.append({**d, "vols": vols, "estimated": est, "shift": shift})

    loss_min = parent.normal_loss_min_ratio
    loss_max = parent.normal_loss_max_ratio
    acc_up = up["point"].accuracy_ratio

    slots: list[dict] = []
    for tau in range(n_slots):
        u = up_vols[tau] if tau < len(up_vols) else None
        if u is None or u <= 0:
            continue
        downs: dict[str, float] = {}
        complete = True
        for d in down_series:
            k = tau + d["shift"]
            v = d["vols"][k] if 0 <= k < n_slots else None
            if v is None:
                complete = False
                break
            downs[d["device_id"]] = v
        if not complete:
            continue
        down_total = sum(downs.values())
        r = u - down_total
        sigma = acc_up * u + sum(
            d["point"].accuracy_ratio * downs[d["device_id"]] for d in down_series)
        lo = loss_min * u - sigma
        hi = loss_max * u + sigma
        excess = 0.0
        if r > hi:
            excess = r - hi
        elif r < lo:
            excess = r - lo
        slots.append({
            "tau": tau,
            "start": (window_start + timedelta(seconds=tau * slot_sec)).isoformat(),
            "end": (window_start + timedelta(seconds=(tau + 1) * slot_sec)).isoformat(),
            "up": round(u, 3), "down": {k: round(v, 3) for k, v in downs.items()},
            "down_total": round(down_total, 3),
            "residual": round(r, 3), "allow_lo": round(lo, 3), "allow_hi": round(hi, 3),
            "excess": round(excess, 3),
            "estimated": up_est[tau] or any(
                d["estimated"][min(tau + d["shift"], n_slots - 1)] for d in down_series),
        })
    return {"slots": slots, "up_vols": up_vols, "down_series": down_series,
            "loss_min": loss_min, "loss_max": loss_max, "acc_up": acc_up}


# ---------------------------------------------------------------- 假设试算
def _fit_constant(slots: list[dict]) -> tuple[float, list, list]:
    """固定偏移/渠段损耗：求 δ 使 lo <= r-δ <= hi 对所有时段成立。

    返回 (δ̂, explained, unexplained)。交集为空时取各时段所需 δ 的中位数，
    应用后仍超差的时段即"仍不相容部分"。
    """
    lo_b = max(s["residual"] - s["allow_hi"] for s in slots)
    hi_b = min(s["residual"] - s["allow_lo"] for s in slots)
    if lo_b <= hi_b:
        delta = (lo_b + hi_b) / 2
        return delta, [s["tau"] for s in slots if abs(s["excess"]) > 1e-9], []
    mids = sorted(s["residual"] - (s["allow_lo"] + s["allow_hi"]) / 2 for s in slots)
    delta = mids[len(mids) // 2]
    explained, unexplained = [], []
    for s in slots:
        r2 = s["residual"] - delta
        if s["allow_lo"] <= r2 <= s["allow_hi"]:
            if abs(s["excess"]) > 1e-9:
                explained.append(s["tau"])
        else:
            unexplained.append({"tau": s["tau"], "start": s["start"],
                                "residual_after": round(r2, 3),
                                "allow": [s["allow_lo"], s["allow_hi"]]})
    return delta, explained, unexplained


def _fit_gain(slots: list[dict], vol_of) -> tuple[float, list, list] | None:
    """倍率异常：r'(τ) = r(τ) − θ·v(τ)，v 为该表该时段水量。

    表在某时段无水量（停灌）时校正项不起作用：该时段仍超差则假设不相容。
    """
    lo_b, hi_b = float("-inf"), float("inf")
    for s in slots:
        v = vol_of(s)
        if v is None or v <= 1e-9:
            if not (s["allow_lo"] <= s["residual"] <= s["allow_hi"]):
                return None  # 停灌时段无法由该表倍率解释
            continue
        lo_b = max(lo_b, (s["residual"] - s["allow_hi"]) / v)
        hi_b = min(hi_b, (s["residual"] - s["allow_lo"]) / v)
    if lo_b > hi_b:
        return None
    theta = (lo_b + hi_b) / 2
    explained, unexplained = [], []
    for s in slots:
        v = vol_of(s) or 0.0
        r2 = s["residual"] - theta * v
        if s["allow_lo"] <= r2 <= s["allow_hi"]:
            if abs(s["excess"]) > 1e-9:
                explained.append(s["tau"])
        else:
            unexplained.append({"tau": s["tau"], "start": s["start"],
                                "residual_after": round(r2, 3),
                                "allow": [s["allow_lo"], s["allow_hi"]]})
    if unexplained:
        return None
    return theta, explained, unexplained


def _fit_offset_for_device(slots: list[dict], vol_of) -> tuple[float, list, list] | None:
    """仪表固定偏移：仅在该表过水时段作用（停灌时段表偏移不发生）。"""
    lo_b, hi_b = float("-inf"), float("inf")
    for s in slots:
        v = vol_of(s)
        if v is None or v <= 1e-9:
            if not (s["allow_lo"] <= s["residual"] <= s["allow_hi"]):
                return None  # 停灌时段残差仍在，表偏移无法解释
            continue
        lo_b = max(lo_b, s["residual"] - s["allow_hi"])
        hi_b = min(hi_b, s["residual"] - s["allow_lo"])
    if lo_b > hi_b:
        return None
    delta = (lo_b + hi_b) / 2
    explained, unexplained = [], []
    for s in slots:
        v = vol_of(s) or 0.0
        r2 = s["residual"] - (delta if v > 1e-9 else 0.0)
        if s["allow_lo"] <= r2 <= s["allow_hi"]:
            if abs(s["excess"]) > 1e-9:
                explained.append(s["tau"])
        else:
            unexplained.append({"tau": s["tau"], "start": s["start"],
                                "residual_after": round(r2, 3),
                                "allow": [s["allow_lo"], s["allow_hi"]]})
    if unexplained:
        return None
    return delta, explained, unexplained


def _residues_with_shift(balance: dict, device_id: str, shift_slots: int,
                         slot_minutes: int, n_slots: int,
                         window_start: datetime) -> list[dict] | None:
    """时钟偏差试算：把指定设备序列时移后重建平衡，返回新时段列表。"""
    up_vols = balance["up_vols"]
    down_series = []
    for d in balance["down_series"]:
        vols = d["vols"]
        if d["device_id"] == device_id:
            vols = [None] * n_slots
            src = balance["down_series_raw"][device_id]
            for k in range(n_slots):
                j = k - shift_slots
                if 0 <= j < n_slots:
                    vols[k] = src[j]
        down_series.append({**d, "vols": vols})
    up = up_vols
    if balance["up_device"] == device_id:
        up = [None] * n_slots
        for k in range(n_slots):
            j = k - shift_slots
            if 0 <= j < n_slots:
                up[k] = up_vols[j]
    parent_loss_min, parent_loss_max = balance["loss_min"], balance["loss_max"]
    slot_sec = slot_minutes * 60.0
    slots = []
    for tau in range(n_slots):
        u = up[tau] if tau < len(up) else None
        if u is None or u <= 0:
            continue
        downs, complete = {}, True
        for d in down_series:
            k = tau + d["shift"]
            v = d["vols"][k] if 0 <= k < n_slots else None
            if v is None:
                complete = False
                break
            downs[d["device_id"]] = v
        if not complete:
            continue
        r = u - sum(downs.values())
        sigma = balance["acc_up"] * u + sum(
            d["point"].accuracy_ratio * downs[d["device_id"]] for d in down_series)
        lo = parent_loss_min * u - sigma
        hi = parent_loss_max * u + sigma
        slots.append({"tau": tau,
                      "start": (window_start + timedelta(seconds=tau * slot_sec)).isoformat(),
                      "residual": round(r, 3), "allow_lo": round(lo, 3),
                      "allow_hi": round(hi, 3),
                      "excess": round(r - hi if r > hi else (r - lo if r < lo else 0.0), 3)})
    return slots


# ---------------------------------------------------------------- 诊断运行
def run_diagnosis(db: Session, channel_code: str, window_start: datetime,
                  window_end: datetime, observation_cutoff: datetime,
                  trigger: str = "manual", slot_minutes: int = 60,
                  note: str = "") -> DiagnosticRun:
    """执行一次诊断分析（快照）。观测截止点之后到达的数据不参与本运行。"""
    at = window_start + (window_end - window_start) / 2
    topo = _resolve_devices(db, channel_code, at)
    n_slots = max(int((window_end - window_start).total_seconds() // (slot_minutes * 60)), 1)
    reading_types = _reading_types(db)
    balance = _build_balance(db, topo, window_start, slot_minutes, n_slots,
                             observation_cutoff, reading_types)
    slots = balance["slots"]
    if not slots:
        raise ValueError("窗口内无完整对齐时段（观测不足），无法建立水量平衡")

    # 原始序列留档（供时钟偏差试算时移）
    balance["down_series_raw"] = {d["device_id"]: d["vols"] for d in balance["down_series"]}
    balance["up_device"] = topo["upstream"]["device_id"]

    unexplained = round(sum(s["excess"] for s in slots), 3)
    up_total = round(sum(s["up"] for s in slots), 3)
    down_total = round(sum(s["down_total"] for s in slots), 3)
    sparse = any(s["estimated"] for s in slots)

    run = DiagnosticRun(
        run_uid=_uid("diag"), channel_code=channel_code,
        window_start=window_start, window_end=window_end,
        observation_cutoff=observation_cutoff, trigger=trigger,
        status=RUN_OPEN, upstream_total=up_total, downstream_total=down_total,
        residual_total=round(up_total - down_total, 3),
        unexplained_residual=unexplained, slot_minutes=slot_minutes,
        inputs_snapshot={
            "loss_ratio_range": [balance["loss_min"], balance["loss_max"]],
            "accuracy": {topo["upstream"]["device_id"]: balance["acc_up"],
                         **{d["device_id"]: d["point"].accuracy_ratio
                            for d in topo["downstream"]}},
            "travel_minutes": {channel_code: topo["parent"].travel_minutes,
                               **{d["channel"].code: d["channel"].travel_minutes
                                  for d in topo["downstream"]}},
            "upstream_device": topo["upstream"]["device_id"],
            "downstream_devices": [d["device_id"] for d in topo["downstream"]],
            "sparse_readings": sparse,
            "slots": slots,
        },
        note=note)
    db.add(run)
    db.flush()

    if all(abs(s["excess"]) < 1e-9 for s in slots):
        run.note = (note + " " if note else "") + \
            "窗口内水量平衡均在允许区间内，无不可解释残差。"
        db.flush()
        return run
    if sparse:
        run.note = (note + " " if note else "") + \
            "注意：部分时段来自稀疏读数均摊（中间缺测），证据强度低，结论需复测确认。"

    hypotheses = _generate_hypotheses(db, run, topo, balance, slots,
                                      slot_minutes, n_slots, window_start)
    _generate_retest_plans(db, run, hypotheses, balance)
    db.flush()
    return run


def _generate_hypotheses(db: Session, run: DiagnosticRun, topo: dict,
                         balance: dict, slots: list[dict],
                         slot_minutes: int, n_slots: int,
                         window_start: datetime) -> list[DiagnosticHypothesis]:
    """逐类试算并全部落库：候选（完全相容）与被数据排除的（含不相容部分）都保留。"""
    up_dev = topo["upstream"]["device_id"]
    down_devices = [d["device_id"] for d in topo["downstream"]]
    hyps: list[DiagnosticHypothesis] = []

    def add(kind, target_kind, target, params, explained, unexplained):
        h = DiagnosticHypothesis(
            run_id=run.id, hypothesis_kind=kind, target_kind=target_kind,
            target=target, params={k: round(v, 4) if isinstance(v, float) else v
                                   for k, v in params.items()},
            explained=explained, unexplained=unexplained,
            residual_after=round(sum(abs(u["residual_after"]) for u in unexplained), 3)
            if unexplained else 0.0,
            is_candidate=not unexplained,
            status=HYP_CANDIDATE if not unexplained else HYP_REFUTED)
        if unexplained:
            h.refute_reason = "试算后仍存在不相容时段，被观测数据排除"
        db.add(h)
        hyps.append(h)
        return h

    def vol_of(device_id):
        if device_id == up_dev:
            return lambda s: s["up"]
        return lambda s: s["down"].get(device_id)

    # 1) 候选仪表固定偏移
    for dev in [up_dev] + down_devices:
        fit = _fit_offset_for_device(slots, vol_of(dev))
        if fit:
            delta, explained, unexplained = fit
            add(HYP_METER_OFFSET, "device", dev,
                {"offset_m3_per_slot": delta}, explained, unexplained)
    # 2) 候选仪表倍率异常
    for dev in [up_dev] + down_devices:
        fit = _fit_gain(slots, vol_of(dev))
        if fit:
            theta, explained, unexplained = fit
            add(HYP_METER_GAIN, "device", dev,
                {"gain_ratio": theta}, explained, unexplained)
    # 3) 候选渠段额外损耗（主干汇合段）
    delta, explained, unexplained = _fit_constant(slots)
    add(HYP_SEGMENT_LOSS, "segment", run.channel_code,
        {"extra_loss_m3_per_slot": delta}, explained, unexplained)
    # 4) 时钟偏差（时移试算；恒定序列时移无信息，跳过不可检验情形）
    base_res = [s["residual"] for s in slots]
    for dev in [up_dev] + down_devices:
        for shift in CLOCK_SHIFT_SLOTS:
            new_slots = _residues_with_shift(balance, dev, shift, slot_minutes,
                                             n_slots, window_start)
            if not new_slots or len(new_slots) < len(slots):
                continue
            new_res = [s["residual"] for s in new_slots]
            if len(new_res) == len(base_res) and all(
                    abs(a - b) < 1e-9 for a, b in zip(new_res, base_res)):
                continue  # 序列恒定，时移前后残差不变，时钟假设不可检验
            bad = [s for s in new_slots if abs(s["excess"]) > 1e-9]
            if not bad:
                add(HYP_CLOCK_SKEW, "device", dev,
                    {"shift_slots": shift, "shift_minutes": shift * slot_minutes},
                    [s["tau"] for s in new_slots], [])
            elif len(bad) < len(new_slots):
                add(HYP_CLOCK_SKEW, "device", dev,
                    {"shift_slots": shift, "shift_minutes": shift * slot_minutes},
                    [s["tau"] for s in new_slots if abs(s["excess"]) <= 1e-9],
                    [{"tau": s["tau"], "start": s["start"],
                      "residual_after": s["residual"],
                      "allow": [s["allow_lo"], s["allow_hi"]]} for s in bad[:4]])
    db.flush()
    return hyps


# ---------------------------------------------------------------- 复测建议
def _predictions_for_point(hypotheses: list[DiagnosticHypothesis],
                           device_id: str, meter_reading: float, tol: float,
                           upstream_devices: set) -> dict:
    """各候选假设对该设备处真值流量的预测区间 {hyp_id: (lo, hi, label)}。

    meter_reading 为观测时刻该表 concurrent 读数流量；与表无关的假设
    （渠段损耗/时钟偏差/其他表）预测真值 = 表读数（表在精度内可信）。
    上游表多计 => 真值 = 读数 − 校正量；下游表少计 => 真值 = 读数 + 校正量。
    """
    out = {}
    for h in hypotheses:
        if h.status not in (HYP_CANDIDATE, HYP_SUPPORTED):
            continue
        pred = meter_reading
        if h.target_kind == "device" and h.target == device_id:
            sign = -1.0 if device_id in upstream_devices else 1.0
            if h.hypothesis_kind == HYP_METER_OFFSET:
                pred = meter_reading + sign * h.params.get("offset_m3_per_slot", 0.0)
            elif h.hypothesis_kind == HYP_METER_GAIN:
                pred = meter_reading * (1.0 + sign * h.params.get("gain_ratio", 0.0))
            # 时钟偏差不改变流量真值：pred = meter_reading
        out[h.id] = (round(pred - tol, 3), round(pred + tol, 3),
                     f"{h.hypothesis_kind}@{h.target}")
    return out


def _generate_retest_plans(db: Session, run: DiagnosticRun,
                           hypotheses: list[DiagnosticHypothesis],
                           balance: dict) -> list[RetestPlan]:
    """按"补一段读数可区分的候选假设对数"为每个可复测位置评分并给出建议。"""
    cands = [h for h in hypotheses if h.status in (HYP_CANDIDATE, HYP_SUPPORTED)]
    plans: list[RetestPlan] = []
    if len(cands) < 2:
        return plans
    slots = run.inputs_snapshot["slots"]
    devices = ([balance["up_device"]] if "up_device" in balance else []) + \
        list(balance.get("down_series_raw", {}).keys())
    if not devices:
        devices = run.inputs_snapshot["downstream_devices"] + \
            [run.inputs_snapshot["upstream_device"]]
    acc_map = run.inputs_snapshot["accuracy"]
    upstream_devices = {run.inputs_snapshot["upstream_device"]}

    # 关闭旧的未执行建议（候选集合已变化）
    for old in db.scalars(select(RetestPlan).where(
            RetestPlan.run_id == run.id, RetestPlan.status == "proposed")).all():
        old.status = "closed"

    for dev in devices:
        vals = []
        for s in slots:
            v = s["up"] if dev == run.inputs_snapshot["upstream_device"] \
                else s["down"].get(dev)
            if v:
                vals.append(v)
        if not vals:
            continue
        v_bar = sum(vals) / len(vals)
        tol = (acc_map.get(dev, 0.02) + PORTABLE_ACCURACY) * v_bar
        preds = _predictions_for_point(cands, dev, v_bar, tol, upstream_devices)
        pairs = []
        ids = sorted(preds)
        for i, ha in enumerate(ids):
            for hb in ids[i + 1:]:
                a, b = preds[ha], preds[hb]
                if a[1] < b[0] or b[1] < a[0]:
                    pairs.append({"hypothesis_id": ha, "rival_id": hb,
                                  "reason": f"{a[2]} 预测 [{a[0]},{a[1]}] 与 "
                                            f"{b[2]} 预测 [{b[0]},{b[1]}] 不重叠"})
        if not pairs:
            continue
        point = db.scalar(select(MeterPoint).join(
            DeviceMapping, DeviceMapping.meter_point_code == MeterPoint.code).where(
            DeviceMapping.device_id == dev).limit(1))
        has_clock = any(h.hypothesis_kind == HYP_CLOCK_SKEW and h.target == dev
                        for h in cands)
        plan = RetestPlan(
            plan_uid=_uid("retest"), run_id=run.id,
            position_kind="meter_point",
            position=point.code if point else dev, device_id=dev,
            method="clock_check" if has_clock and len(cands) <= 2
            else "portable_flow_meter",
            equipment="标准时钟/GPS 对时设备" if has_clock and len(cands) <= 2
            else "便携式超声流量计 + 秒表（同步比对真值流量）",
            suggested_start=run.observation_cutoff + timedelta(hours=1),
            suggested_end=run.observation_cutoff + timedelta(hours=3),
            observation_cutoff=run.observation_cutoff,
            discriminates=pairs, discriminating_score=len(pairs))
        db.add(plan)
        plans.append(plan)
    plans.sort(key=lambda p: -p.discriminating_score)
    db.flush()
    return plans


# ---------------------------------------------------------------- 现场回报
def submit_retest_report(db: Session, plan_id: int,
                         observations: list[dict], operator: str,
                         equipment: str = "", note: str = "") -> RetestReport:
    """现场回报（可一次回报多个位置的观测）：重算并排除未支持的假设。

    observations: [{device_id, observed_value, observed_at}] —— 实测真值流量。
    """
    plan = db.get(RetestPlan, plan_id)
    if not plan:
        raise ValueError("复测任务不存在")
    run = db.get(DiagnosticRun, plan.run_id)
    if run.status == RUN_ADOPTED:
        raise ValueError("该诊断已被采用并闭环，迟到/补充观测请形成新的诊断运行")

    hyps = db.scalars(select(DiagnosticHypothesis).where(
        DiagnosticHypothesis.run_id == run.id)).all()
    before = [h.id for h in hyps if h.status in (HYP_CANDIDATE, HYP_SUPPORTED)]
    acc_map = run.inputs_snapshot["accuracy"]
    upstream_devices = {run.inputs_snapshot["upstream_device"]}

    for obs in observations:
        dev = obs["device_id"]
        measured = float(obs["observed_value"])
        # 观测时刻该表 concurrent 读数流量（未给则取窗口均值）
        meter_reading = obs.get("meter_reading")
        if meter_reading is None:
            slots = run.inputs_snapshot["slots"]
            vals = [s["up"] if dev == run.inputs_snapshot["upstream_device"]
                    else s["down"].get(dev) for s in slots]
            vals = [v for v in vals if v]
            if not vals:
                continue
            meter_reading = sum(vals) / len(vals)
        meter_reading = float(meter_reading)
        tol = (acc_map.get(dev, 0.02) + PORTABLE_ACCURACY) * meter_reading
        preds = _predictions_for_point(
            [h for h in hyps if h.status in (HYP_CANDIDATE, HYP_SUPPORTED)],
            dev, meter_reading, tol, upstream_devices)
        for h in hyps:
            if h.id not in preds:
                continue
            lo, hi, _ = preds[h.id]
            if lo - 1e-9 <= measured <= hi + 1e-9:
                h.status = HYP_SUPPORTED
            else:
                h.status = HYP_REFUTED
                h.refute_reason = (
                    f"复测实测 {measured} m³/h，该假设预测区间 "
                    f"[{lo}, {hi}] 不相容，排除")
    plan.status = "reported"
    for old in db.scalars(select(RetestPlan).where(
            RetestPlan.run_id == run.id, RetestPlan.status == "proposed")).all():
        old.status = "closed"

    remaining = [h for h in hyps if h.status in (HYP_CANDIDATE, HYP_SUPPORTED)]
    after = [h.id for h in remaining]
    if len(remaining) == 1:
        run.status = RUN_CONVERGED
        remaining[0].status = HYP_SUPPORTED
    elif len(remaining) > 1:
        run.status = RUN_INCONCLUSIVE
        run.note = (run.note + " " if run.note else "") + (
            f"复测后仍有 {len(remaining)} 个候选假设同样成立，证据不足，"
            "不能下结论；需在不同流量水平或更多位置补测。")
    else:
        run.status = RUN_INCONCLUSIVE
        run.note = (run.note + " " if run.note else "") + (
            "全部候选假设均被复测排除：故障模式超出当前假设空间"
            "（可能多故障叠加或结构性问题），建议扩大检查范围。")

    report = RetestReport(
        plan_id=plan.id, run_id=run.id,
        observed_value=float(observations[0]["observed_value"]) if observations else 0.0,
        observed_at=(observations[0].get("observed_at") or _now()) if observations
        else _now(),
        equipment=equipment or plan.equipment, operator=operator, note=note,
        candidates_before=before, candidates_after=after)
    report.note = (note + " " if note else "") + \
        "观测明细: " + "; ".join(
            f"{o['device_id']}={o['observed_value']}m³/h" for o in observations)
    db.add(report)
    db.flush()

    # 候选集合变化后，若仍未收敛则按剩余候选重新生成复测建议
    if run.status == RUN_INCONCLUSIVE and len(remaining) >= 2:
        balance = {"up_device": run.inputs_snapshot["upstream_device"],
                   "down_series_raw": {d: [] for d in
                                       run.inputs_snapshot["downstream_devices"]},
                   "acc_up": 0.0, "loss_min": 0.0, "loss_max": 0.0}
        _generate_retest_plans(db, run, hyps, balance)
    db.flush()
    return report


# ---------------------------------------------------------------- 校正参数
def propose_correction(db: Session, hypothesis_id: int,
                       operator: str) -> CorrectionParam:
    """从候选/被支持的仪表类假设提出校正参数（待批准）。"""
    h = db.get(DiagnosticHypothesis, hypothesis_id)
    if not h:
        raise ValueError("假设不存在")
    if h.hypothesis_kind not in (HYP_METER_OFFSET, HYP_METER_GAIN):
        raise ValueError("仅仪表固定偏移/倍率异常假设可转化为校正参数；"
                         "渠段额外损耗请走工程处置（对账闭环）")
    if h.status not in (HYP_CANDIDATE, HYP_SUPPORTED):
        raise ValueError(f"假设当前状态为 {h.status}，仅候选/被支持的假设可提出校正")
    existing = db.scalar(select(CorrectionParam).where(
        CorrectionParam.hypothesis_id == h.id,
        CorrectionParam.status.in_([CORR_PENDING, CORR_APPROVED])))
    if existing:
        raise ValueError("该假设已存在待批准或已批准的校正参数")

    run = db.get(DiagnosticRun, h.run_id)
    up_dev = run.inputs_snapshot["upstream_device"]
    is_upstream = h.target == up_dev
    slots = run.inputs_snapshot["slots"]
    affected = []
    for s in slots:
        v = s["up"] if is_upstream else s["down"].get(h.target)
        if not v:
            continue
        if h.hypothesis_kind == HYP_METER_OFFSET:
            delta = h.params["offset_m3_per_slot"]
        else:
            delta = h.params["gain_ratio"] * v
        # 下游表少计 => 该渠道补计 +δ；上游表多计 => 上游渠道冲减 −δ
        signed = -delta if is_upstream else delta
        affected.append({"interval_start": s["start"], "interval_end": s["end"],
                         "delta_m3": round(signed, 3)})

    # 设备当前映射（供核算归属）
    dm = db.scalar(select(DeviceMapping).where(
        DeviceMapping.device_id == h.target,
        DeviceMapping.effective_to.is_(None)).limit(1)) or \
        db.scalar(select(DeviceMapping).where(
            DeviceMapping.device_id == h.target).order_by(
            DeviceMapping.effective_from.desc()).limit(1))
    channel_code = None
    if dm:
        mp = db.scalar(select(MeterPoint).where(MeterPoint.code == dm.meter_point_code))
        channel_code = mp.channel_code if mp else None
    corr = CorrectionParam(
        correction_uid=_uid("corr"), hypothesis_id=h.id, run_id=run.id,
        kind=h.hypothesis_kind, device_id=h.target,
        meter_point_code=dm.meter_point_code if dm else None,
        channel_code=channel_code, params=h.params, affected_intervals=affected,
        status=CORR_PENDING, proposed_by=operator)
    db.add(corr)
    db.flush()
    return corr


def approve_correction(db: Session, correction_id: int,
                       operator: str) -> CorrectionParam:
    """批准校正参数：按受影响区间生成追加式 adjustment 事实作用于核算。

    不直接改原读数、不改原事实；正式实灌（confirmed_volume）不含校正量，
    校正量以 adjustment 事实单列进 correction_volume。
    """
    corr = db.get(CorrectionParam, correction_id)
    if not corr:
        raise ValueError("校正参数不存在")
    if corr.status != CORR_PENDING:
        raise ValueError(f"校正参数状态为 {corr.status}，仅待批准状态可批准")
    for iv in corr.affected_intervals:
        start = datetime.fromisoformat(iv["interval_start"])
        end = datetime.fromisoformat(iv["interval_end"])
        db.add(SupplyFact(
            fact_uid=_uid("fact"), fact_kind="adjustment", status="confirmed",
            device_id=corr.device_id, meter_point_code=corr.meter_point_code,
            channel_code=corr.channel_code,
            interval_start=start, interval_end=end,
            value=round(float(iv["delta_m3"]), 3), unit="m3",
            method="meter_correction", confidence=1.0,
            quality_flag="APPROVED_CORRECTION",
            correction_id=corr.id))
    corr.status = CORR_APPROVED
    corr.approved_by = operator
    corr.approved_at = _now()
    h = db.get(DiagnosticHypothesis, corr.hypothesis_id)
    h.status = HYP_ADOPTED
    run = db.get(DiagnosticRun, corr.run_id)
    run.status = RUN_ADOPTED
    db.flush()
    return corr


def reject_correction(db: Session, correction_id: int,
                      operator: str, reason: str = "") -> CorrectionParam:
    corr = db.get(CorrectionParam, correction_id)
    if not corr:
        raise ValueError("校正参数不存在")
    if corr.status != CORR_PENDING:
        raise ValueError(f"校正参数状态为 {corr.status}，仅待批准状态可驳回")
    corr.status = CORR_REJECTED
    corr.invalidate_reason = f"[{operator}] 驳回：{reason}" if reason else f"[{operator}] 驳回"
    db.flush()
    return corr


def invalidate_corrections_for_device(db: Session, device_id: str,
                                      reason: str) -> int:
    """设备资料变更（映射调整/换表）使该设备全部待批准校正失效。"""
    rows = db.scalars(select(CorrectionParam).where(
        CorrectionParam.device_id == device_id,
        CorrectionParam.status == CORR_PENDING)).all()
    for c in rows:
        c.status = CORR_INVALIDATED
        c.invalidate_reason = reason
    if rows:
        db.flush()
    return len(rows)
