# 增加跨村安置资源替代编排基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
```

三条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析和改造审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 跨村安置资源替代编排

`rural_allocation` 服务在原有土地调度之外，提供危房家庭跨村过渡安置的替代资源编排。周转房、集中安置房和宅基地指标在通勤、无障碍、面积、人口和半径条件满足时可互相替代。

### 角色权限

- `planner`：登记安置点（`relocation.catalog.write`）与安置资源；
- `dispatcher`：登记家庭申请、生成候选方案、原子确认、交付入住和取消；
- `risk` / `auditor`：可查询方案与审计哈希链。

### 接口

所有写接口接受 `X-Actor-Id` 请求头，并要求请求体携带 `idempotency_key`；相同业务请求（同键同内容）重放返回首次响应，不会重复占用资源；同键不同内容返回 `409 conflict`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/relocation/sites` | 登记集中安置点及基础设施容量（户数） |
| POST | `/relocation/resources` | 登记周转房、集中安置房或宅基地指标（含面积、床位、无障碍、就学/就医/通勤公里数、质量等级；宅基地可带 `restrictions.eligible_villages` 地块限制） |
| POST | `/relocation/applications` | 登记家庭申请：有顺序的 `constraints`（每项标记 `required`，非必需条件只产生妥协提示）、`preference_order` 资源类型替代序列、`max_downgrade` 可接受降级级数 |
| POST | `/relocation/plans` | 在单个事务内冻结房源、地块限制和安置点容量快照，生成候选方案 |
| POST | `/relocation/plans/{plan_id}/confirm` | 携带 `candidate_id` 与 `expected_revision` 原子占用房源和容量 |
| POST | `/relocation/plans/{plan_id}/deliver` | 办理交付入住，资源转为 `occupied` |
| POST | `/relocation/applications/{household_id}/cancel` | 取消申请，只释放尚未交付的预留 |
| GET | `/relocation/plans/{plan_id}` | 查询方案：候选取舍、淘汰原因、未满足条件、确认后的资源谱系 |
| GET | `/relocation/households/{household_id}/genealogy` | 查询家庭全部资源谱系（预留/入住/释放链路） |

### 候选取舍与未满足条件

`POST /relocation/plans` 的响应说明编排结论：

- `candidates`：按家庭偏好顺位、妥协条件数量、质量等级、面积和半径确定性排序的可行候选，每项给出 `preference_rank`、`downgrade_level` 与需要家庭确认的 `compromises`；
- `rejected`：被硬性条件淘汰的房源及逐条原因（面积、无障碍、人口、半径、`downgrade` 超出可接受降级、`plot_restriction` 地块限制、`site_capacity` 基础设施容量）；
- `unmet_conditions`：按偏好顺位汇总没有候选的资源类型及其阻断原因，以及只在部分候选上需要妥协的非必需条件；
- `snapshot_sha256`：冻结快照（含每个资源与安置点的版本号）摘要。

### 原子确认与版本失效

确认在一个 `BEGIN IMMEDIATE` 事务内完成：依次校验方案版本、房源版本与状态、安置点容量版本与剩余容量，全部通过后才占用资源、递增容量并写入谱系；**方案、房源或容量任一版本变化，整单确认失败并回滚**，不会出现部分占用。冲突返回 `409 invalid_state` 并说明变化环节。

### 入住保护与取消释放

- 资源状态为 `available → reserved → occupied`；只有 `reserved`（已确认未交付）可以随取消释放回 `available` 并回退安置点占用户数。
- 已经 `occupied`（入住）的家庭：取消被拒绝，系统不会自动换房；谱系接口明确标注 `auto_reassignment: false`。
- 谱系（`resource_genealogy` / `genealogy`）记录每次占用的资源、安置点、占用时版本与当前版本、预留/入住/释放时间，并通过 `parent_occupancy_id` 串联人工调整链路。

