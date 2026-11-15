# 灌区多源用水事实数据与指标发布平台

面向灌区「六套系统、多源口径」问题建设的数据治理平台。平台**不替代**轮灌调度、闸门控制与农户通知系统，
只提供经治理的数据集、质量事件、版本化指标和查询接口。核心选择是：

> **保留追加式事实账和可回放指标版本**，而不是只维护一张当前读数表，也不因缺测而停发全部指标。

技术栈：**React 18 + Vite（前端）｜ Python 3.12 + FastAPI（后端）｜ PostgreSQL（追加式存储）**。

---

## 1. 它解决什么问题

| 痛点 | 平台机制 |
| --- | --- |
| 报表把计划量当实灌量 | 计划只生成 `fact_kind=plan / status=target` 的目标事实，**永不充当实灌**；实灌须由读数证据编译 |
| 设备补传悄悄改写昨天数字 | 补传写入**新报文行**并挂 `supersedes` 链，旧行原样保留；事实与指标重算生成**新版本 + 差异说明** |
| 累计表换表后读数倒退 | 有凭证→换表前后拆两段；**无凭证→截断区间、关联渠道量列待核实，绝不解释成负供水** |
| 设备补传与人工补录冲突 | 两份证据都保留，各生成一条 `candidate` 候选事实并登记裁定；**未裁定前不进正式指标** |
| 渠道上下游对不上 | 超 5% 容差即**冻结损耗指标并生成对账任务**，禁止用计划量补平 |
| 对不上却找不到原因 | **异常损耗诊断**：按输送时长对齐上下游、连接处水量平衡定位容差外残差；渗漏/表偏移/倍率/时钟偏差**多假设并列试算**（不凭最小残差独断），复测建议按可区分候选对数排序，现场回报后排除未支持假设 |
| 诊断估计混入正式核算 | 诊断只读暂存区、不写事实账；**校正参数必须批准**才生成追加式 `adjustment` 事实作用于核算，**不直接改原读数**；设备资料变更使待批准校正失效 |
| 数字无法追溯 | 血缘浏览器从发布指标回溯到原始报文、换表记录、映射版本、人工裁定、质量规则、对账任务、校正参数与诊断运行 |

### 质量规则引擎覆盖

`READING_REVERSAL` 读数倒退 · `METER_CHANGE_NO_VOUCHER` / `METER_CHANGE_TRUNCATION` 换表缺证与截断 ·
`UNIT_MISMATCH` 单位不一致 · `TIME_OVERLAP` 时间重叠 · `CLOCK_DRIFT` 时钟漂移 · `GAP_MISSING` 缺测 ·
`CHANNEL_IMBALANCE` 渠道守恒失衡 · `EVIDENCE_CONFLICT` 证据冲突 · `MANUAL_WITHOUT_BASIS` 人工补录缺依据 ·
`LATE_ARRIVAL` 迟到补传 · `CONTRACT_VIOLATION` 契约违规。

---

## 2. 目录结构

```
23/
├── backend/
│   ├── app/
│   │   ├── models.py            # 追加式/SCD2 全部 ORM 模型
│   │   ├── schemas.py           # Pydantic 入参出参
│   │   ├── services/
│   │   │   ├── ingestion.py     # 契约登记 + 暂存接入（原样留痕、幂等、补传链）
│   │   │   ├── master_data.py   # 水系/地块/批次/SCD2 映射/换表（变更使待批准校正失效）
│   │   │   ├── fact_compiler.py # 事实编译器（差量/积分/截断/候选/目标分离）
│   │   │   ├── quality.py       # 质量规则引擎 + 守恒对账冻结
│   │   │   ├── metrics.py       # 指标版本服务（水位线/稳定期/差异说明/校正量单列）
│   │   │   ├── diagnosis.py     # 异常损耗诊断：对齐平衡/假设试算/复测建议/校正参数
│   │   │   ├── lineage.py       # 血缘浏览器
│   │   │   ├── workflow.py      # 人工裁定与对账处置
│   │   │   └── ledger.py        # 追加式账本期视图（多值取代链）
│   │   ├── routers/             # FastAPI 路由（含 diagnosis.py 诊断接口）
│   │   └── main.py              # 应用入口（生产模式托管前端 dist）
│   ├── scripts/seed.py          # 样例数据（治理场景 + 渗漏/表漂移/证据不足诊断场景）
│   ├── tests/                   # pytest：治理硬规则 + 诊断硬规则 + API 端到端
│   └── requirements.txt
├── frontend/                    # React 18 + Vite + React Router
│   └── src/pages/               # 10 个业务页面（含异常损耗诊断）
├── docker/entrypoint.sh
├── Dockerfile                   # 多阶段构建（前端构建 + 后端运行时）
├── docker-compose.yml           # 一键启动（PostgreSQL + 应用，自动建表/种子）
└── run_local.sh                 # 本机（预装运行时）一键启动
```

---

## 3. 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
# 启动后访问
#   前端 + API 同源：  http://localhost:8000/
#   交互式 API 文档：  http://localhost:8000/docs
```

`entrypoint.sh` 会自动：等待数据库就绪 → 建表 → 写入样例数据（含迟到补传、换表缺证、渠道失衡、证据冲突）。
重新开始：`docker compose down -v && docker compose up --build`。

### 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `admin` | `admin123` | 灌区管理员（可人工裁定、对账处置、触发发布） |
| `viewer` | `viewer123` | 运行查看员 |

> 本平台为数据治理后端 + 展示前端，轮灌调度/闸门控制/农户通知仍在既有业务系统，故内置的是演示级登录。

---

## 4. 本地开发启动（容器/裸机已预装 Python、Node、PostgreSQL 时）

```bash
# 后端
cd backend
/opt/python-3.12/bin/python3 -m venv ../.venv
../.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
cp .env.example .env                      # 按需修改数据库连接
../.venv/bin/python -m scripts.seed       # 建表 + 样例数据
../.venv/bin/uvicorn app.main:app --reload --port 8000

# 前端（另开终端，开发模式带 /api 代理）
cd frontend
/opt/node-22/bin/npm install --registry=https://registry.npmmirror.com
/opt/node-22/bin/npm run dev              # http://localhost:5173
```

或直接执行根目录 `./run_local.sh`（自动建虚拟环境、建库、写种子、起后端并托管已构建前端）。

---

## 5. 验证方式

### 5.1 自动化测试

```bash
cd backend
# 测试库（与开发库隔离）
PGPASSWORD=ccdev /opt/postgresql-18/bin/psql -h 127.0.0.1 -U ccdev -d postgres \
  -c "CREATE DATABASE irrigation_test OWNER ccdev;" 2>/dev/null || true
../.venv/bin/python -m pytest tests/ -v
```

测试覆盖（`tests/test_governance.py`、`tests/test_diagnosis.py`、`tests/test_api.py`）：

1. **读数倒退永不产生负供水**，倒退区间落 `pending_verification` 并出质量事件；
2. **换表无凭证即截断**，生成两条 `truncated` 待核实事实；
3. **证据冲突双候选、未裁定不进指标**，人工裁定采信设备后重编译转为确认；
4. **渠道失衡冻结损耗指标且不用计划补平**（`loss_lower/upper` 置空、指标 `frozen`）；
5. **迟到补传生成指标新版本并写差异说明**，旧报文与旧版本均保留；
6. **诊断：输送时长对齐与容差外残差定位**（平衡窗口不产生假设）；
7. **诊断：多假设并列不独断**（渗漏与表偏移/倍率同样成立时全部候选）；
8. **诊断：复测回报真正改变候选集合**（未支持假设被排除并留排除原因）；
9. **诊断：证据不足保持 `inconclusive`**，复测后仍多候选则明确不下结论；
10. **校正参数必须批准才作用于核算**：批准前指标校正量为 0，批准后生成追加式
    `adjustment` 事实且**原读数不变**；校正量单列 `correction_volume`，不混入确认实灌；
11. **渠段渗漏假设不可转计量校正**（真实水损失走工程处置）；
12. **设备资料变更（换表/映射调整）使待批准校正失效**，失效后可重新提出；
13. **迟到数据形成新诊断运行**，已采用诊断的状态与假设不被覆盖；
14. API 端到端：登录、契约登记、幂等接入、编译、发布、血缘链路、版本回放、
    诊断运行/假设/复测建议/回报/校正接口。

### 5.2 样例场景手工验证（种子脚本已实际触发）

种子叙事日期为 **2026-11-10（UTC）**，运行 `python -m scripts.seed` 后可在界面核对：

| 场景 | 触发方式 | 期望可见结果 |
| --- | --- | --- |
| 迟到补传 | 渠首 `D-HEAD-01` 末日读数 600 于 11-12 补传为 620 | 暂存区两条报文（rev1/rev2，旧行保留）；`LATE_ARRIVAL` 事件；MAIN 渠道指标出现新版本，差异说明「确认实灌量: 600.0 -> 620.0」 |
| 换表缺证 | `D-B1-01` 02:30 换表、新表首读数 5、无凭证 | `METER_CHANGE_NO_VOUCHER` + `METER_CHANGE_TRUNCATION`；两条截断待核实事实（待核实 55m³）；无任何负值 |
| 渠道失衡 | 渠首确认 620，两支渠可解释仅 506.5（差异率 18.31% > 5%） | 对账任务 `frozen`；MAIN 损耗区间冻结为「冻结/不适用」；质量事件 `CHANNEL_IMBALANCE` |
| 证据冲突 | `D-B2-01` 01:00–02:00 同时有设备补传与人工补录 | 两条 `candidate` 事实 + 一条待裁定记录；未裁定前确认量不含该区间；裁定并重编译后才转正 |
| 附带规则 | 单位错填 `L`、05:00 缺测、03:00 时钟偏快、无依据人工补录 | `UNIT_MISMATCH`、`GAP_MISSING`、`CLOCK_DRIFT`、`MANUAL_WITHOUT_BASIS` 各 1 条 |

**血缘验证**：在「指标版本」页任选指标点「血缘」，可看到 `发布指标 → 供水事实 → 原始报文/人工证据 → 数据契约/映射版本/换表记录/人工裁定/质量事件/对账任务` 的完整有向图（样例 MAIN 指标约 9 类节点、40+ 条边），每个节点可点开查看留痕字段。

**追溯闭环操作路径**：
`质量事件与裁定` 页裁定冲突 → `供水事实账` 点「重新编译事实」→ `指标版本` 点「发布新版本」→
查看新版本差异说明 → `对账与重算` 页处置对账任务 → 任一版本「血缘」回看全部输入与规则。

### 5.3 异常损耗诊断场景手工验证（西干渠 WEST，种子已实际触发）

诊断叙事日期为 **2026-11-11 ~ 11-13（UTC）**，在「异常损耗诊断」页核对：

| 场景 | 触发方式 | 期望可见结果 |
| --- | --- | --- |
| 渠段额外渗漏（11-11） | 渠首两档流量 225/112 m³/h，W2 后段停灌；残差 [35,35,22,22,22] 超出允许区间 | 诊断运行#1：5 个候选并列（渠段渗漏 + 渠首/W1 表偏移与倍率，W2 被停灌时段排除）；复测建议渠首/W1 各可区分 6 对；回报（渠首 225→224.5、W1 90→90.8）后候选 5→1 收敛为渠段渗漏；对账任务工程处置闭环，**不生成校正参数** |
| 单表漂移（11-12） | W2 表过水时每小时少计约 12 m³（真值 100/30，表读 88/18） | 诊断运行#2：6 个候选（W2 倍率被流量变化排除）；回报三块表（渠首 194→193.5、W1 90→90.1、W2 88→99.8）后候选 6→1 收敛为 W2 固定偏移；校正参数提出（5 段受影响区间、合计 +59.3 m³）|
| 校正批准与失效 | 校正待批准期间登记 W2 换表（11-13 00:30，有凭证） | 待批准校正 **失效**（原因留痕）；重新提出后管理员批准，生成 5 条追加式 `adjustment` 事实；W2 渠道 11-12 指标出现 `校正调整量 59.3`，确认实灌不变，原读数不变 |
| 迟到数据（11-12 10:00） | W2 06:00 读数补传 2618→2610（晚于诊断截止 08:00） | 形成新诊断运行#3（`trigger=late_data`，新观测截止点）；旧运行#2 保持 `converged/adopted` 不被覆盖 |
| 证据不足（11-13） | 各表仅 00:00/06:00 两个读数（中间缺测，稀疏均摊） | 诊断运行#4：7 个候选并列并标注「读数稀疏」；回报 W1（90→89.7）后候选 7→5，状态 `inconclusive`，明确提示「证据不足，不能下结论」；WEST 对账任务保持冻结 |

**诊断页操作路径**：`异常损耗诊断` → 点运行「详情」看逐时段平衡与并列假设 →
「现场回报」填报便携流量计观测（可多位置同报）→ 候选集合缩小 →
对收敛的仪表假设「提出校正参数」→ 管理员在页面下方「校正参数」区批准/驳回 →
`指标版本` 查看校正调整量（与确认实灌分列）→ 「血缘」回看 指标→调整事实→校正参数→诊断假设→诊断运行 链路。

---

## 6. 关键设计说明

- **追加式事实账**：`supply_facts` 行永不物理删除或原地改写。重算时新事实通过
  `supersedes_fact_id`（单值，兼容链）与 `replaces_fact_ids`（JSONB 多值，覆盖双候选场景）取代旧事实；
  指标 `metric_versions` 同理，旧版本以 `superseded_by` 指向新版本并保留 `diff_note`。
- **SCD2 主数据**：设备→计量点映射、地块→批次分配均带 `effective_from/effective_to` 与 `mapping_version`，
  事实行固化当时的映射版本号，血缘可按版本回放。
- **事件时间 / 水位线 / 稳定期**：指标按事件时间归集，水位线 = 发布时刻 − 允许迟到时长（15 分钟），
  稳定期 30 分钟；含迟到、缺测、待核实、未裁定候选的指标标 `tentative`，被对账冻结的标 `frozen`。
- **损耗区间**：`[max(渠首−支渠−待核实,0), max(渠首−支渠,0)]`，待核实带来区间不确定性；渠道冻结时整体置空。
- **缺测不停发**：缺测只折算 `missing_rate` 并产生事件，其余指标照常发布并标暂定状态。
- **异常损耗诊断**：
  - 输入正常损耗区间（渠道参数 `normal_loss_min/max_ratio`）、仪表精度（计量点 `accuracy_ratio`）、
    连续读数（暂存区，受观测截止点约束），按输送时长（渠道 `travel_minutes`）对齐上下游，
    在连接处建立逐时段水量平衡，只对误差允许区间（正常损耗 ± 仪表合成不确定度）之外的残差定位；
  - 对每台候选仪表试算固定偏移（仅过水时段作用）与倍率异常、对汇合渠段试算额外损耗、
    对时钟偏差试算序列时移；每个假设记录**能解释的观测**与**仍不相容部分**，
    完全相容者成为候选；**多种解释同样成立时并列，绝不凭最小残差判定故障**；
  - 复测建议按「在该位置补一段读数可区分的候选假设对数」排序，给出位置、时间窗与所需设备；
    现场回报（便携流量计实测真值 + 该表 concurrent 读数，可多位置同报）后重算，
    预测区间不含观测值的假设被排除并留痕；
  - 诊断引擎只读暂存区、不写事实账（**诊断估计与正式实灌分离**）；
    仅仪表类候选/被支持假设可提出校正参数（渠段渗漏走工程处置），
    校正**必须批准**才按「全部受影响区间及差额」生成追加式 `adjustment` 事实作用于核算，
    **不直接改原读数**，校正量在指标中单列 `correction_volume`；
  - 诊断运行创建时**固定观测截止点**，迟到数据形成新运行（`trigger=late_data`），
    旧结果不覆盖已采用（`adopted`）诊断；设备资料变更（换表/映射调整）使待批准校正**失效**。

## 7. 主要 HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 演示登录 |
| POST/GET | `/api/contracts` | 数据契约登记/查询 |
| POST/GET | `/api/ingestion/raw`、`/api/ingestion/manual` | 设备报文/人工修订接入（幂等、原样留痕） |
| POST/GET | `/api/master/*` | 渠道、计量点、地块、批次、SCD2 映射、换表登记 |
| POST | `/api/compile` | 事实编译 + 质量规则全量检查 |
| GET | `/api/facts`、`/api/quality/events` | 事实账、质量事件 |
| GET/POST | `/api/adjudications` | 证据冲突裁定 |
| GET/POST | `/api/reconciliations/{id}/resolve` | 对账任务与处置 |
| POST/GET | `/api/metrics/publish`、`/api/metrics` | 指标版本发布/查询 |
| GET | `/api/metrics/{id}/versions`、`/api/metrics/{id}/lineage` | 版本回放与血缘图 |
| GET | `/api/recompute-runs` | 重算任务登记 |
| POST | `/api/diagnosis/run` | 发起异常损耗诊断（固定观测截止点） |
| GET | `/api/diagnosis/runs`、`/api/diagnosis/runs/{id}` | 诊断运行列表/详情（并列假设、逐时段平衡） |
| GET | `/api/diagnosis/runs/{id}/retests` | 复测建议（按可区分候选对数排序） |
| POST | `/api/diagnosis/retests/{id}/report` | 现场回报（重算并排除未支持假设） |
| POST | `/api/diagnosis/hypotheses/{id}/propose-correction` | 提出校正参数（待批准） |
| GET/POST | `/api/diagnosis/corrections`、`.../{id}/approve`、`.../{id}/reject` | 校正参数查询/批准/驳回 |

完整 schema 见运行后的 `http://localhost:8000/docs`（FastAPI 自动生成的 OpenAPI）。
