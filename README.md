# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。

## 可恢复的繁育配额账

在通用实体之外，`src/quota.py` + `src/rules.py` 实现了一套可恢复的繁育名额账：

- **许可证（permits）**：一条许可证对应一个物种和一个年度，带名额、有效期与版本号。
  同一物种同一年度只允许一张有效许可证；非名额信息可 `amend`，**旧批准仍可继续执行**。
- **配对占用（pairing 实体）**：`proposed → approved/queued → executed`，
  失效为 `voided`（带中文失效原因）。批准在单条 `BEGIN IMMEDIATE` 事务内
  “读现占用—决策—落账”，两人同时提交时只有一笔占名额，后来者拿到剩余名额或排队，
  排队按提交先后、不插队。
- **重算排队**：许可证下调名额时，超出容量的未执行批准自动失效
  （`void_reason=quota_reduced`），排队项在有名额时递补；到期/撤回时所有未执行的
  批准和排队项全部失效（`permit_expired` / `permit_withdrawn`），
  **已完成（executed）结果永久保留**。完成的配对永久占用该年度名额，不释放名额。
- **运输放行（release 实体）**：与配对批准分开记录，只允许对已完成配对开具，
  保存许可证编号与版本快照；之后许可证变更/撤回不影响已完成放行。
- **配额流水（quota_entries）**：`reserve / enqueue / promote / requeue /
  execute / void` 全部带记账后余额，账可逐条重放核对。
- **外部登记对账（registry_jobs / registry_receipts）**：回执按 `receipt_no` 去重，
  重复回传只登记关联、不重处理；晚到回执与现占用冲突（许可证不存在、状态不一致、
  占用数超名额）时保留为 `pending` 待处理项；`retry` 只续做 pending 项，
  人工可 `accept`（以本地账为准）或 `exempt`（登记豁免/差错）。所有状态落 SQLite，
  服务重启时 `recover_on_startup()` 自动续核未完成项。

### 配额账接口

身份仍通过 `X-User-Id` / `X-Role` 传入（admin / registrar / coordinator / registry）。

- `POST /api/permits`：发证；`GET /api/permits`：列表。
- `GET /api/permits/<no>/ledger`：许可证台账（占用、排队、完成、失效、配对、流水）。
- `POST /api/permits/<no>/amend`：变更非名额信息；
  `POST /api/permits/<no>/adjust-quota`：调整名额并重算排队（`{"quota":n}`）；
  `POST /api/permits/<no>/expire` / `withdraw`：到期 / 撤回。
- `POST /api/pairings`：提交建议（`permit_no`、`species`）；
  `POST /api/pairings/<id>/approve`：批准（`sire_id`、`dam_id`，并发安全）；
  `POST /api/pairings/<id>/execute`：完成（结果保留）；
  `POST /api/pairings/<id>/reject`：取消未执行项（释放名额并重算）；
  `GET /api/pairings?permit_no=&status=`：查询，`voided` 项含失效原因。
- `POST /api/releases`：运输放行（`pairing_id`、`from_institution`、`to_institution`）。
- `POST /api/registry-jobs`：新建对账批次；
  `POST /api/registry-jobs/<no>/receipts`：回传一批回执（`{"receipts":[...]}`）；
  `POST /api/registry-jobs/<no>/retry`：只续做未完成项；
  `GET /api/registry-jobs/<no>`：批次与全部回执/重复关联；
  `GET /api/pending-receipts`：全部待处理项；
  `POST /api/receipts/<no>/resolve`：`{"resolution":"accept|exempt","note":...}`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
