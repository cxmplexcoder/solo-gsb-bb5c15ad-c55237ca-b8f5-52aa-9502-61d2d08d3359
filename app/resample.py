"""测量序列重采样：排序、线性插值与缺口保留。

纯函数实现，不依赖 FastAPI；HTTP 层（``main.py``）负责请求解析、
单位换算与字段级错误定位。约定：

* 时间统一表示为“自 Unix 纪元起的秒”（:class:`fractions.Fraction`，精确）；
* 网格从首个样本时刻开始，以固定步长推进，直到不超过末样本时刻的最后一点；
* 相邻样本间隔 ``<= max_gap`` 时，落在其间的网格点取线性插值；
* 间隔 ``> max_gap`` 的区间 **不跨缺口插值**，落在其中的网格点标记为 missing；
* 恰好落在样本时刻的网格点直接取样本值（不算插值）。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Sample:
    """归一化后的样本：时间（秒）+ 目标单位下的数值。"""

    time: Fraction
    value: Fraction


@dataclass(frozen=True)
class GridPoint:
    """规则网格点；``value`` 为 ``None`` 表示 missing（位于缺口内）。"""

    time: Fraction
    value: Fraction | None
    interpolated: bool


@dataclass(frozen=True)
class Gap:
    """相邻样本间隔超过 ``max_gap`` 的缺口区间。"""

    start: Fraction
    end: Fraction


@dataclass(frozen=True)
class ResampleResult:
    """重采样结果：首尾范围、网格点与缺口列表。"""

    start: Fraction
    end: Fraction
    points: list[GridPoint]
    gaps: list[Gap]


def resample(samples: list[Sample], step: Fraction, max_gap: Fraction) -> ResampleResult:
    """把样本序列重采样到规则网格。

    调用方保证 ``samples`` 非空、``step``/``max_gap`` 为正；
    时间戳重复属于请求错误，此处防御性拒绝。
    """
    if not samples:
        raise ValueError("samples 不能为空")
    if step <= 0 or max_gap <= 0:
        raise ValueError("step 与 max_gap 必须为正数")

    ordered = sorted(samples, key=lambda s: s.time)
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.time == nxt.time:
            raise ValueError(f"样本时间戳重复: {prev.time}")

    start, end = ordered[0].time, ordered[-1].time
    gaps = [Gap(prev.time, nxt.time)
            for prev, nxt in zip(ordered, ordered[1:])
            if nxt.time - prev.time > max_gap]

    points: list[GridPoint] = []
    i = 0  # ordered[i]：当前网格点左侧（或重合）的样本
    t = start
    while t <= end:
        while i + 1 < len(ordered) and ordered[i + 1].time <= t:
            i += 1
        left = ordered[i]
        if t == left.time:
            points.append(GridPoint(t, left.value, interpolated=False))
        else:
            right = ordered[i + 1]
            if right.time - left.time <= max_gap:
                ratio = (t - left.time) / (right.time - left.time)
                value = left.value + (right.value - left.value) * ratio
                points.append(GridPoint(t, value, interpolated=True))
            else:
                points.append(GridPoint(t, None, interpolated=False))
        t += step
    return ResampleResult(start=start, end=end, points=points, gaps=gaps)
