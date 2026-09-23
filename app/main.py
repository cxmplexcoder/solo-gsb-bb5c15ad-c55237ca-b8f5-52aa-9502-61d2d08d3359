"""FastAPI 入口：设备工程测量计算 API。

启动时（lifespan）初始化一次内置单位表；默认监听 8000 端口。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .evaluator import Evaluator, Measurement
from .parser import CalcError, parse_expression
from .resample import Sample, resample
from .units import (
    UnitRegistry,
    build_registry,
    dims_equal,
    format_dimension,
    DIMENSIONLESS,
    DIM_T,
)

logger = logging.getLogger("engineering_calc")

MAX_EXPRESSION_LEN = 2000


def _dim_text(dim) -> str:
    text = format_dimension(dim)
    return f"{text}（无量纲）" if dim == DIMENSIONLESS else text


class APIError(Exception):
    """非表达式级别的请求错误（无源码位置）。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


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
                position: dict[str, int] | None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "position": position}}


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


# ---------------------------------------------------------------------------
# /resample：测量序列重采样
# ---------------------------------------------------------------------------

MAX_GRID_POINTS = 100_000
MAX_SAMPLES = 100_000

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ResampleError(Exception):
    """重采样请求的语义错误：携带出错字段定位，统一返回 422。"""

    def __init__(self, code: str, message: str, field: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class DurationIn(BaseModel):
    value: float = Field(..., description="时长数值，必须为正数")
    unit: str = Field("s", min_length=1, max_length=32, description="时间单位，缺省为秒 s")


class SampleIn(BaseModel):
    timestamp: str | float = Field(
        ..., description="ISO 8601 时间戳（无时区按 UTC 处理）或 Unix 秒（数字）")
    value: float = Field(..., description="测量值")
    unit: str = Field(..., min_length=1, max_length=32, description="测量单位")


class ResampleRequest(BaseModel):
    samples: list[SampleIn] = Field(..., min_length=1, max_length=MAX_SAMPLES,
                                    description="测量样本序列（无需按时间排序）")
    unit: str = Field(..., min_length=1, max_length=32,
                      description="目标单位：全部样本换算到该单位后输出")
    step: DurationIn = Field(..., description="固定步长（时间单位）")
    max_gap: DurationIn = Field(...,
                                description="最大可插值间隔：相邻样本间隔不超过它才做线性插值")


def _field_error_body(code: str, message: str, field: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "field": field}}


def _finite_fraction(value: float, field: str) -> Fraction:
    if isinstance(value, bool) or not math.isfinite(value):
        raise ResampleError("INVALID_VALUE", "数值必须是有限数", field)
    return Fraction(str(value))


def _datetime_to_epoch(dt: datetime) -> Fraction:
    """datetime -> 自 Unix 纪元起的秒（精确到微秒）；无时区信息按 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - _EPOCH
    return Fraction(delta.days * 86400 + delta.seconds) + Fraction(delta.microseconds, 10**6)


def _epoch_to_iso(seconds: Fraction) -> str:
    """自 Unix 纪元起的秒 -> ISO 8601（UTC，Z 后缀）。

    网格时刻的分母恒为 2^a*5^b（输入精度与时间单位因子均为十进制），
    因此小数部分必为有限小数，逐位展开即可精确输出。
    """
    whole = seconds.numerator // seconds.denominator
    frac = seconds - whole
    base = (_EPOCH + timedelta(seconds=whole)).strftime("%Y-%m-%dT%H:%M:%S")
    if not frac:
        return f"{base}Z"
    digits: list[str] = []
    while frac and len(digits) < 30:  # 30 位足够覆盖任何十进制时间精度
        frac *= 10
        digit = frac.numerator // frac.denominator
        digits.append(str(digit))
        frac -= digit
    return f"{base}.{''.join(digits)}Z"


def _parse_timestamp(raw: str | float, field: str) -> Fraction:
    if isinstance(raw, bool):
        raise ResampleError("INVALID_TIMESTAMP", "时间戳不能是布尔值", field)
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw):
            raise ResampleError("INVALID_TIMESTAMP", "时间戳必须是有限数", field)
        return Fraction(str(raw))
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ResampleError(
            "INVALID_TIMESTAMP",
            f"无法解析的时间戳 {raw!r}：应为 ISO 8601（如 2026-01-01T00:00:00Z）或 Unix 秒",
            field,
        ) from None
    return _datetime_to_epoch(dt)


def _parse_duration(registry: UnitRegistry, duration: DurationIn, field: str,
                    code: str) -> Fraction:
    """把 ``{value, unit}`` 时长换算为秒；单位必须是时间量纲且数值为正。"""
    value = _finite_fraction(duration.value, f"{field}.value")
    if value <= 0:
        raise ResampleError(code, f"{field} 必须为正数", f"{field}.value")
    unit = registry.get(duration.unit)
    if unit is None:
        raise ResampleError("UNKNOWN_UNIT", f"未知时间单位 {duration.unit!r}", f"{field}.unit")
    if unit.dimension != DIM_T:
        raise ResampleError(
            "UNIT_DIM_MISMATCH",
            f"{field} 的单位必须是时间单位，而 {duration.unit!r} 的量纲为 "
            f"{format_dimension(unit.dimension)}",
            f"{field}.unit",
        )
    return unit.to_si(value)


def _build_samples(registry: UnitRegistry, req: ResampleRequest):
    """校验并归一化样本：量纲一致、时间戳互异、换算到目标单位。"""
    target = registry.get(req.unit)
    if target is None:
        raise ResampleError("UNKNOWN_UNIT", f"未知目标单位 {req.unit!r}", "unit")

    step = _parse_duration(registry, req.step, "step", "INVALID_STEP")
    max_gap = _parse_duration(registry, req.max_gap, "max_gap", "INVALID_MAX_GAP")

    samples: list[Sample] = []
    seen: dict[Fraction, int] = {}
    series_dim: tuple[int, ...] | None = None
    for i, sample_in in enumerate(req.samples):
        time = _parse_timestamp(sample_in.timestamp, f"samples[{i}].timestamp")
        if time in seen:
            raise ResampleError(
                "DUPLICATE_TIMESTAMP",
                f"时间戳与 samples[{seen[time]}] 重复",
                f"samples[{i}].timestamp",
            )
        seen[time] = i
        unit = registry.get(sample_in.unit)
        if unit is None:
            raise ResampleError("UNKNOWN_UNIT", f"未知单位 {sample_in.unit!r}",
                                f"samples[{i}].unit")
        if series_dim is None:
            series_dim = unit.dimension
        elif not dims_equal(unit.dimension, series_dim):
            raise ResampleError(
                "DIMENSION_MISMATCH",
                f"样本量纲不一致：samples[0] 为 {format_dimension(series_dim)}，"
                f"而 {sample_in.unit!r} 为 {format_dimension(unit.dimension)}",
                f"samples[{i}].unit",
            )
        value = _finite_fraction(sample_in.value, f"samples[{i}].value")
        samples.append(Sample(time, unit.to_si(value)))

    assert series_dim is not None  # samples 已约束 min_length=1
    if not dims_equal(target.dimension, series_dim):
        raise ResampleError(
            "UNIT_DIM_MISMATCH",
            f"目标单位量纲不匹配：样本为 {format_dimension(series_dim)}，"
            f"而 {req.unit!r} 为 {format_dimension(target.dimension)}",
            "unit",
        )
    converted = [Sample(s.time, target.from_si(s.value)) for s in samples]
    return converted, target, step, max_gap


def create_app() -> FastAPI:
    # 服务就绪前初始化一次内置单位表（lifespan 中仅记录日志）
    registry = build_registry()

    async def lifespan(app: FastAPI):
        logger.info("内置单位表就绪：%d 个名称", len(registry.units))
        yield

    app = FastAPI(
        title="设备工程测量计算 API",
        description="解析带数值与单位的工程算式，自动传播量纲、换算兼容单位，并校验非法运算；"
                    "另提供测量序列重采样（规则网格线性插值 + 缺口保留）。",
        version="1.1.0",
        lifespan=lifespan,
    )

    def get_registry() -> UnitRegistry:
        return registry

    # ---- 错误处理 ----
    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        return JSONResponse(status_code=exc.status_code,
                            content=_error_body(exc.code, exc.message, None))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return JSONResponse(status_code=422,
                            content=_error_body("INVALID_REQUEST", details, None))

    @app.exception_handler(ResampleError)
    async def resample_error_handler(request: Request, exc: ResampleError):
        return JSONResponse(status_code=422,
                            content=_field_error_body(exc.code, exc.message, exc.field))

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
              summary="测量序列重采样到规则网格", response_model=None)
    async def resample_measurements(req: ResampleRequest) -> dict[str, Any]:
        registry = get_registry()
        samples, target, step, max_gap = _build_samples(registry, req)

        start = min(s.time for s in samples)
        end = max(s.time for s in samples)
        grid_count = (end - start) // step + 1
        if grid_count > MAX_GRID_POINTS:
            raise ResampleError(
                "INVALID_STEP",
                f"步长过小：将生成 {grid_count} 个网格点，超过上限 {MAX_GRID_POINTS}",
                "step.value",
            )

        result = resample(samples, step, max_gap)
        points = [
            {
                "timestamp": _epoch_to_iso(p.time),
                "value": None if p.value is None else _fraction_to_float(p.value),
                "interpolated": p.interpolated,
                "missing": p.value is None,
            }
            for p in result.points
        ]
        gaps = [
            {
                "start": _epoch_to_iso(g.start),
                "end": _epoch_to_iso(g.end),
                "duration_seconds": _fraction_to_float(g.end - g.start),
            }
            for g in result.gaps
        ]
        return {
            "unit": target.name,
            "dimension": list(target.dimension),
            "dimension_text": _dim_text(target.dimension),
            "step_seconds": _fraction_to_float(step),
            "max_gap_seconds": _fraction_to_float(max_gap),
            "sample_count": len(samples),
            "range": {"start": _epoch_to_iso(result.start),
                      "end": _epoch_to_iso(result.end)},
            "points": points,
            "point_count": len(points),
            "missing_count": sum(1 for p in result.points if p.value is None),
            "gaps": gaps,
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
