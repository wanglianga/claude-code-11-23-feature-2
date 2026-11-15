import React, { useCallback, useMemo, useState } from "react";
import { get, post } from "../api";
import {
  Card, Badge, Drawer, useFetch, useToast, fmt, dayOf, Loading, Empty,
} from "../components";

const RUN_STATUS = {
  open: { label: "候选未收敛", tone: "tentative" },
  converged: { label: "已收敛", tone: "confirmed" },
  inconclusive: { label: "证据不足", tone: "pending_verification" },
  adopted: { label: "已采用", tone: "stable" },
};
const HYP_KIND = {
  meter_offset: "仪表固定偏移",
  meter_gain: "仪表倍率异常",
  segment_loss: "渠段额外损耗",
  clock_skew: "设备时钟偏差",
};
const HYP_STATUS = {
  candidate: { label: "候选", tone: "candidate" },
  supported: { label: "复测支持", tone: "confirmed" },
  refuted: { label: "已排除", tone: "rejected" },
  adopted: { label: "已采纳", tone: "stable" },
};
const CORR_STATUS = {
  pending_approval: { label: "待批准", tone: "pending_verification" },
  approved: { label: "已批准", tone: "confirmed" },
  rejected: { label: "已驳回", tone: "rejected" },
  invalidated: { label: "已失效", tone: "frozen" },
};

function hypParamText(h) {
  const p = h.params || {};
  if (h.hypothesis_kind === "meter_offset") return `每时段固定偏移 ${fmt(p.offset_m3_per_slot)} m³`;
  if (h.hypothesis_kind === "meter_gain") return `倍率异常 ${(p.gain_ratio * 100).toFixed(1)}%`;
  if (h.hypothesis_kind === "segment_loss") return `渠段额外损耗 ${fmt(p.extra_loss_m3_per_slot)} m³/时段`;
  if (h.hypothesis_kind === "clock_skew") return `时钟偏差 ${p.shift_minutes} 分钟`;
  return JSON.stringify(p);
}

export default function Diagnosis() {
  const toast = useToast();
  const user = JSON.parse(localStorage.getItem("irr_user") || "null");
  const isAdmin = user?.role === "admin";
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [retests, setRetests] = useState([]);
  const [reportPlan, setReportPlan] = useState(null);
  const [obs, setObs] = useState({});
  const [showRunForm, setShowRunForm] = useState(false);
  const [runForm, setRunForm] = useState({
    channel_code: "WEST", window_start: "2026-11-13T00:00", window_end: "2026-11-13T06:00",
    observation_cutoff: "2026-11-13T08:00", note: "",
  });

  const loadRuns = useCallback(() => get("/diagnosis/runs"), []);
  const loadCorrections = useCallback(() => get("/diagnosis/corrections"), []);
  const loadTree = useCallback(() => get("/master/tree"), []);
  const runs = useFetch(loadRuns);
  const corrections = useFetch(loadCorrections);
  const tree = useFetch(loadTree);

  const openRun = async (r) => {
    setSelected(r);
    const [d, plans] = await Promise.all([
      get(`/diagnosis/runs/${r.id}`), get(`/diagnosis/runs/${r.id}/retests`)]);
    setDetail(d);
    setRetests(plans);
  };

  const refreshDetail = async () => {
    if (!selected) return;
    const [d, plans] = await Promise.all([
      get(`/diagnosis/runs/${selected.id}`), get(`/diagnosis/runs/${selected.id}/retests`)]);
    setDetail(d);
    setRetests(plans);
    runs.reload();
    corrections.reload();
  };

  const startDiagnosis = async () => {
    try {
      const body = {
        ...runForm,
        window_start: new Date(runForm.window_start).toISOString(),
        window_end: new Date(runForm.window_end).toISOString(),
        observation_cutoff: new Date(runForm.observation_cutoff).toISOString(),
      };
      const r = await post("/diagnosis/run", body);
      toast(`诊断运行 #${r.id} 已生成，容差外残差 ${fmt(r.unexplained_residual)} m³`, "ok");
      setShowRunForm(false);
      runs.reload();
    } catch (e) { toast(e.message, "err"); }
  };

  const submitReport = async () => {
    const observations = Object.entries(obs)
      .filter(([, v]) => v.observed !== "")
      .map(([dev, v]) => ({
        device_id: dev,
        observed_value: Number(v.observed),
        meter_reading: v.meter === "" ? null : Number(v.meter),
      }));
    if (!observations.length) { toast("请至少填写一个位置的实测真值", "err"); return; }
    try {
      const r = await post(`/diagnosis/retests/${reportPlan.id}/report`, {
        observations, operator: user?.name || "站长", equipment: reportPlan.equipment,
      });
      toast(`回报已登记：候选 ${r.candidates_before.length} -> ${r.candidates_after.length}`, "ok");
      setReportPlan(null); setObs({});
      refreshDetail();
    } catch (e) { toast(e.message, "err"); }
  };

  const proposeCorrection = async (hypId) => {
    try {
      const r = await post(`/diagnosis/hypotheses/${hypId}/propose-correction`,
        { operator: user?.name || "站长" });
      toast(`校正参数 #${r.id} 已提出，待批准（${r.affected_intervals.length} 个受影响区间）`, "ok");
      refreshDetail();
    } catch (e) { toast(e.message, "err"); }
  };

  const approveCorrection = async (id) => {
    try {
      const r = await post(`/diagnosis/corrections/${id}/approve`, { operator: user?.name || "admin" });
      toast(`校正参数 #${r.id} 已批准，生成 ${r.adjustment_facts} 条调整事实（不改原读数）`, "ok");
      refreshDetail();
    } catch (e) { toast(e.message, "err"); }
  };

  const rejectCorrection = async (id) => {
    try {
      await post(`/diagnosis/corrections/${id}/reject`, { operator: user?.name || "admin", reason: "证据不足" });
      toast("校正参数已驳回", "ok");
      refreshDetail();
    } catch (e) { toast(e.message, "err"); }
  };

  const parentChannels = useMemo(() => {
    if (!tree.data) return [];
    const parents = new Set(tree.data.channels.filter((c) => !c.parent_code).map((c) => c.code));
    return [...parents];
  }, [tree.data]);

  if (!runs.data || !corrections.data) return <Loading />;
  const candidates = detail ? detail.hypotheses.filter((h) => h.is_candidate) : [];
  const excluded = detail ? detail.hypotheses.filter((h) => !h.is_candidate) : [];
  const proposedPlans = retests.filter((p) => p.status === "proposed");
  const slotRows = detail?.inputs_snapshot?.slots || [];

  return (
    <div>
      <div className="page-head flex">
        <div style={{ flex: 1 }}>
          <h1>异常损耗诊断与复测安排</h1>
          <p>按输送时长对齐上下游、连接处水量平衡，对容差外残差定位；多假设并列试算，不凭最小残差独断；
            复测区分候选，校正参数批准后才作用于核算</p>
        </div>
        <button onClick={() => setShowRunForm(!showRunForm)}>发起诊断</button>
      </div>

      {showRunForm && (
        <Card title="发起异常损耗诊断" className="mb-16">
          <div className="form-grid">
            <label className="field">汇合渠道
              <select value={runForm.channel_code}
                onChange={(e) => setRunForm({ ...runForm, channel_code: e.target.value })}>
                {parentChannels.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            <label className="field">窗口开始
              <input type="datetime-local" value={runForm.window_start}
                onChange={(e) => setRunForm({ ...runForm, window_start: e.target.value })} />
            </label>
            <label className="field">窗口结束
              <input type="datetime-local" value={runForm.window_end}
                onChange={(e) => setRunForm({ ...runForm, window_end: e.target.value })} />
            </label>
            <label className="field">观测截止点（固定，迟到数据将形成新分析）
              <input type="datetime-local" value={runForm.observation_cutoff}
                onChange={(e) => setRunForm({ ...runForm, observation_cutoff: e.target.value })} />
            </label>
          </div>
          <label className="field">备注
            <input value={runForm.note} onChange={(e) => setRunForm({ ...runForm, note: e.target.value })}
              placeholder="诊断背景说明" />
          </label>
          <div className="flex gap-6 mt-12">
            <button onClick={startDiagnosis}>执行诊断</button>
            <button className="ghost" onClick={() => setShowRunForm(false)}>取消</button>
          </div>
        </Card>
      )}

      <Card title="诊断运行">
        {runs.data.length === 0 ? <Empty text="暂无诊断运行" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>渠道</th><th>分析窗口</th><th>观测截止点</th>
                <th className="num">上游总量</th><th className="num">容差外残差</th>
                <th className="num">候选假设</th><th>触发</th><th>状态</th><th></th>
              </tr></thead>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.id} style={selected?.id === r.id ? { background: "var(--bg-soft)" } : undefined}>
                    <td><b>{r.channel_code}</b>{r.sparse_readings &&
                      <Badge tone="warning">读数稀疏</Badge>}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {dayOf(r.window_start)} {r.window_start.slice(11, 16)}~{r.window_end.slice(11, 16)}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{r.observation_cutoff.slice(0, 16).replace("T", " ")}</td>
                    <td className="num">{fmt(r.upstream_total)}</td>
                    <td className="num" style={{ color: "var(--danger)", fontWeight: 700 }}>
                      {fmt(r.unexplained_residual)}</td>
                    <td className="num">{r.candidate_count}</td>
                    <td><Badge tone="neutral">{r.trigger}</Badge></td>
                    <td><Badge tone={RUN_STATUS[r.status]?.tone}>{RUN_STATUS[r.status]?.label || r.status}</Badge></td>
                    <td><button className="ghost sm" onClick={() => openRun(r)}>详情</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {detail && (
        <>
          <h2 className="section-title">诊断详情 · 运行 #{detail.id}（{detail.channel_code}）</h2>
          {detail.note && <p className="soft" style={{ margin: "4px 0 10px" }}>{detail.note}</p>}
          {detail.status === "inconclusive" && (
            <Card className="mb-16" tight>
              <Badge tone="pending_verification">证据不足，不能下结论</Badge>
              <span className="soft" style={{ marginLeft: 8, fontSize: 12.5 }}>
                仍有多个候选假设同样成立，需在不同流量水平或更多位置补测；损耗指标保持冻结，不得凭残差最小选定故障。
              </span>
            </Card>
          )}

          <Card title="逐时段水量平衡（按输送时长对齐）" className="mb-16">
            <div className="table-wrap">
              <table className="data">
                <thead><tr>
                  <th>时段(上游时刻)</th><th className="num">上游量</th>
                  {detail.inputs_snapshot.downstream_devices.map((d) => (
                    <th key={d} className="num">{d}</th>))}
                  <th className="num">残差</th><th>允许区间</th><th>判定</th>
                </tr></thead>
                <tbody>
                  {slotRows.map((s) => (
                    <tr key={s.tau}>
                      <td className="mono" style={{ fontSize: 11.5 }}>
                        {s.start.slice(11, 16)}~{s.end.slice(11, 16)}{s.estimated && " ≈"}</td>
                      <td className="num">{fmt(s.up)}</td>
                      {detail.inputs_snapshot.downstream_devices.map((d) => (
                        <td key={d} className="num">{fmt(s.down[d] ?? 0)}</td>))}
                      <td className="num" style={{ color: Math.abs(s.excess) > 1e-9 ? "var(--danger)" : undefined, fontWeight: 700 }}>
                        {fmt(s.residual)}</td>
                      <td className="mono muted" style={{ fontSize: 11 }}>[{fmt(s.allow_lo)}, {fmt(s.allow_hi)}]</td>
                      <td>{Math.abs(s.excess) > 1e-9
                        ? <Badge tone="critical">超差 {fmt(s.excess)}</Badge>
                        : <Badge tone="confirmed">相容</Badge>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="soft" style={{ fontSize: 12, marginTop: 6 }}>
              允许区间 = 正常损耗 [{detail.inputs_snapshot.loss_ratio_range.map((x) => `${(x * 100).toFixed(1)}%`).join(" ~ ")}]
              ± 仪表合成不确定度；带 ≈ 的时段来自稀疏读数均摊（中间缺测），证据强度低。
            </p>
          </Card>

          <Card title={`候选假设并列（${candidates.filter((h) => h.status !== "refuted").length} 个成立）`}
            extra={<span className="muted" style={{ fontSize: 12 }}>多种解释同样成立须并列，不按残差最小判定</span>}
            className="mb-16">
            {candidates.length === 0 ? <Empty text="无候选假设" /> : (
              <div className="hyp-grid">
                {candidates.map((h) => (
                  <div key={h.id} className={`hyp-card ${h.status}`}>
                    <div className="flex" style={{ justifyContent: "space-between" }}>
                      <b>{HYP_KIND[h.hypothesis_kind]}</b>
                      <Badge tone={HYP_STATUS[h.status]?.tone}>{HYP_STATUS[h.status]?.label}</Badge>
                    </div>
                    <div className="mono" style={{ fontSize: 12, margin: "4px 0" }}>
                      对象：{h.target_kind === "device" ? `仪表 ${h.target}` : `渠段 ${h.target}`}</div>
                    <div style={{ fontSize: 12.5 }}>{hypParamText(h)}</div>
                    <div className="soft" style={{ fontSize: 12, marginTop: 6 }}>
                      能解释时段：{h.explained.length} 个
                      {h.unexplained.length > 0 && `；仍不相容 ${h.unexplained.length} 个`}
                    </div>
                    {h.refute_reason && <div className="warn-text" style={{ fontSize: 12 }}>{h.refute_reason}</div>}
                    {(h.status === "candidate" || h.status === "supported") &&
                      h.hypothesis_kind !== "segment_loss" && h.hypothesis_kind !== "clock_skew" && (
                      <button className="ghost sm" style={{ marginTop: 8 }}
                        onClick={() => proposeCorrection(h.id)}>提出校正参数</button>
                    )}
                    {h.hypothesis_kind === "segment_loss" && h.status !== "refuted" && (
                      <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
                        真实水损失：请走工程处置（对账闭环），不生成计量校正</div>
                    )}
                  </div>
                ))}
              </div>
            )}
            {excluded.length > 0 && (
              <details style={{ marginTop: 10 }}>
                <summary className="soft" style={{ cursor: "pointer", fontSize: 12.5 }}>
                  被观测数据排除的假设（{excluded.length} 个，含仍不相容部分）</summary>
                <div className="table-wrap" style={{ marginTop: 8 }}>
                  <table className="data">
                    <thead><tr><th>假设</th><th>对象</th><th>试算参数</th><th>能解释</th><th>仍不相容部分</th></tr></thead>
                    <tbody>
                      {excluded.map((h) => (
                        <tr key={h.id}>
                          <td>{HYP_KIND[h.hypothesis_kind]}</td>
                          <td className="mono" style={{ fontSize: 11.5 }}>{h.target}</td>
                          <td style={{ fontSize: 12 }}>{hypParamText(h)}</td>
                          <td className="num">{h.explained.length} 时段</td>
                          <td style={{ fontSize: 11.5 }}>
                            {h.unexplained.slice(0, 3).map((u) => (
                              <div key={u.tau} className="mono">
                                {String(u.start).slice(11, 16)} 残差 {fmt(u.residual_after)}（允许 [{fmt(u.allow[0])},{fmt(u.allow[1])}]）
                              </div>
                            ))}
                            {h.unexplained.length > 3 && <div className="muted">…共 {h.unexplained.length} 段</div>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </Card>

          <Card title="复测建议（按可区分候选假设对数排序）" className="mb-16">
            {proposedPlans.length === 0 ? <Empty text="暂无待执行复测建议" /> : (
              <div className="table-wrap">
                <table className="data">
                  <thead><tr>
                    <th>复测位置</th><th>方式/设备</th><th>建议时间窗</th>
                    <th className="num">可区分假设对</th><th>观测截止点</th><th>操作</th>
                  </tr></thead>
                  <tbody>
                    {proposedPlans.map((p) => (
                      <tr key={p.id}>
                        <td><b>{p.position}</b><span className="mono muted" style={{ fontSize: 11 }}>（{p.device_id}）</span></td>
                        <td style={{ fontSize: 12 }}>{p.equipment}</td>
                        <td className="mono" style={{ fontSize: 11.5 }}>
                          {p.suggested_start.slice(0, 16).replace("T", " ")} ~ {p.suggested_end.slice(11, 16)}</td>
                        <td className="num" style={{ fontWeight: 700 }}>{p.discriminating_score}</td>
                        <td className="mono muted" style={{ fontSize: 11.5 }}>{p.observation_cutoff.slice(0, 16).replace("T", " ")}</td>
                        <td><button className="ghost sm" onClick={() => { setReportPlan(p); setObs({}); }}>
                          现场回报</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {detail.reports.length > 0 && (
              <>
                <h3 style={{ fontSize: 13, margin: "12px 0 6px" }}>现场回报记录</h3>
                <div className="table-wrap">
                  <table className="data">
                    <thead><tr><th>时间</th><th>观测明细</th><th>操作人</th><th>候选集合变化</th></tr></thead>
                    <tbody>
                      {detail.reports.map((rep) => (
                        <tr key={rep.id}>
                          <td className="mono" style={{ fontSize: 11.5 }}>{(rep.observed_at || "").slice(0, 16).replace("T", " ")}</td>
                          <td style={{ fontSize: 12 }}>{rep.note}</td>
                          <td>{rep.operator}</td>
                          <td>
                            <Badge tone="candidate">{rep.candidates_before.length} 个</Badge>
                            <span style={{ margin: "0 6px" }}>→</span>
                            <Badge tone={rep.candidates_after.length === 1 ? "confirmed" : "pending_verification"}>
                              {rep.candidates_after.length} 个</Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Card>
        </>
      )}

      <h2 className="section-title">校正参数（待批准 / 已批准 / 已失效）</h2>
      <Card>
        {corrections.data.length === 0 ? <Empty text="暂无校正参数" /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr>
                <th>类型</th><th>设备</th><th>参数</th><th className="num">受影响区间</th>
                <th className="num">合计差额</th><th>状态</th><th>提出/审批</th><th>操作</th>
              </tr></thead>
              <tbody>
                {corrections.data.map((c) => (
                  <React.Fragment key={c.id}>
                    <tr>
                      <td>{c.kind === "meter_offset" ? "固定偏移" : "倍率异常"}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>{c.device_id}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>
                        {c.kind === "meter_offset"
                          ? `+${fmt(c.params.offset_m3_per_slot)} m³/时段`
                          : `${(c.params.gain_ratio * 100).toFixed(1)}%`}</td>
                      <td className="num">{c.affected_intervals.length} 段</td>
                      <td className="num" style={{ fontWeight: 700 }}>
                        {fmt(c.affected_intervals.reduce((s, x) => s + x.delta_m3, 0))} m³</td>
                      <td>
                        <Badge tone={CORR_STATUS[c.status]?.tone}>{CORR_STATUS[c.status]?.label}</Badge>
                        {c.invalidate_reason &&
                          <div className="muted" style={{ fontSize: 11, maxWidth: 220 }}>{c.invalidate_reason}</div>}
                      </td>
                      <td style={{ fontSize: 11.5 }}>
                        <div>提出：{c.proposed_by}</div>
                        {c.approved_by && <div>批准：{c.approved_by} @ {c.approved_at?.slice(0, 16).replace("T", " ")}</div>}
                      </td>
                      <td>
                        {c.status === "pending_approval" && isAdmin && (
                          <div className="flex gap-6">
                            <button className="ghost sm" onClick={() => approveCorrection(c.id)}>批准</button>
                            <button className="ghost sm" onClick={() => rejectCorrection(c.id)}>驳回</button>
                          </div>
                        )}
                        {c.status === "pending_approval" && !isAdmin &&
                          <span className="muted" style={{ fontSize: 11.5 }}>需管理员批准</span>}
                      </td>
                    </tr>
                    <tr>
                      <td colSpan={8} style={{ padding: "2px 10px 10px", borderTop: "none" }}>
                        <details>
                          <summary className="soft" style={{ cursor: "pointer", fontSize: 11.5 }}>
                            全部受影响区间及差额（{c.affected_intervals.length} 段；批准后生成追加式调整事实，不改原读数）</summary>
                          <div className="mono" style={{ fontSize: 11, marginTop: 4, columnCount: 2 }}>
                            {c.affected_intervals.map((iv, i) => (
                              <div key={i}>{iv.interval_start.slice(0, 16).replace("T", " ")} ~ {iv.interval_end.slice(11, 16)}：
                                {iv.delta_m3 > 0 ? "+" : ""}{fmt(iv.delta_m3)} m³</div>
                            ))}
                          </div>
                        </details>
                      </td>
                    </tr>
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {reportPlan && (
        <Drawer title={`现场回报 · ${reportPlan.position}（${reportPlan.device_id}）`}
          onClose={() => setReportPlan(null)}>
          <p className="soft">
            所需设备：{reportPlan.equipment}。用便携设备实测真值流量，并记录该表 concurrent 读数；
            可一次填报多个位置的观测。回报后系统重新计算，未支持的假设被排除。
          </p>
          <ObservationForm plan={reportPlan} detail={detail} obs={obs} setObs={setObs} />
          <div className="flex gap-6 mt-12">
            <button onClick={submitReport}>提交回报并重算</button>
            <button className="ghost" onClick={() => setReportPlan(null)}>取消</button>
          </div>
        </Drawer>
      )}
    </div>
  );
}

function ObservationForm({ plan, detail, obs, setObs }) {
  const devices = [detail.inputs_snapshot.upstream_device,
    ...detail.inputs_snapshot.downstream_devices];
  const set = (dev, key, val) => setObs((o) => ({ ...o, [dev]: { ...o[dev], [key]: val } }));
  return (
    <div>
      <div className="soft" style={{ fontSize: 12, marginBottom: 6 }}>
        建议位置：{plan.position}（也可顺带填报其他位置，一次回报全部生效）</div>
      {devices.map((dev) => (
        <div key={dev} className="flex gap-6" style={{ alignItems: "flex-end", marginBottom: 6 }}>
          <span className="mono" style={{ fontSize: 12, width: 110 }}>{dev}{dev === plan.device_id && " ★"}</span>
          <label className="field" style={{ flex: 1 }}>表读数流量 (m³/h)
            <input type="number" value={obs[dev]?.meter ?? ""}
              onChange={(e) => set(dev, "meter", e.target.value)} placeholder="该表 concurrent 读数" />
          </label>
          <label className="field" style={{ flex: 1 }}>实测真值 (m³/h)
            <input type="number" value={obs[dev]?.observed ?? ""}
              onChange={(e) => set(dev, "observed", e.target.value)} placeholder="便携设备实测" />
          </label>
        </div>
      ))}
    </div>
  );
}
