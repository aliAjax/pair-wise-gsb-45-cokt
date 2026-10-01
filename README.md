# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。
在原有靠泊状态机之上，接入潮汐窗口、拖轮/引航资源，形成一条**可恢复的进港排班**链路。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、潮汐窗口校验和冲突分类。
- `src/scheduler.py`：排班引擎（潮汐窗口匹配、拖轮/引航/泊位整点占用、容量排队）。
- `src/repository.py`：SQLite建表、事务和查询（记录、排班、批次、潮汐、系统事件）。
- `src/service.py`：用例编排、批次幂等合并、潮汐重算、资源读取故障注入和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与可恢复排班测试。

## 排班语义

- 同一拖轮、同一引航员、同一泊位在同一整点小时只服务一艘船；容量不足顺延为`queued`排队。
- 排班结果：`pending`（窗口内排出，待值班员确认）、`queued`（容量不足排队）、`conflict`（潮汐窗口内进水不足）、`active`（已确认占用资源）、`berthed`（已靠泊锁定）。
- 潮汐窗口变更（`plan_version`递增）：未靠泊计划全部作废旧排班并重算；已`confirmed`未靠泊的回退`draft`重新确认；已`berthed`的结果保持不变。
- 窗口改宽后，只恢复**系统重排**（`system_managed`）的计划；值班员/用户主动`cancel`的永不恢复；`restored`只计上一版`conflict`、本版重新可排入的计划。
- 批次重复提交按`plan_version`幂等合并，已有活跃排班直接返回原结果，不重复占用拖轮。
- 资源系统读取失败：预取阶段失败整批拒绝（503）；批次处理中途失败则保留已受理条目（批次`partial`），返回`retry_record_ids`，重试时已受理的合并、未处理的重排。
- 接口、列表和审计均返回三个核心数量：`pending_confirmation`（待确认，含排队）、`conflict`（冲突）、`restored`（恢复），另附`queued`。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表并播种默认潮汐窗口（0-6、12-18，水位12m）与2拖轮2引航员。

## 主要接口

记录与状态机：

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计，含`schedule_counts`（待确认/冲突/恢复/排队）。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作（confirm/berth/depart/cancel）。

进港排班：

- `GET /api/plans`：靠泊计划列表，每条记录附带最近排班，顶层返回`counts`。
- `POST /api/batches`：批次提交排班，请求体`{"record_ids":[1,2]}`；成功201，资源中途不可读202（保留已受理批次，含`failure.retry_record_ids`）。
- `POST /api/records/{id}/actions/confirm_schedule`：值班员确认系统排班（使用排班里的拖轮/引航员）。
- `GET /api/tide-windows`：当前潮汐窗口与`plan_version`。
- `POST /api/tide-windows`：变更潮汐窗口`{"tide_windows":[{"start_hour":12,"end_hour":18,"water_level_m":13.0}]}`，未靠泊计划即时失效重算。
- `GET /api/resources?kind=tug|pilot`：拖轮/引航员列表。
- `POST /api/resources`：登记或停用资源。
- `POST /api/resources/check`：主动探测资源系统是否可读。
- `GET /api/system-events`：系统级审计（潮汐重算、批次提交），每条事件带待确认/冲突/恢复数量。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、潮汐收窄/放宽恢复、已靠泊锁定、容量排队、批次重复提交幂等、资源读取失败保留已受理批次与重试合并。
