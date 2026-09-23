# 设备工程测量计算 API（FastAPI）

客户端提交包含**数值与单位变量**的工程算式，服务端完成表达式解析、
**量纲传播**、兼容单位自动换算，并拒绝非法运算（不同量纲相加、
除以带单位的测量变量等），返回规范化数值与单位，错误时返回**精确出错位置**。

- Web 框架：FastAPI + Uvicorn
- 数值计算：全程 `fractions.Fraction` 精确有理运算，输出时转 `float`
- 量纲：7 个 SI 基本量纲整数指数向量 `(L, M, T, I, Θ, N, J)`
- 默认端口：**8000**
- 启动时初始化内置单位表（1700+ 名称，含 SI 前缀自动派生）

## 快速开始

### Docker Compose（推荐）

```bash
# 构建镜像并前台启动（Ctrl-C 停止）
docker compose up --build

# 或后台运行
docker compose up --build -d

# 查看状态与健康检查
docker compose ps

# 跟踪日志
docker compose logs -f

# 停止并移除容器（数据仅在内存中，无需保留卷）
docker compose down
```

Compose 文件会把容器 **8000** 端口映射到宿主机 **8000**，
并配置了基于 `/health` 的 healthcheck；容器内由 Uvicorn 直接提供服务，
无需额外反向代理。端口被占用时可在 `docker-compose.yml` 中改成
`"8001:8000"` 等映射。

服务启动后：

- API 文档（Swagger）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 单位表：<http://localhost:8000/units>

无 Docker 环境时也可直接用 Dockerfile 构建运行：

```bash
docker build -t engineering-calc-api .
docker run --rm -p 8000:8000 engineering-calc-api
```

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

把带时间戳、数值和单位的测量序列重采样到**固定步长的规则网格**：
先确认全部样本量纲一致并换算到目标单位、按时间排序；相邻样本间隔不超过
`maxGap` 时在网格点上线性插值，跨越更大缺口时缺口内部保留 missing
（`value: null`），**绝不跨缺口插值**。

时间计算全程使用 `fractions.Fraction` 精确有理运算（步长 `0.1` 不会浮点漂移），
输出时转 `float`。`timestamp`、`step`、`maxGap` 共用同一条数值时间轴
（如 Unix 秒），服务端不解释其物理单位。

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `samples` | array | 至少 1 个样本：`{timestamp, value, unit?}`，`unit` 缺省为无量纲 |
| `unit` | string | 目标单位，须与样本量纲一致 |
| `step` | number | 网格固定步长，**必须为正有限数** |
| `maxGap` | number | 允许插值的最大相邻样本间隔（非负有限数；也接受 `max_gap`） |

响应字段：

| 字段 | 说明 |
|---|---|
| `points[]` | `{timestamp, value, interpolated}`；缺口中的点 `value=null, interpolated=false` |
| `gaps[]` | 缺口区间 `{start, end}`（间隔严格大于 `maxGap` 的相邻样本对） |
| `time_range` | 首尾样本范围 `{start, end}` |
| `sample_count` / `grid_count` | 样本数 / 网格点数 |
| `interpolated_count` / `missing_count` | 插值点数 / 缺口内 missing 点数 |

#### 示例 1：单位换算 + 线性插值

```bash
curl -s -X POST http://localhost:8000/resample \
  -H 'Content-Type: application/json' \
  -d '{"samples":[{"timestamp":0,"value":0,"unit":"m"},
                  {"timestamp":2,"value":1000,"unit":"km"}],
       "unit":"km","step":1,"maxGap":2}'
```

```json
{"unit":"km","step":1.0,"max_gap":2.0,
 "time_range":{"start":0.0,"end":2.0},
 "points":[{"timestamp":0.0,"value":0.0,"interpolated":false},
           {"timestamp":1.0,"value":500.0,"interpolated":true},
           {"timestamp":2.0,"value":1000.0,"interpolated":false}],
 "gaps":[],"sample_count":2,"grid_count":3,
 "interpolated_count":1,"missing_count":0}
```

#### 示例 2：大缺口保留 missing，不跨缺口插值

样本在 `t=2` 与 `t=6` 之间间隔 4（> `maxGap:2`），网格点 3/4/5 保留 missing，
缺口端点仍为真实样本：

```bash
curl -s -X POST http://localhost:8000/resample \
  -H 'Content-Type: application/json' \
  -d '{"samples":[{"timestamp":0,"value":0,"unit":"m"},
                  {"timestamp":2,"value":20,"unit":"m"},
                  {"timestamp":6,"value":60,"unit":"m"}],
       "unit":"m","step":1,"maxGap":2}'
```

```json
{"points":[{"timestamp":0.0,"value":0.0,"interpolated":false},
           {"timestamp":1.0,"value":10.0,"interpolated":true},
           {"timestamp":2.0,"value":20.0,"interpolated":false},
           {"timestamp":3.0,"value":null,"interpolated":false},
           {"timestamp":4.0,"value":null,"interpolated":false},
           {"timestamp":5.0,"value":null,"interpolated":false},
           {"timestamp":6.0,"value":60.0,"interpolated":false}],
 "gaps":[{"start":2.0,"end":6.0}],
 "time_range":{"start":0.0,"end":6.0}, "...":"..."}
```

规则细节：

- 网格为 `start + k·step`，只生成不超过 `end` 的点（步长不整除跨度时
  最后一个网格点可能早于 `end`）；网格点恰好落在样本时间戳上时取真实值，
  不标记插值。
- 缺口判定为**严格大于** `maxGap`：间隔恰好等于上限时允许插值。
- 温度等仿射单位（°C/°F）同样支持，先换算到目标温标再插值
  （仿射变换保持线性）。
- 网格点数上限 100000，超出返回 `GRID_TOO_LARGE`（定位 `step`）。

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
| `UNIT_DIM_MISMATCH` | 422 | `/calculate` 的 `unit` 参数与结果量纲不一致；或 `/resample` 目标单位与样本量纲不一致（附 `field`） |
| `NUMERIC_OVERFLOW` | 422 | 结果超出有限数范围 |
| `DUPLICATE_TIMESTAMP` | 422 | `/resample` 样本时间戳重复（`field` 指向第二个重复样本） |
| `GRID_TOO_LARGE` | 422 | `/resample` 网格点数超过 100000（`field: step`） |

所有表达式错误都返回 `error.position`：`offset`（0 基字符偏移）、
`length`、`line`、`column`（1 基行列号）。

`/resample` 与请求体校验错误不使用 `position`（值为 `null`），
改为返回 `error.field` 精确定位请求字段，如 `samples[1].unit`、
`samples[0].timestamp`、`step`、`maxGap`、`unit`。

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
  resampling.py  # /resample：量纲一致性校验、单位换算、规则网格插值与缺口保留
  main.py        # FastAPI 路由 / 请求模型 / 错误位置换算
tests/test_api.py
tests/test_resample.py
Dockerfile
docker-compose.yml
requirements.txt
```
