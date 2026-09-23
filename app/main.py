"""FastAPI 入口：设备工程测量计算 API。

启动时（lifespan）初始化一次内置单位表；默认监听 8000 端口。
"""
from __future__ import annotations

import logging
from fractions import Fraction
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .evaluator import Evaluator, Measurement
from .parser import CalcError, parse_expression
from .resampling import (
    MAX_GRID_POINTS,
    ResampleError,
    build_grid,
    prepare_samples,
)
from .units import (
    UnitRegistry,
    build_registry,
    dims_equal,
    format_dimension,
    DIMENSIONLESS,
)

logger = logging.getLogger("engineering_calc")

MAX_EXPRESSION_LEN = 2000


def _dim_text(dim) -> str:
    text = format_dimension(dim)
    return f"{text}（无量纲）" if dim == DIMENSIONLESS else text


class APIError(Exception):
    """非表达式级别的请求错误（无源码位置）。"""

    def __init__(self, code: str, message: str, status_code: int = 400,
                 field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------

class VariableIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="变量名")
    value: float = Field(..., description="数值")
    unit: str = Field("1", min_length=1, max_length=32, description="单位名称，缺省为无量纲")


class CalcRequest(BaseModel):
    expression: str = Field(..., min_length=1, max_length=MAX_EXPRESSION_LEN,
                            description="算式，支持 + - * / 与括号，可直接使用单位名")
    variables: list[VariableIn] = Field(default_factory=list, description="变量定义")
    result_unit: str | None = Field(None, alias="unit",
                                    description="期望的结果单位（须与结果量纲一致），缺省返回规范化单位")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# /resample 请求模型
# ---------------------------------------------------------------------------

class ResampleSampleIn(BaseModel):
    timestamp: float = Field(..., description="采样时间戳（数值时间轴，如 Unix 秒）")
    value: float = Field(..., description="测量读数")
    unit: str = Field("1", min_length=1, max_length=32,
                      description="测量单位，缺省为无量纲；全部样本须量纲一致")


class ResampleRequest(BaseModel):
    samples: list[ResampleSampleIn] = Field(..., min_length=1,
                                            description="带时间戳、数值和单位的测量序列")
    unit: str = Field(..., min_length=1, max_length=32, description="目标单位")
    step: float = Field(..., description="规则网格固定步长（与时间戳同时间轴），必须为正数")
    max_gap: float = Field(..., alias="maxGap",
                           description="允许线性插值的最大相邻样本间隔；超过则保留缺口",
                           )

    model_config = {"populate_by_name": True}

    @field_validator("step")
    @classmethod
    def _check_step(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("step 必须是有限数")
        if v <= 0:
            raise ValueError("step 必须大于 0")
        return v

    @field_validator("max_gap")
    @classmethod
    def _check_max_gap(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("maxGap 必须是有限数")
        if v < 0:
            raise ValueError("maxGap 不能为负数")
        return v


def _fraction_from_number(value: float) -> Fraction:
    if isinstance(value, bool):
        raise APIError("INVALID_VALUE", "变量数值不能是布尔值")
    if value != value or value in (float("inf"), float("-inf")):
        raise APIError("INVALID_VALUE", "变量数值必须是有限数")
    return Fraction(str(value))


def _position_dict(expression: str, pos: int, length: int) -> dict[str, int]:
    line = expression.count("\n", 0, pos) + 1
    last_nl = expression.rfind("\n", 0, pos)
    column = pos - last_nl  # 1 基列号（last_nl=-1 时即 pos+1）
    return {"offset": pos, "length": length, "line": line, "column": column}


# 语义类错误 -> HTTP 422，其余 -> 400
_SEMANTIC_CODES = frozenset({
    "DIMENSION_MISMATCH",
    "DIVISION_WITH_UNIT",
    "DIVISION_BY_ZERO",
    "UNDEFINED_VARIABLE",
    "NUMERIC_OVERFLOW",
    "UNIT_DIM_MISMATCH",
})


def _error_body(code: str, message: str,
                position: dict[str, int] | None = None,
                field: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        body["field"] = field
    body["position"] = position
    return {"error": body}


def _loc_text(loc: tuple) -> str:
    """Pydantic 错误定位 -> 请求字段路径，如 ``samples[0].step``。"""
    parts = [p for p in loc if p not in ("body", "")]
    out = ""
    for p in parts:
        if isinstance(p, int):
            out = f"{out}[{p}]"
        else:
            out = f"{out}.{p}" if out else str(p)
    return out


def _build_variables(registry: UnitRegistry, variables: list[VariableIn]
                     ) -> dict[str, Measurement]:
    result: dict[str, Measurement] = {}
    for var in variables:
        if var.name in result:
            raise APIError("DUPLICATE_VARIABLE", f"变量 {var.name!r} 重复定义")
        unit = registry.get(var.unit)
        if unit is None:
            raise APIError("UNKNOWN_UNIT", f"未知单位 {var.unit!r}（变量 {var.name!r}）")
        value = _fraction_from_number(var.value)
        result[var.name] = Measurement(unit.to_si(value), unit.dimension)
    return result


def _fraction_to_float(value: Fraction) -> float:
    return float(value)


def create_app() -> FastAPI:
    # 服务就绪前初始化一次内置单位表（lifespan 中仅记录日志）
    registry = build_registry()

    async def lifespan(app: FastAPI):
        logger.info("内置单位表就绪：%d 个名称", len(registry.units))
        yield

    app = FastAPI(
        title="设备工程测量计算 API",
        description="解析带数值与单位的工程算式，自动传播量纲、换算兼容单位，并校验非法运算。",
        version="1.0.0",
        lifespan=lifespan,
    )

    def get_registry() -> UnitRegistry:
        return registry

    # ---- 错误处理 ----
    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        return JSONResponse(status_code=exc.status_code,
                            content=_error_body(exc.code, exc.message,
                                                field=exc.field))

    @app.exception_handler(ResampleError)
    async def resample_error_handler(request: Request, exc: ResampleError):
        # /resample 的全部语义/输入错误均为 422，并定位到具体请求字段
        return JSONResponse(status_code=422,
                            content=_error_body(exc.code, exc.message,
                                                field=exc.field))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        field: str | None = None
        parts: list[str] = []
        for err in exc.errors():
            loc = _loc_text(err["loc"])
            if field is None and loc:
                field = loc
            parts.append(f"{loc or 'request'}: {err['msg']}")
        return JSONResponse(status_code=422,
                            content=_error_body("INVALID_REQUEST",
                                                "; ".join(parts), field=field))

    # ---- 业务路由 ----
    @app.post("/calculate", tags=["calculate"],
              summary="计算带单位的工程算式", response_model=None)
    async def calculate(req: CalcRequest) -> dict[str, Any] | JSONResponse:
        registry = get_registry()
        variables = _build_variables(registry, req.variables)
        try:
            ast = parse_expression(req.expression)
            result = Evaluator(registry, variables).eval(ast)

            unit_name = result.canonical_unit()
            output_value = _fraction_to_float(result.value)

            if req.result_unit is not None:
                target = registry.get(req.result_unit)
                if target is None:
                    raise APIError("UNKNOWN_UNIT", f"未知结果单位 {req.result_unit!r}")
                if not dims_equal(target.dimension, result.dimension):
                    raise APIError(
                        "UNIT_DIM_MISMATCH",
                        f"结果单位量纲不匹配：期望 {format_dimension(result.dimension)}，"
                        f"而 {req.result_unit!r} 为 {format_dimension(target.dimension)}",
                        status_code=422,
                    )
                output_value = _fraction_to_float(target.from_si(result.value))
                unit_name = target.name
        except CalcError as exc:
            status = 422 if exc.code in _SEMANTIC_CODES else 400
            position = _position_dict(req.expression, exc.pos, exc.length)
            return JSONResponse(status_code=status,
                                content=_error_body(exc.code, exc.message, position))

        return {
            "expression": req.expression,
            "value": output_value,
            "unit": unit_name,
            "dimension": list(result.dimension),
            "dimension_text": _dim_text(result.dimension),
        }

    @app.post("/resample", tags=["resample"],
              summary="测量序列重采样到规则网格（线性插值，缺口保留 missing）",
              response_model=None)
    async def resample(req: ResampleRequest) -> dict[str, Any]:
        registry = get_registry()
        unit_name, prepared = prepare_samples(registry, req.samples, req.unit)
        step = _fraction_from_number(req.step)
        max_gap = _fraction_from_number(req.max_gap)
        if step <= 0:  # Pydantic 已拦截，这里做最终防线
            raise ResampleError("INVALID_STEP", "step 必须大于 0", "step")
        grid = build_grid(prepared, step, max_gap)
        return {
            "unit": unit_name,
            "step": req.step,
            "max_gap": req.max_gap,
            **grid,
        }

    @app.get("/units", tags=["meta"], summary="列出内置单位")
    async def list_units() -> dict[str, Any]:
        registry = get_registry()
        units = []
        for u in registry.canonical_units():
            units.append({
                "name": u.name,
                "dimension": list(u.dimension),
                "dimension_text": _dim_text(u.dimension),
                "aliases": sorted(u.aliases),
                "factor": float(u.factor),
                "offset": float(u.offset),
            })
        return {"count": len(units), "units": units}

    @app.get("/health", tags=["meta"], summary="健康检查")
    async def health() -> dict[str, str]:
        return {"status": "ok", "units_loaded": str(len(get_registry().units))}

    @app.get("/", tags=["meta"], summary="服务信息")
    async def root() -> dict[str, Any]:
        return {
            "service": "engineering-measurement-calc",
            "version": app.version,
            "docs": "/docs",
            "endpoints": ["/calculate", "/resample", "/units", "/health"],
        }

    return app


app = create_app()
