"""API 端到端：登录、契约、接入、编译、发布、血缘链路可达。"""
import os

os.environ.setdefault(
    "IRR_DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/irrigation_test")

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, engine, SessionLocal
from app.main import app

client = TestClient(app)


def setup_module(module):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _reset():
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE metric_versions, recompute_runs, reconciliation_tasks, "
            "adjudications, quality_events, supply_facts, meter_changes, "
            "plot_batch_allocations, device_mappings, mapping_versions, "
            "rotation_batches, plots, meter_points, channels, "
            "manual_revisions, raw_messages, data_contracts RESTART IDENTITY CASCADE"))


def test_full_api_flow():
    _reset()
    # 登录
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    bad = client.post("/api/auth/login", json={"username": "admin", "password": "x"})
    assert bad.status_code == 401

    # 契约登记
    r = client.post("/api/contracts", json={
        "code": "CT-CUM", "name": "累计契约", "source_kind": "device",
        "reading_type": "cumulative", "unit": "m3",
        "device_id_pattern": "D-.*", "collection_interval_sec": 3600,
        "payload_schema": {"reading": {"required": True}},
        "source_version": "v1",
        "active_from": "2026-11-10T00:00:00+00:00"})
    assert r.status_code == 201

    # 主数据
    client.post("/api/master/channels", json={"code": "MAIN", "name": "干渠", "level": 1})
    client.post("/api/master/channels",
                json={"code": "B1", "name": "支渠", "level": 2, "parent_code": "MAIN"})
    client.post("/api/master/meter-points",
                json={"code": "MP-HEAD", "name": "渠首", "channel_code": "MAIN",
                      "point_role": "head"})
    client.post("/api/master/meter-points",
                json={"code": "MP-B1", "name": "支渠点", "channel_code": "B1",
                      "point_role": "branch"})
    client.post("/api/master/plots",
                json={"code": "P1", "name": "地块", "area_mu": 100, "channel_code": "B1"})
    client.post("/api/master/batches",
                json={"code": "B1B", "name": "批次",
                      "planned_start": "2026-11-10T00:00:00+00:00",
                      "planned_end": "2026-11-10T06:00:00+00:00"})
    r = client.post("/api/master/mapping/publish", params={"note": "init"})
    vno = r.json()["mapping_version"]
    client.post("/api/master/device-mappings", json={
        "device_id": "D-HEAD", "meter_point_code": "MP-HEAD",
        "effective_from": "2026-11-10T00:00:00+00:00"})
    client.post("/api/master/device-mappings", json={
        "device_id": "D-B1", "meter_point_code": "MP-B1",
        "effective_from": "2026-11-10T00:00:00+00:00"})
    client.post("/api/master/allocations", json={
        "plot_code": "P1", "batch_code": "B1B", "planned_volume": 500,
        "effective_from": "2026-11-10T00:00:00+00:00"})

    # 报文（含倒退）
    for h, reading in [(0, 0), (1, 100), (2, 200), (3, 180)]:
        r = client.post("/api/ingestion/raw", json={
            "message_uid": f"M-{h}", "contract_code": "CT-CUM", "device_id": "D-B1",
            "payload": {"reading": reading, "unit": "m3"},
            "event_time": f"2026-11-10T0{h}:00:00+00:00",
            "received_at": f"2026-11-10T0{h}:05:00+00:00",
            "source_version": "v1"})
        assert r.status_code == 201
    # 幂等：重复 uid 被忽略
    dup = client.post("/api/ingestion/raw", json={
        "message_uid": "M-0", "contract_code": "CT-CUM", "device_id": "D-B1",
        "payload": {"reading": 0, "unit": "m3"},
        "event_time": "2026-11-10T00:00:00+00:00",
        "received_at": "2026-11-10T00:05:00+00:00", "source_version": "v1"})
    assert dup.json()["accepted"] is False

    # 编译 + 发布
    r = client.post("/api/compile")
    assert r.status_code == 201 and r.json()["supply_facts_written"] >= 1
    r = client.post("/api/metrics/publish", params={"trigger": "test"})
    assert r.status_code == 201 and r.json()["published"] >= 1

    # 指标、事实、质量事件可查
    metrics = client.get("/api/metrics").json()
    assert any(m["grain_type"] == "plot" for m in metrics)
    facts = client.get("/api/facts").json()
    assert any(f["quality_flag"] == "READING_REVERSAL" for f in facts)
    events = client.get("/api/quality/events").json()
    assert any(e["rule_code"] == "READING_REVERSAL" for e in events)

    # 血缘：任一指标都能回溯到节点
    metric_id = metrics[0]["id"]
    lineage = client.get(f"/api/metrics/{metric_id}/lineage").json()
    assert lineage["nodes"] and lineage["edges"]
    kinds = {n["kind"] for n in lineage["nodes"]}
    assert "metric" in kinds and "fact" in kinds

    # 版本链可回放
    versions = client.get(f"/api/metrics/{metric_id}/versions").json()
    assert len(versions) >= 1


def test_diagnosis_api_flow():
    """诊断 API：运行 -> 假设并列 -> 复测建议 -> 回报 -> 校正提出/批准。"""
    _reset()
    client.post("/api/contracts", json={
        "code": "CT-CUM", "name": "累计契约", "source_kind": "device",
        "reading_type": "cumulative", "unit": "m3",
        "device_id_pattern": "D-.*", "collection_interval_sec": 3600,
        "payload_schema": {"reading": {"required": True}},
        "source_version": "v1", "active_from": "2026-11-11T00:00:00+00:00"})
    client.post("/api/master/channels", json={
        "code": "WEST", "name": "西干渠", "level": 1,
        "normal_loss_min_ratio": 0.005, "normal_loss_max_ratio": 0.03,
        "travel_minutes": 60})
    client.post("/api/master/channels", json={
        "code": "W1", "name": "西一", "level": 2, "parent_code": "WEST",
        "travel_minutes": 60})
    client.post("/api/master/channels", json={
        "code": "W2", "name": "西二", "level": 2, "parent_code": "WEST",
        "travel_minutes": 60})
    client.post("/api/master/meter-points", json={
        "code": "MP-WH", "name": "渠首", "channel_code": "WEST",
        "point_role": "head", "accuracy_ratio": 0.02})
    client.post("/api/master/meter-points", json={
        "code": "MP-W1", "name": "支一", "channel_code": "W1",
        "point_role": "branch", "accuracy_ratio": 0.02})
    client.post("/api/master/meter-points", json={
        "code": "MP-W2", "name": "支二", "channel_code": "W2",
        "point_role": "branch", "accuracy_ratio": 0.02})
    client.post("/api/master/mapping/publish", params={"note": "init"})
    for dev, mp in [("D-WH-01", "MP-WH"), ("D-W1-01", "MP-W1"), ("D-W2-01", "MP-W2")]:
        client.post("/api/master/device-mappings", json={
            "device_id": dev, "meter_point_code": mp,
            "effective_from": "2026-11-11T00:00:00+00:00"})

    # 渠段渗漏场景读数
    series = {
        "D-WH-01": [1000, 1225, 1450, 1562, 1674, 1786, 1898],
        "D-W1-01": [500, 590, 680, 770, 860, 950, 1040],
        "D-W2-01": [2000, 2100, 2200, 2300, 2300, 2300, 2300],
    }
    for dev, readings in series.items():
        for h, r in enumerate(readings):
            resp = client.post("/api/ingestion/raw", json={
                "message_uid": f"{dev}-{h}", "contract_code": "CT-CUM",
                "device_id": dev, "payload": {"reading": r, "unit": "m3"},
                "event_time": f"2026-11-11T0{h}:00:00+00:00",
                "received_at": f"2026-11-11T0{h}:05:00+00:00",
                "source_version": "v1"})
            assert resp.status_code == 201

    # 运行诊断
    r = client.post("/api/diagnosis/run", json={
        "channel_code": "WEST",
        "window_start": "2026-11-11T00:00:00+00:00",
        "window_end": "2026-11-11T06:00:00+00:00",
        "observation_cutoff": "2026-11-11T08:00:00+00:00"})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    assert r.json()["unexplained_residual"] > 0

    # 详情：候选假设并列（不独断）
    detail = client.get(f"/api/diagnosis/runs/{run_id}").json()
    cands = [h for h in detail["hypotheses"] if h["is_candidate"]]
    assert len(cands) >= 3
    kinds = {h["hypothesis_kind"] for h in cands}
    assert "segment_loss" in kinds and "meter_offset" in kinds

    # 复测建议：按区分能力排序且非空
    plans = client.get(f"/api/diagnosis/runs/{run_id}/retests").json()
    proposed = [p for p in plans if p["status"] == "proposed"]
    assert proposed and all(p["discriminating_score"] > 0 for p in proposed)
    assert proposed[0]["equipment"]

    # 现场回报：候选集合真正缩小
    plan_id = proposed[0]["id"]
    r = client.post(f"/api/diagnosis/retests/{plan_id}/report", json={
        "observations": [
            {"device_id": "D-WH-01", "observed_value": 224.5, "meter_reading": 225.0},
            {"device_id": "D-W1-01", "observed_value": 90.8, "meter_reading": 90.0},
        ], "operator": "站长"})
    assert r.status_code == 201, r.text
    assert len(r.json()["candidates_after"]) < len(r.json()["candidates_before"])
    assert len(r.json()["candidates_after"]) == 1

    # 渗漏假设不可转校正；校正列表为空
    seg = next(h for h in client.get(f"/api/diagnosis/runs/{run_id}").json()["hypotheses"]
               if h["hypothesis_kind"] == "segment_loss")
    r = client.post(f"/api/diagnosis/hypotheses/{seg['id']}/propose-correction",
                    json={"operator": "站长"})
    assert r.status_code == 409
    assert client.get("/api/diagnosis/corrections").json() == []
