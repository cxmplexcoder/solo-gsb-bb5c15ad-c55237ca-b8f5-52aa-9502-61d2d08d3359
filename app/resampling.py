"""测量序列重采样：量纲校验、单位换算与规则网格线性插值。

流程：

1. 校验所有样本单位均可识别、量纲一致，并与目标单位量纲一致；
2. 把全部样本读数换算到目标单位（温度等仿射单位同样成立，
   因为仿射变换保持线性插值）；
3. 按时间戳排序（重复时间戳拒绝）；
4. 以 ``t0 + k*step`` 生成规则网格：网格点落在样本上取原值，
   落在相邻样本之间且间隔不超过 ``max_gap`` 时线性插值；
   相邻样本间隔超过 ``max_gap`` 视为缺口，缺口内部网格点保留
   为 missing（``value=null``），**不跨缺口插值**。

时间戳、``step``、``max_gap`` 共用同一条数值时间轴（如秒），
服务端不解释其物理单位。时间与数值全程使用
:class:`fractions.Fraction`，避免网格端点漂移，仅在输出时转 float。
"""
from __future__ import annotations

import bisect
import math
from fractions import Fraction

from .units import UnitRegistry, dims_equal, format_dimension

# 网格点数量安全上限，防止 step 相对跨度过小导致资源耗尽
MAX_GRID_POINTS = 100_000


class ResampleError(Exception):
    """重采样请求错误，携带可定位到请求字段的 ``field``。"""

    def __init__(self, code: str, message: str, field: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def _fraction(value: float, field: str, what: str) -> Fraction:
    """把 float 入参转为精确 Fraction，拒绝 NaN/Inf。"""
    if not math.isfinite(value):
        raise ResampleError("INVALID_VALUE", f"{what}必须是有限数", field)
    return Fraction(str(value))


def prepare_samples(
    registry: UnitRegistry,
    samples: list,
    target_unit_name: str,
) -> tuple[str, list[tuple[Fraction, Fraction]]]:
    """校验单位/量纲/时间戳，返回 ``(目标单位名, [(时间戳, 目标单位数值)...])``。

    样本按原始顺序校验，错误 ``field`` 指向请求中的具体下标。
    """
    target = registry.get(target_unit_name)
    if target is None:
        raise ResampleError("UNKNOWN_UNIT", f"未知目标单位 {target_unit_name!r}", "unit")

    prepared: list[tuple[Fraction, Fraction, object]] = []
    reference_dim = None
    seen_ts: dict[Fraction, int] = {}

    for i, s in enumerate(samples):
        unit = registry.get(s.unit)
        if unit is None:
            raise ResampleError(
                "UNKNOWN_UNIT",
                f"未知单位 {s.unit!r}（samples[{i}]）",
                f"samples[{i}].unit",
            )
        ts = _fraction(s.timestamp, f"samples[{i}].timestamp", "时间戳")
        val = _fraction(s.value, f"samples[{i}].value", "数值")

        if reference_dim is None:
            reference_dim = unit.dimension
        elif not dims_equal(unit.dimension, reference_dim):
            raise ResampleError(
                "DIMENSION_MISMATCH",
                f"样本量纲不一致：samples[{i}] 的单位 {s.unit!r} 为 "
                f"{format_dimension(unit.dimension)}，而首个样本为 "
                f"{format_dimension(reference_dim)}",
                f"samples[{i}].unit",
            )

        if ts in seen_ts:
            raise ResampleError(
                "DUPLICATE_TIMESTAMP",
                f"时间戳重复：{float(ts)} 同时出现在 samples[{seen_ts[ts]}] "
                f"与 samples[{i}]",
                f"samples[{i}].timestamp",
            )
        seen_ts[ts] = i

        prepared.append((ts, val, unit))

    assert reference_dim is not None  # Pydantic 已保证 samples 非空

    if not dims_equal(target.dimension, reference_dim):
        raise ResampleError(
            "UNIT_DIM_MISMATCH",
            f"目标单位量纲不匹配：样本为 {format_dimension(reference_dim)}，"
            f"而 {target_unit_name!r} 为 {format_dimension(target.dimension)}",
            "unit",
        )

    converted = [
        (ts, target.from_si(unit.to_si(val))) for ts, val, unit in prepared
    ]
    converted.sort(key=lambda item: item[0])
    return target.name, converted


def build_grid(
    samples: list[tuple[Fraction, Fraction]],
    step: Fraction,
    max_gap: Fraction,
    max_points: int = MAX_GRID_POINTS,
) -> dict:
    """在已换算、已排序的样本上生成规则网格。

    缺口定义：相邻样本间隔严格大于 ``max_gap``；缺口端点上的网格点
    仍取真实样本值，只有严格落在缺口内部的点标记为 missing。
    """
    times = [ts for ts, _ in samples]
    values = dict(samples)
    t_start, t_end = times[0], times[-1]

    k_max = (t_end - t_start) // step  # 最后一个不超过 t_end 的网格点
    if k_max + 1 > max_points:
        raise ResampleError(
            "GRID_TOO_LARGE",
            f"网格点数 {int(k_max) + 1} 超过上限 {max_points}，"
            "请增大 step 或缩小时间范围",
            "step",
        )

    gap_indexes = {
        j for j in range(len(times) - 1)
        if times[j + 1] - times[j] > max_gap
    }
    gaps = [
        {"start": float(times[j]), "end": float(times[j + 1])}
        for j in sorted(gap_indexes)
    ]

    points: list[dict] = []
    interpolated_count = 0
    missing_count = 0
    for k in range(int(k_max) + 1):
        t = t_start + k * step
        if t in values:  # 网格点与样本重合（含首尾）
            points.append({
                "timestamp": float(t),
                "value": float(values[t]),
                "interpolated": False,
            })
            continue

        j = bisect.bisect_right(times, t) - 1
        if j in gap_indexes:  # 严格位于缺口内部：不插值
            points.append({"timestamp": float(t), "value": None,
                           "interpolated": False})
            missing_count += 1
            continue

        t_a, t_b = times[j], times[j + 1]
        ratio = (t - t_a) / (t_b - t_a)
        v = values[t_a] + ratio * (values[t_b] - values[t_a])
        points.append({
            "timestamp": float(t),
            "value": float(v),
            "interpolated": True,
        })
        interpolated_count += 1

    return {
        "time_range": {"start": float(t_start), "end": float(t_end)},
        "points": points,
        "gaps": gaps,
        "sample_count": len(samples),
        "grid_count": len(points),
        "interpolated_count": interpolated_count,
        "missing_count": missing_count,
    }
