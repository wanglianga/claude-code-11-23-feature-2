"""异常损耗诊断与复测安排 API。

- POST /api/diagnosis/run                 触发诊断（固定观测截止点）
- GET  /api/diagnosis/runs                诊断运行列表
- GET  /api/diagnosis/runs/{id}           运行详情（含并列假设与逐时段残差）
- GET  /api/diagnosis/runs/{id}/retests   复测建议（按区分能力排序）
- POST /api/diagnosis/retests/{id}/report 现场回报（重算并排除未支持假设）
- POST /api/diagnosis/hypotheses/{id}/propose-correction  提出校正参数（待批准）
- GET  /api/diagnosis/corrections         校正参数列表
- POST /api/diagnosis/corrections/{id}/approve|reject     批准/驳回
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    CorrectionParam, DiagnosticHypothesis, DiagnosticRun, RetestPlan,
    RetestReport,
)
from ..schemas import (
    CorrectionApproveIn, CorrectionProposeIn, CorrectionRejectIn,
    DiagnosisRunIn, RetestReportIn,
)
from ..services import diagnosis as diag

router = APIRouter(prefix="/api/diagnosis", tags=["异常损耗诊断"])


@router.post("/run", status_code=201)
def post_run(d: DiagnosisRunIn, db: Session = Depends(get_db)):
    try:
        run = diag.run_diagnosis(
            db, d.channel_code, d.window_start, d.window_end,
            d.observation_cutoff, trigger=d.trigger,
            slot_minutes=d.slot_minutes, note=d.note)
        db.commit()
        return {"id": run.id, "run_uid": run.run_uid, "status": run.status,
                "unexplained_residual": run.unexplained_residual}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)):
    rows = db.scalars(select(DiagnosticRun).order_by(
        DiagnosticRun.id.desc())).all()
    out = []
    for r in rows:
        cand = db.scalars(select(DiagnosticHypothesis).where(
            DiagnosticHypothesis.run_id == r.id,
            DiagnosticHypothesis.status.in_(["candidate", "supported"]))).all()
        out.append({
            "id": r.id, "run_uid": r.run_uid, "channel_code": r.channel_code,
            "window_start": r.window_start.isoformat(),
            "window_end": r.window_end.isoformat(),
            "observation_cutoff": r.observation_cutoff.isoformat(),
            "trigger": r.trigger, "status": r.status,
            "upstream_total": r.upstream_total,
            "downstream_total": r.downstream_total,
            "residual_total": r.residual_total,
            "unexplained_residual": r.unexplained_residual,
            "slot_minutes": r.slot_minutes,
            "sparse_readings": r.inputs_snapshot.get("sparse_readings", False),
            "candidate_count": len(cand), "note": r.note,
            "created_at": r.created_at.isoformat()})
    return out


@router.get("/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db)):
    run = db.get(DiagnosticRun, run_id)
    if not run:
        raise HTTPException(404, "诊断运行不存在")
    hyps = db.scalars(select(DiagnosticHypothesis).where(
        DiagnosticHypothesis.run_id == run.id).order_by(
        DiagnosticHypothesis.is_candidate.desc(),
        DiagnosticHypothesis.id)).all()
    reports = db.scalars(select(RetestReport).where(
        RetestReport.run_id == run.id).order_by(RetestReport.id)).all()
    return {
        "id": run.id, "run_uid": run.run_uid, "channel_code": run.channel_code,
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "observation_cutoff": run.observation_cutoff.isoformat(),
        "trigger": run.trigger, "status": run.status,
        "upstream_total": run.upstream_total,
        "downstream_total": run.downstream_total,
        "residual_total": run.residual_total,
        "unexplained_residual": run.unexplained_residual,
        "slot_minutes": run.slot_minutes,
        "inputs_snapshot": run.inputs_snapshot, "note": run.note,
        "created_at": run.created_at.isoformat(),
        "hypotheses": [_hyp_dict(h) for h in hyps],
        "reports": [{"id": rep.id, "plan_id": rep.plan_id,
                     "observed_value": rep.observed_value,
                     "observed_at": rep.observed_at.isoformat()
                     if rep.observed_at else None,
                     "equipment": rep.equipment, "operator": rep.operator,
                     "note": rep.note,
                     "candidates_before": rep.candidates_before,
                     "candidates_after": rep.candidates_after}
                    for rep in reports]}


@router.get("/runs/{run_id}/retests")
def run_retests(run_id: int, db: Session = Depends(get_db)):
    plans = db.scalars(select(RetestPlan).where(
        RetestPlan.run_id == run_id).order_by(
        RetestPlan.status, RetestPlan.discriminating_score.desc())).all()
    return [_plan_dict(p) for p in plans]


@router.post("/retests/{plan_id}/report", status_code=201)
def post_retest_report(plan_id: int, d: RetestReportIn,
                       db: Session = Depends(get_db)):
    if not d.observations:
        raise HTTPException(422, "至少需要一个观测值")
    try:
        report = diag.submit_retest_report(
            db, plan_id,
            [o.model_dump() for o in d.observations],
            operator=d.operator, equipment=d.equipment, note=d.note)
        db.commit()
        return {"id": report.id, "candidates_before": report.candidates_before,
                "candidates_after": report.candidates_after}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/hypotheses/{hypothesis_id}/propose-correction", status_code=201)
def post_propose(hypothesis_id: int, d: CorrectionProposeIn,
                 db: Session = Depends(get_db)):
    try:
        corr = diag.propose_correction(db, hypothesis_id, d.operator)
        db.commit()
        return {"id": corr.id, "correction_uid": corr.correction_uid,
                "status": corr.status,
                "affected_intervals": corr.affected_intervals}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/corrections")
def list_corrections(db: Session = Depends(get_db)):
    rows = db.scalars(select(CorrectionParam).order_by(
        CorrectionParam.id.desc())).all()
    return [{
        "id": c.id, "correction_uid": c.correction_uid,
        "hypothesis_id": c.hypothesis_id, "run_id": c.run_id,
        "kind": c.kind, "device_id": c.device_id,
        "meter_point_code": c.meter_point_code,
        "channel_code": c.channel_code, "params": c.params,
        "affected_intervals": c.affected_intervals,
        "status": c.status, "proposed_by": c.proposed_by,
        "approved_by": c.approved_by,
        "approved_at": c.approved_at.isoformat() if c.approved_at else None,
        "invalidate_reason": c.invalidate_reason,
        "created_at": c.created_at.isoformat()} for c in rows]


@router.post("/corrections/{correction_id}/approve")
def post_approve(correction_id: int, d: CorrectionApproveIn,
                 db: Session = Depends(get_db)):
    try:
        corr = diag.approve_correction(db, correction_id, d.operator)
        db.commit()
        return {"id": corr.id, "status": corr.status,
                "adjustment_facts": len(corr.affected_intervals)}
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/corrections/{correction_id}/reject")
def post_reject(correction_id: int, d: CorrectionRejectIn,
                db: Session = Depends(get_db)):
    try:
        corr = diag.reject_correction(db, correction_id, d.operator, d.reason)
        db.commit()
        return {"id": corr.id, "status": corr.status}
    except ValueError as e:
        raise HTTPException(409, str(e))


def _hyp_dict(h: DiagnosticHypothesis) -> dict:
    return {
        "id": h.id, "run_id": h.run_id,
        "hypothesis_kind": h.hypothesis_kind, "target_kind": h.target_kind,
        "target": h.target, "params": h.params,
        "explained": h.explained, "unexplained": h.unexplained,
        "residual_after": h.residual_after, "is_candidate": h.is_candidate,
        "status": h.status, "refute_reason": h.refute_reason,
        "created_at": h.created_at.isoformat()}


def _plan_dict(p: RetestPlan) -> dict:
    return {
        "id": p.id, "plan_uid": p.plan_uid, "run_id": p.run_id,
        "position_kind": p.position_kind, "position": p.position,
        "device_id": p.device_id, "method": p.method,
        "equipment": p.equipment,
        "suggested_start": p.suggested_start.isoformat(),
        "suggested_end": p.suggested_end.isoformat(),
        "observation_cutoff": p.observation_cutoff.isoformat(),
        "discriminates": p.discriminates,
        "discriminating_score": p.discriminating_score,
        "status": p.status, "created_at": p.created_at.isoformat()}
