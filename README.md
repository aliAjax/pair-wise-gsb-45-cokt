# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `src/scheduling/types.py`：排班计划状态、潮汐/资源校验与互斥资源池。
- `src/scheduling/planner.py`：潮汐可行小时、拖轮/引航员/泊位互斥分配、排队与窗口重算。
- `src/scheduling/repository.py`：排班表结构与按计划版本合并的重排事务。
- `src/scheduling/resources.py`：拖轮/引航员资源读取（可注入读取失败）。
- `src/scheduling/service.py`：批次幂等受理、潮汐重算、故障重试、靠泊冻结与数量口径。
- `static/index.html`：排班演示页面。
- `tests/`：完整流程、规则计算、失败场景与排班端到端测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 潮汐进港排班接口

排班状态：`pending`（待确认/排队）、`scheduled`（已排班）、`berthed`（已靠泊，结果冻结）、`invalid`（系统重排失效）、`cancelled`（用户取消，永不自动恢复）。

- `POST /api/scheduling/resources`：维护拖轮/引航员目录，`{"tugboats":["T1"],"pilots":["P1"]}`，更新后自动重排。
- `POST /api/scheduling/tide`：下发24小时潮位表 `{"tide_levels":[1.2,...24项]}`。窗口一变，未靠泊计划全部按新版本重算；`berthed`保持原结果；窗口放宽后只有系统置为`invalid`的计划重新获得分配（计入recovered）。
- `POST /api/scheduling/batches`：批次提交进港申请，幂等。请求体：
  ```json
  {"batch_id":"B-0001","client_key":"client-0001","plans":[
    {"vessel":"海云号","berth":"B12","draft_m":10.2,"vessel_length_m":180,
     "start_hour":6,"service_hours":3}]}
  ```
  同一拖轮/引航员/泊位在同一半开时段只服务一艘船，容量不足在当日内顺延，全天排不下为`pending`排队。重复`batch_id`/`client_key`返回已受理结果（`replay:true`），重复船舶进入`conflicts`，均不会重复占用拖轮。资源读取失败时批次保留为`waiting_resources`。
- `POST /api/scheduling/retry`：资源恢复后重试，在单个事务内按计划版本合并：已靠泊/已取消或快照后被更新版本处理的计划跳过，杜绝重复占用。仍失败返回503 `resource_unavailable`。
- `POST /api/scheduling/plans/{id}/berth`：靠泊确认，`{"actual_draft_m":10.3}`；校验进港时刻潮位富余，成功后分配冻结。
- `POST /api/scheduling/plans/{id}/cancel`：`{"cancel_reason":"..."}`，释放容量后排队计划补位；已取消计划不会被后续重排恢复。
- `GET /api/scheduling/plans?state=&batch_id=`：计划列表。
- `GET /api/scheduling/batches/{batch_id}`：批次详情。
- `GET /api/scheduling/audit?scope=&ref=`：排班审计时间线。

接口响应、列表与审计统一返回数量口径 `counts`：

```json
{"counts": {"pending": 0, "conflicts": 1, "recovered": 0,
            "scheduled": 1, "berthed": 0, "cancelled": 0}}
```

写接口额外返回本次 `operation`（`scheduled/pending/conflicts/recovered/invalidated/merged/skipped`）与逐条 `conflicts` 列表。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
