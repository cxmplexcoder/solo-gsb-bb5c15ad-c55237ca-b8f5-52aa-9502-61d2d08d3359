# 设备工程测量计算 API（FastAPI）

客户端提交包含**数值与单位变量**的工程算式，服务端完成表达式解析、
**量纲传播**、兼容单位自动换算，并拒绝非法运算（不同量纲相加、
除以带单位的测量变量等），返回规范化数值与单位，错误时返回**精确出错位置**。

另提供 **POST /resample**：把带时间戳、数值与单位的测量序列重采样到
**固定步长的规则网格**——相邻样本间隔不超过上限时线性插值，
更大缺口保留为 missing 段（不跨缺口插值），并返回缺口区间与首尾范围。

- Web 框架：FastAPI + Uvicorn
- 数值计算：全程 `fractions.Fraction` 精确有理运算，输出时转 `float`
- 量纲：7 个 SI 基本量纲整数指数向量 `(L, M, T, I, Θ, N, J)`
- 默认端口：**8000**
- 启动时初始化内置单位表（1700+ 名称，含 SI 前缀自动派生）

## 快速开始

### Docker Compose（推荐）

```bash
# 构建镜像并后台启动
docker compose up --build -d

# 查看启动日志 / 运行状态
docker compose logs -f
docker compose ps

# 健康检查（Compose 健康检查也依赖该端点）
curl http://localhost:8000/health

# 停止并清理
docker compose down
```

服务启动后：

- API 文档（Swagger）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 单位表：<http://localhost:8000/units>

默认把宿主机的 8000 端口映射到容器的 8000 端口；若端口被占用，
修改 `docker-compose.yml` 中 `ports` 的左侧端口（如 `"8080:8000"`）即可。

### 本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements.txt pytest httpx
python3 -m pytest -q
```

## API

### `POST /calculate`

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `expression` | string | 算式，支持 `+ - * /`、括号、一元正负号 |
| `variables` | array | 变量定义 `{name, value, unit}`，`unit` 省略时为无量纲 |
| `unit` | string | 可选，期望的结果单位；省略则返回 SI 规范化单位 |

表达式里可以直接写单位名（单位名作为“1 个该单位”的常量），
也支持数字紧贴单位的隐式乘法：`3 m`、`2 (a+b)`。

#### 示例 1：兼容单位自动换算

```bash
curl -s -X POST http://localhost:8000/calculate \
  -H 'Content-Type: application/json' \
  -d '{"expression":"a + b",
       "variables":[{"name":"a","value":1,"unit":"km"},
                    {"name":"b","value":500,"unit":"m"}]}'
```

```json
{"expression":"a + b","value":1500.0,"unit":"m",
 "dimension":[1,0,0,0,0,0,0],"dimension_text":"m"}
```

#### 示例 2：量纲传播（乘法 / 单位常量构造复合单位）

```bash
curl -s -X POST http://localhost:8000/calculate \
  -H 'Content-Type: application/json' \
  -d '{"expression":"100 m / 10 s"}'
```

```json
{"value":10.0,"unit":"m/s","dimension":[1,0,-1,0,0,0,0],"dimension_text":"m/s"}
```

温度读数用仿射单位，`100 °C` 会换算为绝对温度 `373.15 K`；
加减温度按绝对温度运算；可指定 `"unit":"°C"` 换回摄氏度。

#### 示例 3：不同量纲相加被拒绝（带出错位置）

```bash
curl -s -X POST http://localhost:8000/calculate \
  -H 'Content-Type: application/json' -d '{"expression":"10 m + 5 s"}'
```

```json
{"error":{"code":"DIMENSION_MISMATCH",
          "message":"量纲不一致，不能相加：m 与 s",
          "position":{"offset":5,"length":1,"line":1,"column":6}}}
```

#### 示例 4：除以带单位的变量被拒绝

工程安全约束：**除数不允许依赖任何带量纲的用户变量**
（防止把两个测量值直接相除）。用单位常量构造复合单位（`m/s`）、
或除以无量纲系数均正常。

```bash
curl -s -X POST http://localhost:8000/calculate \
  -H 'Content-Type: application/json' \
  -d '{"expression":"d / t",
       "variables":[{"name":"d","value":100,"unit":"m"},
                    {"name":"t","value":10,"unit":"s"}]}'
```

```json
{"error":{"code":"DIVISION_WITH_UNIT",
          "message":"除数不能依赖带单位的变量 't'（量纲 s）……",
          "position":{"offset":4,"length":1,"line":1,"column":5}}}
```

### `POST /resample`

把带时间戳的测量序列重采样到**固定步长的规则网格**：

1. 校验所有样本单位**量纲一致**，并统一换算到目标单位；
2. 按时间排序（时间戳重复返回 422）；
3. 网格从首个样本时刻开始，以固定步长推进到最后一个不超过末样本时刻的点；
4. 相邻样本间隔 **≤ `max_gap`** 时，落在其间的网格点做**线性插值**；
5. 间隔 **> `max_gap`** 的区间**不跨缺口插值**，落在其中的网格点
   `value = null` 且 `missing = true`，整个区间计入 `gaps`。

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `samples` | array | 样本序列（≥1 个，无需排序）：`{timestamp, value, unit}` |
| `samples[].timestamp` | string \| number | ISO 8601 时间戳（无时区按 UTC，如 `2026-01-01T00:00:00Z`）或 Unix 秒（数字） |
| `samples[].value` | number | 测量值（有限数） |
| `samples[].unit` | string | 测量单位，所有样本量纲必须一致 |
| `unit` | string | 目标单位，量纲须与样本一致 |
| `step` | object | 固定步长 `{value, unit}`，`unit` 为时间单位（缺省 `s`），`value` 必须为正 |
| `max_gap` | object | 最大可插值间隔 `{value, unit}`，格式同上 |

响应：

| 字段 | 说明 |
|---|---|
| `unit` / `dimension` / `dimension_text` | 目标单位及其量纲 |
| `step_seconds` / `max_gap_seconds` | 换算为秒的步长与最大可插值间隔 |
| `sample_count` | 样本数 |
| `range` | 首尾样本时刻 `{start, end}`（ISO 8601，UTC） |
| `points` | 网格点：`{timestamp, value, interpolated, missing}`；`value` 为 `null` 表示 missing |
| `point_count` / `missing_count` | 网格点总数 / missing 点数 |
| `gaps` | 缺口区间列表：`{start, end, duration_seconds}` |

#### 示例：混合单位换算 + 缺口保留

```bash
curl -s -X POST http://localhost:8000/resample \
  -H 'Content-Type: application/json' \
  -d '{"samples":[
        {"timestamp":"2026-01-01T00:00:00Z","value":0,"unit":"m"},
        {"timestamp":"2026-01-01T00:00:10Z","value":1000,"unit":"cm"},
        {"timestamp":"2026-01-01T00:01:40Z","value":100,"unit":"m"},
        {"timestamp":"2026-01-01T00:01:50Z","value":110,"unit":"m"}],
       "unit":"m",
       "step":{"value":5,"unit":"s"},
       "max_gap":{"value":15,"unit":"s"}}'
```

```json
{"unit":"m","dimension":[1,0,0,0,0,0,0],"dimension_text":"m",
 "step_seconds":5.0,"max_gap_seconds":15.0,"sample_count":4,
 "range":{"start":"2026-01-01T00:00:00Z","end":"2026-01-01T00:01:50Z"},
 "points":[
   {"timestamp":"2026-01-01T00:00:00Z","value":0.0,"interpolated":false,"missing":false},
   {"timestamp":"2026-01-01T00:00:05Z","value":5.0,"interpolated":true,"missing":false},
   {"timestamp":"2026-01-01T00:00:10Z","value":10.0,"interpolated":false,"missing":false},
   {"timestamp":"2026-01-01T00:00:15Z","value":null,"interpolated":false,"missing":true}
 ],
 "point_count":23,"missing_count":17,
 "gaps":[{"start":"2026-01-01T00:00:10Z","end":"2026-01-01T00:01:40Z",
          "duration_seconds":90.0}]}
```

（`points` 仅截取前 4 个；`00:00:10` 到 `00:01:40` 间隔 90 s 超过
`max_gap`，其间 17 个网格点全部保留为 missing。）

#### 示例：字段定位的 422 错误

`/resample` 的语义错误统一返回 **HTTP 422**，错误体用 `error.field`
定位出错字段（如 `samples[2].timestamp`、`step.value`、`unit`）：

```bash
curl -s -X POST http://localhost:8000/resample \
  -H 'Content-Type: application/json' \
  -d '{"samples":[{"timestamp":"2026-01-01T00:00:00Z","value":1,"unit":"m"},
                  {"timestamp":"2026-01-01T00:00:00Z","value":2,"unit":"m"}],
       "unit":"m","step":{"value":5,"unit":"s"},"max_gap":{"value":15,"unit":"s"}}'
```

```json
{"error":{"code":"DUPLICATE_TIMESTAMP",
          "message":"时间戳与 samples[0] 重复",
          "field":"samples[1].timestamp"}}
```

网格点数上限为 100 000（步长过小会返回 `INVALID_STEP`），样本数上限为 100 000。

### `GET /units`

返回去重后的全部内置单位：名称、量纲向量、量纲文本、别名、
SI 倍数与温度偏移。

### `GET /health` / `GET /`

健康检查与服务信息。健康检查同时用于 Docker / Compose 的 healthcheck。

## 量纲与换算规则

| 运算 | 规则 |
|---|---|
| `+` / `-` | 两侧量纲必须相同（不同则 `DIMENSION_MISMATCH`，HTTP 422）；先各自换算到 SI 相干单位再运算 |
| `*` | 数值相乘，量纲指数相加 |
| `/` | 除数**不得依赖带单位的用户变量**（`DIVISION_WITH_UNIT`，HTTP 422）；单位常量/无量纲系数允许，量纲指数相减 |
| 除以零 | `DIVISION_BY_ZERO`（HTTP 422） |
| 温度单位（°C/°F） | 仿射换算到 K；`数字*温度单位` 按“读数”处理 |

结果默认以 SI 相干单位规范化输出（`m, kg, s, A, K, mol, cd, Hz, N, Pa, J, W,
C, V, Ω, F, S, Wb, T, H, m/s, m^2, m^3 …`），
也可用 `unit` 参数指定任意同量纲单位（如 `kPa`、`°C`、`g`）。

## 错误码

| code | HTTP | 含义 |
|---|---|---|
| `INVALID_CHAR` | 400 | 非法字符（位置即该字符） |
| `INVALID_NUMBER` | 400 | 无法解析的数字 |
| `EMPTY_EXPRESSION` | 400 | 空表达式 |
| `UNEXPECTED_TOKEN` | 400 | 意外符号（如两个标识符相邻、缺少运算符） |
| `UNEXPECTED_END` | 400 | 表达式意外结束 |
| `MISSING_PAREN` | 400 | 缺少右括号 |
| `INVALID_REQUEST` | 422 | 请求体校验失败（Pydantic） |
| `UNKNOWN_UNIT` | 400 | 未知单位 |
| `DUPLICATE_VARIABLE` | 400 | 变量重复定义 |
| `INVALID_VALUE` | 400 | 变量值不是有限数 |
| `UNDEFINED_VARIABLE` | 422 | 未定义的变量/单位（附拼写建议） |
| `DIMENSION_MISMATCH` | 422 | 加减两侧量纲不一致 |
| `DIVISION_WITH_UNIT` | 422 | 除数依赖带单位的变量 |
| `DIVISION_BY_ZERO` | 422 | 除以零 |
| `UNIT_DIM_MISMATCH` | 422 | `unit` 参数与结果量纲不一致 |
| `NUMERIC_OVERFLOW` | 422 | 结果超出有限数范围 |
| `DUPLICATE_TIMESTAMP` | 422 | `/resample`：样本时间戳重复（`field` 定位后出现的样本） |
| `INVALID_TIMESTAMP` | 422 | `/resample`：时间戳无法解析 |
| `INVALID_STEP` | 422 | `/resample`：步长非正数或网格点数超限 |
| `INVALID_MAX_GAP` | 422 | `/resample`：最大可插值间隔非正数 |

`/resample` 的语义错误（含上表后四行及 `UNKNOWN_UNIT`、`UNIT_DIM_MISMATCH`、
`DIMENSION_MISMATCH`、`INVALID_VALUE`）一律返回 422，并以 `error.field`
给出字段定位（如 `samples[2].unit`、`step.value`、`max_gap.unit`、`unit`）。

所有表达式错误都返回 `error.position`：`offset`（0 基字符偏移）、
`length`、`line`、`column`（1 基行列号）。

## 内置单位

- SI 基本/导出单位：`m kg s A K mol cd rad sr Hz N Pa J W C V Ω F S Wb T H`
- SI 词头自动派生：`km, mm, µm/um, kPa, MHz, mA, kΩ/kohm …`（q–Q 全词头）
- 英制与常用：`in ft yd mi nmi lb oz t gal mph kn hp psi atm bar Torr …`
- 温标：`K °C °F`（仿射换算）
- 角度（无量纲）：`rad ° arcmin arcsec`
- 时间：`min h d week year`
- 常用中文别名：`米/千米/公里、克/千克/公斤、秒/分/小时、英寸/英尺/英里/海里、
  牛/帕/焦/瓦/伏/安/欧/赫兹、升/毫升、公顷/亩、摄氏度 …`

完整清单见 `GET /units`，或在启动日志中查看加载数量。

## 项目结构

```
app/
  units.py       # 量纲向量、单位定义、SI 前缀、内置单位表（启动初始化）
  parser.py      # 词法分析 + 递归下降解析（AST 携带源码位置）
  evaluator.py   # 量纲传播、精确求值、非法运算校验
  resample.py    # 重采样核心：排序、规则网格、线性插值、缺口保留（纯函数）
  main.py        # FastAPI 路由 / 请求模型 / 错误位置与字段定位
tests/
  test_api.py        # /calculate、/units、/health 端到端测试
  test_resample.py   # /resample 端到端测试
Dockerfile
docker-compose.yml
requirements.txt
```
