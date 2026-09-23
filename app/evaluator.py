"""语义求值：在 AST 上传播量纲并执行精确有理运算。

规则（遵循题目约束）：

* ``+/-``：两侧量纲必须相同，否则报 ``DIMENSION_MISMATCH``；
  兼容单位先自动换算到 SI 再运算。温度仿射单位参与加减时按
  绝对温度（开尔文）处理。
* ``*``：量纲相加，数值相乘。
* ``/``：**除数不得依赖任何带单位的用户变量**，否则报
  ``DIVISION_WITH_UNIT``（工程安全约束：禁止测量变量之间相除，
  如 ``d/t``）。用单位常量构造复合单位（如 ``m/s``）或用量纲一致的
  无量纲系数除法则允许，量纲正常传播。
* 除以零：``DIVISION_BY_ZERO``。

数值全程使用 :class:`fractions.Fraction`，避免浮点量纲误差；
仅在 API 输出时转换为 float。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from .parser import (
    BinaryExpr,
    CalcError,
    NumberExpr,
    UnaryExpr,
    VarExpr,
)
from .units import (
    PREFERRED_LABELS,
    UnitDef,
    UnitRegistry,
    add_dims,
    dims_equal,
    format_dimension,
    format_generic_unit,
    DIMENSIONLESS,
)

@dataclass(frozen=True)
class Measurement:
    """计算结果：SI 相干单位下的数值 + 量纲。"""

    value: Fraction
    dimension: tuple[int, ...]

    def canonical_unit(self) -> str:
        preferred = PREFERRED_LABELS.get(self.dimension)
        if preferred is not None:
            return preferred
        return format_generic_unit(self.dimension)


def _suggest(name: str, candidates: list[str], limit: int = 3) -> list[str]:
    """简单的拼写建议：公共前缀 + 子串包含。"""
    name_l = name.lower()
    starts = [c for c in candidates if c.lower().startswith(name_l[:2])]
    contains = [c for c in candidates if name_l and name_l in c.lower()]
    seen: set[str] = set()
    out: list[str] = []
    for c in starts + contains:
        if c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _finite(value: Fraction) -> bool:
    """判断 Fraction 是否可表示为有限 float。"""
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _unit_measurement(unit: UnitDef) -> Measurement:
    """单位名作为常量：1 个该单位（如表达式中出现 ``m``）。"""
    return Measurement(unit.to_si(Fraction(1)), unit.dimension)


class Evaluator:
    def __init__(self, registry: UnitRegistry, variables: dict[str, Measurement]):
        self.registry = registry
        self.variables = variables

    def eval(self, node) -> Measurement:
        if isinstance(node, NumberExpr):
            return Measurement(node.value, DIMENSIONLESS)
        if isinstance(node, VarExpr):
            return self._eval_var(node)
        if isinstance(node, UnaryExpr):
            operand = self.eval(node.operand)
            if node.op == "-":
                return Measurement(-operand.value, operand.dimension)
            return operand
        if isinstance(node, BinaryExpr):
            return self._eval_binary(node)
        raise TypeError(f"未知 AST 节点: {type(node)!r}")

    def _eval_var(self, node: VarExpr) -> Measurement:
        if node.name in self.variables:
            return self.variables[node.name]
        unit = self.registry.get(node.name)
        if unit is not None:
            return _unit_measurement(unit)
        candidates = [*self.variables.keys()]
        candidates.extend(u.name for u in self.registry.canonical_units())
        suggestions = _suggest(node.name, candidates)
        hint = f"，是否想写 {'、'.join(suggestions)}？" if suggestions else ""
        raise CalcError(
            "UNDEFINED_VARIABLE",
            f"未定义的变量或单位 {node.name!r}{hint}",
            node.pos,
            node.end - node.pos,
        )

    def _eval_binary(self, node: BinaryExpr) -> Measurement:
        left = self.eval(node.left)
        right = self.eval(node.right)
        op_len = 1
        if node.op == "+":
            return self._add_sub(node, left, right, sign=Fraction(1), op_len=op_len)
        if node.op == "-":
            return self._add_sub(node, left, right, sign=Fraction(-1), op_len=op_len)
        if node.op == "*":
            return self._multiply(node, left, right)
        if node.op == "/":
            return self._divide(node, left, right, op_len)
        raise TypeError(f"未知运算符: {node.op!r}")

    def _add_sub(self, node: BinaryExpr, left: Measurement, right: Measurement,
                 sign: Fraction, op_len: int) -> Measurement:
        if not dims_equal(left.dimension, right.dimension):
            raise CalcError(
                "DIMENSION_MISMATCH",
                f"量纲不一致，不能相{'加' if sign == 1 else '减'}："
                f"{format_dimension(left.dimension)} 与 "
                f"{format_dimension(right.dimension)}",
                node.pos,
                op_len,
            )
        value = left.value + sign * right.value
        if not _finite(value):
            raise CalcError("NUMERIC_OVERFLOW", "计算结果超出数值范围", node.pos, op_len)
        return Measurement(value, left.dimension)

    def _multiply(self, node: BinaryExpr, left: Measurement, right: Measurement) -> Measurement:
        # 数值 × 仿射单位常量 = “读数”（如 100 °C -> 373.15 K），
        # 而非 100 × 1°C 的乘法；用于温度等带偏移单位。
        affine = self._affine_reading(node, left, right)
        if affine is not None:
            return affine
        value = left.value * right.value
        if not _finite(value):
            raise CalcError("NUMERIC_OVERFLOW", "计算结果超出数值范围", node.pos, 1)
        return Measurement(value, add_dims(left.dimension, right.dimension))

    def _affine_reading(self, node: BinaryExpr, left: Measurement,
                        right: Measurement) -> Measurement | None:
        """处理 ``number * 仿射单位`` 形式的温度读数，返回绝对 SI 值。"""
        for scalar_side, unit_side in ((left, right), (right, left)):
            unit_node = node.right if unit_side is right else node.left
            if scalar_side.dimension != DIMENSIONLESS:
                continue
            if not isinstance(unit_node, VarExpr):
                continue
            unit = self.registry.get(unit_node.name)
            if unit is None or unit.offset == 0:
                continue
            # 只有引用的是“单位常量本身”（即 1 单位）才视为读数
            if unit_side.value != unit.to_si(Fraction(1)):
                continue
            return Measurement(unit.to_si(scalar_side.value), unit.dimension)
        return None

    def _divide(self, node: BinaryExpr, left: Measurement, right: Measurement,
                op_len: int) -> Measurement:
        offending = self._dimensional_variable(node.right)
        if offending is not None:
            raise CalcError(
                "DIVISION_WITH_UNIT",
                f"除数不能依赖带单位的变量 {offending.name!r}"
                f"（量纲 {format_dimension(right.dimension)}）；"
                "本服务禁止测量变量之间相除。若需构造复合单位请使用单位常量（如 m/s），"
                "若需无量纲比率请先换算为同一单位后改用减法核对",
                offending.pos,
                offending.end - offending.pos,
            )
        if right.value == 0:
            raise CalcError("DIVISION_BY_ZERO", "不能除以零", node.pos, op_len)
        value = left.value / right.value
        if not _finite(value):
            raise CalcError("NUMERIC_OVERFLOW", "计算结果超出数值范围", node.pos, op_len)
        return Measurement(value, tuple(a - b for a, b in zip(left.dimension,
                                                              right.dimension)))

    def _dimensional_variable(self, node) -> VarExpr | None:
        """查找表达式中第一个“带单位的用户变量”（用于除数校验）。"""
        if isinstance(node, VarExpr):
            var = self.variables.get(node.name)
            if var is not None and var.dimension != DIMENSIONLESS:
                return node
            return None
        if isinstance(node, BinaryExpr):
            return self._dimensional_variable(node.left) or self._dimensional_variable(node.right)
        if isinstance(node, UnaryExpr):
            return self._dimensional_variable(node.operand)
        return None
