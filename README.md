# 增加跨村安置资源替代编排基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景，以及跨村安置资源替代编排（安置点与房源目录、家庭约束登记、候选方案生成、原子确认、入住与取消、资源谱系）；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 跨村安置资源替代编排

区级搬迁专班为危房家庭安排过渡住房时，原定小区房源不足，相邻乡镇的周转房、集中安置房和宅基地指标在通勤、无障碍和家庭人口条件上可以互相替代。平台不再按单一资源排队，而是按以下流程编排：

1. **资源登记**（`planner`）：安置点登记基础设施容量（可安置户数），房源与宅基地指标登记面积、容纳人口、无障碍、通勤分钟、就学就医距离和地块限制（`eligible_townships` 限定哪些迁出乡镇可用）；房源可冻结（`suspended`）或退役（`retired`）。
2. **申请登记**（`dispatcher`）：经办人登记有顺序的家庭约束（`constraints`，按重要程度排序，未列入的已登记约束一律硬性不可降级）、最低面积、就学就医半径和可接受的降级范围（`downgrade_scope`：可替代资源类型顺序、面积/通勤/半径的最大放宽）。
3. **候选生成**：系统基于冻结的房源版本、地块限制和基础设施容量生成候选方案，单套容纳不下时在同安置点组合多套；响应说明每个候选的取舍（`tradeoffs`）和全局未满足条件（`unmet_conditions`）。输入快照不变时重放返回同一批方案。
4. **原子确认**：确认时在同一事务内逐项校验并占用全部关联资源与安置点容量，任何资源或安置点版本变化都使整单确认失败、不产生任何占用；相同幂等键重放返回首次确认结果，不会重复占用。
5. **入住与取消**：入住后家庭不能被自动换房（禁止重新生成候选）；取消申请只释放尚未交付的预留，已交付入住的占用保留并在响应中列明。
6. **资源谱系**：`GET /relocation/applications/{id}/lineage` 返回申请、方案、预留（冻结版本与当前版本对照）和审计事件，供经办与审计核对。

### 安置接口

- `POST /resettlement/sites`、`GET /resettlement/sites/{id}`、`POST /resettlement/sites/{id}/state`
- `POST /resettlement/resources`、`GET /resettlement/resources/{id}`、`POST /resettlement/resources/{id}/state`
- `POST /relocation/applications`（幂等键防重复登记）
- `POST /relocation/applications/{id}/candidates`（生成/重放候选方案）
- `POST /relocation/plans/{id}/confirm`（原子确认，`expected_revision` + 幂等键）
- `POST /relocation/applications/{id}/move-in`（交付入住）
- `POST /relocation/applications/{id}/cancel`（只释放未交付预留）
- `GET /relocation/applications/{id}/lineage`（资源谱系）

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

三条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、跨村安置替代编排、危房测量分析和改造审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
