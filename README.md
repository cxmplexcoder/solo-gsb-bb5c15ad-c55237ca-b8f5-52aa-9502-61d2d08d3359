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
docker compose up --build
```

服务启动后：

- API 文档（Swagger）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 单位表：<http://localhost:8000/units>

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
  main.py        # FastAPI 路由 / 请求模型 / 错误位置换算
tests/test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```
