# 邮件别名路由判定服务

邮件投递前的**别名路由判定服务**。给定收件地址与已发布版本，服务按“精确规则 → 域兜底规则”逐跳展开别名，返回去重后的终点邮箱及各自的完整到达路径。任何一个分支**拒收、无路由或成环**，整次判定失败且不返回部分结果。

- Python 3.11+ / FastAPI / SQLite（零外部中间件）
- 草稿编辑带乐观锁修订号（revision），冲突返回 `409`
- 发布在单个 SQLite 事务内**原子**完成：核对修订号 → 校验所有规则目标可解析且无环 → 生成不可变快照；失败保持旧版
- 历史版本永久可查，判定时指定版本号即可**复现当时结果**

## 路由模型

规则分两类，每条规则要么**拒收**（`reject`），要么**转发到一组目标**（`forward`）：

| 规则类型 | match | 说明 |
|---|---|---|
| `exact` | 完整邮箱地址（如 `team@corp.example`） | 地址精确规则 |
| `domain` | 域名（如 `corp.example`） | 域兜底规则 |

转发目标分两类：

| 目标类型 | 行为 |
|---|---|
| `alias` | 内部别名，**继续逐跳**展开（先查精确规则，再查域兜底） |
| `mailbox` | 终点邮箱，**不再递归** |

逐跳判定规则：

1. 先用完整地址查 `exact` 规则，未命中再用地址域查 `domain` 兜底；都没有 → `no_route`，整次失败；
2. 命中 `reject` → 整次失败；
3. 命中 `forward` → 依次处理其目标：`alias` 继续递归，`mailbox` 记为终点；
4. 展开全部完成后，**终点邮箱去重**（同终点的多条到达路径全部保留），作为成功结果返回；
5. 任一分支出错，立即失败，**不返回任何部分终点**；运行时按路径检测环（`a→b→a`）。

输入地址会做大小写归一（`A@X.IO` → `a@x.io`）与首尾空白裁剪。发布时额外做全局校验：每个 `alias` 目标必须能解析到规则、规则图必须无环。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听在 **http://0.0.0.0:8080**，数据保存在命名卷 `router-data`（容器内 `/data/router.db`）。
容器启动时会先执行 `python -m app.initdb` 幂等建库，再启动 uvicorn。

- 健康检查：`GET http://localhost:8080/healthz`
- 交互式文档：`GET http://localhost:8080/docs`（Swagger UI）

停止 / 清理：

```bash
docker compose down            # 停止
docker compose down -v         # 停止并删除数据卷
```

## 本地直接运行（不用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export ROUTER_DB_PATH=./data/router.db   # 可选，默认 /data/router.db
.venv/bin/python -m app.initdb           # 建库（幂等）
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ROUTER_DB_PATH` | `/data/router.db` | SQLite 数据库文件路径，目录自动创建 |
| `ROUTER_HOST` | `0.0.0.0` | 监听地址 |
| `ROUTER_PORT` | `8080` | 监听端口 |
| `ROUTER_MAX_HOPS` | `100` | 单次展开允许的最大别名跳数（兜底防御） |

## API 一览

所有错误响应统一为：

```json
{"error": {"code": "validation_error", "message": "...", "details": {...}}}
```

### 1. 路由判定

```bash
POST /api/v1/resolve
GET  /api/v1/resolve?recipient=<邮箱>&version=<版本号|latest>
```

请求：`{"recipient": "team@corp.example", "version": 1}`（`version` 省略或传 `"latest"` 表示当前发布版）

成功 `200`：

```json
{
  "recipient": "team@corp.example",
  "version": 1,
  "endpoints": [
    {"address": "a@corp.example", "paths": [
      ["team@corp.example", "sales@corp.example", "west@corp.example", "a@corp.example"]
    ]},
    {"address": "boss@corp.example", "paths": [
      ["team@corp.example", "boss@corp.example"]
    ]}
  ]
}
```

失败 `422`（不携带任何终点），`error.code` 取值：`rejected` / `no_route` / `cycle` / `max_hops_exceeded`，`details.path` 给出出错分支路径：

```json
{"error": {"code": "no_route", "message": "无路由规则: x@other.example",
  "details": {"version": 1, "recipient": "x@other.example", "path": ["x@other.example"]}}}
```

### 2. 草稿编辑（乐观锁）

每个写请求都必须携带**当前所见的修订号** `expected_revision`；不匹配返回 `409`，成功后修订号 +1。

```bash
# 查看当前草稿
GET /api/v1/draft

# 整份替换（首次 expected_revision=0）
PUT /api/v1/draft
{
  "expected_revision": 0,
  "rules": [
    {"rule_type": "exact", "match": "team@corp.example", "action": "forward",
     "targets": [
       {"kind": "alias", "address": "sales@corp.example"},
       {"kind": "mailbox", "address": "boss@corp.example"}]},
    {"rule_type": "exact", "match": "sales@corp.example", "action": "forward",
     "targets": [{"kind": "alias", "address": "west@corp.example"}]},
    {"rule_type": "exact", "match": "west@corp.example", "action": "forward",
     "targets": [{"kind": "mailbox", "address": "a@corp.example"}]},
    {"rule_type": "domain", "match": "corp.example", "action": "reject", "targets": []}
  ]
}

# 单条新增/更新（按 rule_type+match 标识）
PUT /api/v1/draft/rules
{"expected_revision": 1, "rule_type": "exact", "match": "west@corp.example",
 "action": "forward", "targets": [{"kind": "mailbox", "address": "b@corp.example"}]}

# 删除单条（参数走 query，正文带修订号）
POST /api/v1/draft/rules/delete?rule_type=exact&match=west@corp.example
{"expected_revision": 2}
```

输入约束（违反返回 `422`）：无效邮箱/域名、重复规则、转发规则空目标、拒收规则携带目标、规则内重复目标。

### 3. 发布（原子、不可变）

```bash
POST /api/v1/publish
{"expected_revision": 1}
```

- 修订号不符 → `409`；
- 存在不可解析的 alias 或规则环 → `422`，返回全部问题，**旧发布版保持不变**；
- 成功 → `200`，返回新版本号、对应草稿修订号、规则数、内容校验和与完整快照。

### 4. 历史版本

```bash
GET /api/v1/versions            # 列表（新到旧）
GET /api/v1/versions/{version}  # 某版本完整快照
POST /api/v1/resolve            # body 里 "version": 1 即按历史版复现结果
```

版本快照一经生成不再修改；之后无论草稿如何变更，带旧版本号的判定结果始终与发布时一致。

## 调用示例（curl）

```bash
# 1) 写入草稿
curl -s -X PUT http://localhost:8080/api/v1/draft \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"rules":[
    {"rule_type":"exact","match":"team@corp.example","action":"forward","targets":[
      {"kind":"alias","address":"sales@corp.example"},
      {"kind":"mailbox","address":"boss@corp.example"}]},
    {"rule_type":"exact","match":"sales@corp.example","action":"forward","targets":[
      {"kind":"mailbox","address":"a@corp.example"}]},
    {"rule_type":"domain","match":"corp.example","action":"forward","targets":[
      {"kind":"mailbox","address":"catch@corp.example"}]}]}'

# 2) 发布
curl -s -X POST http://localhost:8080/api/v1/publish \
  -H 'Content-Type: application/json' -d '{"expected_revision":1}'

# 3) 判定（精确展开）
curl -s -X POST http://localhost:8080/api/v1/resolve \
  -H 'Content-Type: application/json' \
  -d '{"recipient":"team@corp.example"}'

# 4) 判定（域兜底）
curl -s 'http://localhost:8080/api/v1/resolve?recipient=nobody@corp.example'

# 5) 按历史版本复现
curl -s -X POST http://localhost:8080/api/v1/resolve \
  -H 'Content-Type: application/json' \
  -d '{"recipient":"team@corp.example","version":1}'
```

## 运行测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
```

覆盖：逐跳精确优先/域兜底、多分支展开、终点去重与路径保留、菱形展开、拒收/无路由/成环整次失败、地址归一、重复规则与空目标拒绝、乐观锁冲突、发布校验（环/不可解析）、发布失败保持旧版、历史版本复现。

## 目录结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接、建表、BEGIN IMMEDIATE 事务
  validators.py # 邮箱/域名校验与归一化
  engine.py     # 展开算法、路径/环检测、发布前图校验
  schemas.py    # Pydantic 请求模型
  service.py    # 业务服务：草稿编辑、原子发布、版本与判定查询
  main.py       # FastAPI 路由
  initdb.py     # 独立建库入口（容器启动时执行）
tests/          # pytest 用例
Dockerfile
docker-compose.yml
requirements.txt
```
