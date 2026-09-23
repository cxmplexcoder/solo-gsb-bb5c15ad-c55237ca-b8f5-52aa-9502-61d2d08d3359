"""单位表与量纲系统。

量纲采用 7 个 SI 基本量纲的整数指数向量::

    (长度 L, 质量 M, 时间 T, 电流 I, 热力学温度 Θ, 物质的量 N, 发光强度 J)

每个单位通过线性（或仿射，用于温标）关系换算到 SI：

    value_si = factor * value_unit + offset
    value_unit = (value_si - offset) / factor

应用启动时由 :func:`build_registry` 初始化全部内置单位，
包括 SI 前缀（k、m、μ……）自动派生的倍数/分数单位。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

# ---------------------------------------------------------------------------
# 量纲向量常量，顺序：L, M, T, I, Θ, N, J
# ---------------------------------------------------------------------------

_DIM_ZERO = (0, 0, 0, 0, 0, 0, 0)
DIMENSIONLESS = _DIM_ZERO
DIM_L = (1, 0, 0, 0, 0, 0, 0)
DIM_M = (0, 1, 0, 0, 0, 0, 0)
DIM_T = (0, 0, 1, 0, 0, 0, 0)
DIM_I = (0, 0, 0, 1, 0, 0, 0)
DIM_THETA = (0, 0, 0, 0, 1, 0, 0)
DIM_N = (0, 0, 0, 0, 0, 1, 0)
DIM_J = (0, 0, 0, 0, 0, 0, 1)

BASE_SYMBOLS = ("m", "kg", "s", "A", "K", "mol", "cd")

# 基本单位量纲 -> 优先显示的“相干导出单位”（factor=1, offset=0）
PREFERRED_LABELS: dict[tuple[int, ...], str] = {
    _DIM_ZERO: "1",
    DIM_L: "m",
    DIM_M: "kg",
    DIM_T: "s",
    DIM_I: "A",
    DIM_THETA: "K",
    DIM_N: "mol",
    DIM_J: "cd",
    (2, 0, 0, 0, 0, 0, 0): "m^2",
    (3, 0, 0, 0, 0, 0, 0): "m^3",
    (1, 0, -1, 0, 0, 0, 0): "m/s",
    (1, 0, -2, 0, 0, 0, 0): "m/s^2",
    (0, 0, -1, 0, 0, 0, 0): "Hz",
    (1, 1, -2, 0, 0, 0, 0): "N",
    (-1, 1, -2, 0, 0, 0, 0): "Pa",
    (2, 1, -2, 0, 0, 0, 0): "J",
    (2, 1, -3, 0, 0, 0, 0): "W",
    (0, 0, 1, 1, 0, 0, 0): "C",
    (2, 1, -3, -1, 0, 0, 0): "V",
    (2, 1, -3, -2, 0, 0, 0): "Ω",
    (-2, -1, 3, 2, 0, 0, 0): "S",
    (-2, -1, 4, 2, 0, 0, 0): "F",
    (2, 1, -2, -1, 0, 0, 0): "Wb",
    (0, 1, -2, -1, 0, 0, 0): "T",
    (2, 1, -2, -2, 0, 0, 0): "H",
}


def add_dims(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(x + y for x, y in zip(a, b))


def sub_dims(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(x - y for x, y in zip(a, b))


def dims_equal(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    return a == b


def _format_parts(dim: tuple[int, ...], *, dimensionless_text: str) -> str:
    parts: list[str] = []
    for sym, exp in zip(BASE_SYMBOLS, dim):
        if exp == 0:
            continue
        parts.append(sym if exp == 1 else f"{sym}^{exp}")
    return "*".join(parts) if parts else dimensionless_text


def format_dimension(dim: tuple[int, ...]) -> str:
    """以 ``m*kg/s^2`` 形式输出量纲（用于错误提示）。"""
    text = _format_parts(dim, dimensionless_text="")
    return f"{text}（无量纲）" if not text else text


def format_generic_unit(dim: tuple[int, ...]) -> str:
    """量纲的通用相干单位字符串，如 ``m*kg/s^2``、``m^-2``。"""
    return _format_parts(dim, dimensionless_text="1")


# ---------------------------------------------------------------------------
# 单位定义
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnitDef:
    """单位定义。

    :param name: 规范名称
    :param dimension: 7 维量纲向量
    :param factor: 相对 SI 的倍数（value_si = factor * value + offset）
    :param offset: 仿射偏移（温度单位用）
    :param aliases: 别名集合
    :param si_prefix: 是否自动派生 SI 前缀单位
    :param builtin: 是否为内置单位
    """

    name: str
    dimension: tuple[int, ...]
    factor: Fraction = Fraction(1)
    offset: Fraction = Fraction(0)
    aliases: frozenset[str] = frozenset()
    si_prefix: bool = False
    builtin: bool = True

    def to_si(self, value: Fraction) -> Fraction:
        return self.factor * value + self.offset

    def from_si(self, value: Fraction) -> Fraction:
        return (value - self.offset) / self.factor


@dataclass
class UnitRegistry:
    """单位注册表：规范名 / 别名 -> :class:`UnitDef`。"""

    units: dict[str, UnitDef] = field(default_factory=dict)

    def add(self, unit: UnitDef) -> None:
        names = {unit.name, *unit.aliases}
        for n in names:
            if n in self.units:
                raise ValueError(f"单位名称冲突: {n!r}")
        for n in names:
            self.units[n] = unit

    def get(self, name: str) -> UnitDef | None:
        return self.units.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.units

    def canonical_units(self) -> list[UnitDef]:
        """去重后的全部内置单位（按规范名排序）。"""
        seen: set[str] = set()
        result: list[UnitDef] = []
        for u in self.units.values():
            if u.name not in seen:
                seen.add(u.name)
                result.append(u)
        return sorted(result, key=lambda u: u.name)


# SI 前缀：符号, 名称（中文提示用）, 指数
SI_PREFIXES: tuple[tuple[str, str, int], ...] = (
    ("q", "quecto", -30),
    ("r", "ronto", -27),
    ("y", "yocto", -24),
    ("z", "zepto", -21),
    ("a", "atto", -18),
    ("f", "femto", -15),
    ("p", "pico", -12),
    ("n", "nano", -9),
    ("µ", "micro", -6),
    ("m", "milli", -3),
    ("c", "centi", -2),
    ("d", "deci", -1),
    ("da", "deca", 1),
    ("h", "hecto", 2),
    ("k", "kilo", 3),
    ("M", "mega", 6),
    ("G", "giga", 9),
    ("T", "tera", 12),
    ("P", "peta", 15),
    ("E", "exa", 18),
    ("Z", "zetta", 21),
    ("Y", "yotta", 24),
    ("R", "ronna", 27),
    ("Q", "quetta", 30),
)

# 微符号的 ASCII 写法
_PREFIX_ALIASES = {"µ": "u"}


def _add_prefixed(registry: UnitRegistry, root: UnitDef) -> None:
    """为根单位自动生成 SI 前缀单位。"""
    for symbol, _pname, exp in SI_PREFIXES:
        factor = Fraction(10) ** exp
        name = f"{symbol}{root.name}"
        aliases = {f"{symbol}{a}" for a in root.aliases}
        if symbol in _PREFIX_ALIASES:
            aliases.add(f"{_PREFIX_ALIASES[symbol]}{root.name}")
            aliases.update(f"{_PREFIX_ALIASES[symbol]}{a}" for a in root.aliases)
        # 根单位本身若有别名，前缀后的单位仍可解析
        registry.add(
            UnitDef(
                name=name,
                dimension=root.dimension,
                factor=factor * root.factor,
                aliases=frozenset(aliases),
                builtin=True,
            )
        )


def _root(
    name: str,
    dim: tuple[int, ...],
    factor: Fraction | str | float = Fraction(1),
    offset: Fraction | str | float = Fraction(0),
    aliases: tuple[str, ...] = (),
    si_prefix: bool = True,
) -> UnitDef:
    return UnitDef(
        name=name,
        dimension=dim,
        factor=Fraction(factor) if not isinstance(factor, Fraction) else factor,
        offset=Fraction(offset) if not isinstance(offset, Fraction) else offset,
        aliases=frozenset(aliases),
        si_prefix=si_prefix,
    )


def build_registry() -> UnitRegistry:
    """构建内置单位表（应用启动时调用一次）。"""
    reg = UnitRegistry()

    # ---- 无量纲 ----
    reg.add(UnitDef("1", DIMENSIONLESS, aliases=frozenset({"一", "无量纲"}), si_prefix=False))
    reg.add(UnitDef("%", DIMENSIONLESS, factor=Fraction(1, 100), aliases=frozenset({"percent", "百分比"}), si_prefix=False))

    # ---- 基本单位 ----
    # 质量以 kg 为 SI 相干单位（factor=1），g = 10^-3 kg，
    # 从 kilo 起的前缀直接派生于 kg，避免与 g 的前缀重复。
    roots: list[UnitDef] = [
        _root("m", DIM_L, aliases=("meter", "metre", "米")),
        _root("s", DIM_T, aliases=("sec", "second", "秒")),
        _root("A", DIM_I, aliases=("amp", "ampere", "安", "安培")),
        _root("K", DIM_THETA, aliases=("kelvin", "开尔文")),
        _root("mol", DIM_N, aliases=("mole", "摩尔")),
        _root("cd", DIM_J, aliases=("candela", "坎德拉"), si_prefix=False),
    ]
    for r in roots:
        reg.add(r)
        if r.si_prefix:
            _add_prefixed(reg, r)

    reg.add(_root("kg", DIM_M, aliases=("kilogram", "千克", "公斤"), si_prefix=False))
    reg.add(UnitDef("g", DIM_M, factor=Fraction(1, 1000),
                    aliases=frozenset({"gram", "克"}), si_prefix=False))
    # SI 规定千克的词头加在“克”上：mg、µg … g kg Mg Gg …（没有“千千克”）
    for symbol, _pname, exp in SI_PREFIXES:
        if exp == 3:
            continue  # kg 已作为基本单位注册
        factor = Fraction(10) ** (exp - 3)
        name = f"{symbol}g"
        aliases = {f"{symbol}{a}" for a in ("gram", "克")}
        if symbol in _PREFIX_ALIASES:
            aliases.add(f"{_PREFIX_ALIASES[symbol]}g")
        reg.add(UnitDef(name=name, dimension=DIM_M, factor=factor,
                        aliases=frozenset(aliases), builtin=True))

    # ---- 温标（仿射单位，不加前缀） ----
    reg.add(UnitDef("°C", DIM_THETA, factor=Fraction(1), offset=Fraction(27315, 100),
                    aliases=frozenset({"Celsius", "degC", "摄氏度"}), si_prefix=False))
    reg.add(UnitDef("°F", DIM_THETA, factor=Fraction(5, 9),
                    offset=Fraction(45967, 100) * Fraction(5, 9),
                    aliases=frozenset({"Fahrenheit", "degF", "华氏度"}), si_prefix=False))
    # ---- 时间 ----
    reg.add(UnitDef("min", DIM_T, factor=Fraction(60), aliases=frozenset({"minute", "分钟", "分"})))
    reg.add(UnitDef("h", DIM_T, factor=Fraction(3600), aliases=frozenset({"hr", "hour", "小时", "时"})))
    reg.add(UnitDef("d", DIM_T, factor=Fraction(86400), aliases=frozenset({"day", "天", "日"})))
    reg.add(UnitDef("week", DIM_T, factor=Fraction(604800), aliases=frozenset({"周", "星期"})))
    reg.add(UnitDef("year", DIM_T, factor=Fraction(31557600), aliases=frozenset({"年"})))

    # ---- 长度（英制/航海） ----
    reg.add(UnitDef("in", DIM_L, factor=Fraction(254, 10000), aliases=frozenset({"inch", "英寸"})))
    reg.add(UnitDef("ft", DIM_L, factor=Fraction(3048, 10000), aliases=frozenset({"foot", "feet", "英尺"})))
    reg.add(UnitDef("yd", DIM_L, factor=Fraction(9144, 10000), aliases=frozenset({"yard", "码"})))
    reg.add(UnitDef("mi", DIM_L, factor=Fraction(1609344, 1000), aliases=frozenset({"mile", "英里"})))
    reg.add(UnitDef("nmi", DIM_L, factor=Fraction(1852), aliases=frozenset({"海里"})))
    reg.add(UnitDef("公里", DIM_L, factor=Fraction(1000)))
    reg.add(UnitDef("ly", DIM_L, factor=Fraction(9460730472580800), aliases=frozenset({"光年"})))
    reg.add(UnitDef("Å", DIM_L, factor=Fraction(1, 10000000000), aliases=frozenset({"angstrom", "埃"})))

    # ---- 质量 ----
    reg.add(UnitDef("t", DIM_M, factor=Fraction(1_000_000), aliases=frozenset({"tonne", "吨"})))
    reg.add(UnitDef("lb", DIM_M, factor=Fraction(45359237, 100000000), aliases=frozenset({"lbs", "pound", "磅"})))
    reg.add(UnitDef("oz", DIM_M, factor=Fraction(28349523125, 10**12), aliases=frozenset({"ounce", "盎司"})))
    reg.add(UnitDef("u", DIM_M, factor=Fraction(166053906660, 10**39), aliases=frozenset({"Da", "dalton"})))

    # ---- 面积/体积 ----
    reg.add(UnitDef("ha", add_dims(DIM_L, DIM_L), factor=Fraction(10000), aliases=frozenset({"hectare", "公顷"})))
    reg.add(UnitDef("acre", add_dims(DIM_L, DIM_L), factor=Fraction(40468564224, 10000)))
    reg.add(UnitDef("亩", add_dims(DIM_L, DIM_L), factor=Fraction(2000, 3)))
    reg.add(UnitDef("L", (3, 0, 0, 0, 0, 0, 0), factor=Fraction(1, 1000),
                    aliases=frozenset({"l", "liter", "litre", "升"})))
    _add_prefixed(reg, reg.get("L"))
    reg.add(UnitDef("gal", (3, 0, 0, 0, 0, 0, 0), factor=Fraction(3785411784, 10**15),
                    aliases=frozenset({"gallon", "加仑"})))

    # ---- 速度 ----
    reg.add(UnitDef("kph", (1, 0, -1, 0, 0, 0, 0), factor=Fraction(1000, 3600),
                    aliases=frozenset({"km/h", "千米每小时", "公里每小时"})))
    reg.add(UnitDef("mph", (1, 0, -1, 0, 0, 0, 0), factor=Fraction(1609344, 1000 * 3600),
                    aliases=frozenset({"英里每小时"})))
    reg.add(UnitDef("kn", (1, 0, -1, 0, 0, 0, 0), factor=Fraction(1852, 3600),
                    aliases=frozenset({"knot", "节"})))

    # ---- 力学 ----
    reg.add(UnitDef("Hz", sub_dims(DIMENSIONLESS, DIM_T), aliases=frozenset({"hertz", "赫兹"})))
    _add_prefixed(reg, reg.get("Hz"))
    reg.add(UnitDef("rpm", sub_dims(DIMENSIONLESS, DIM_T), factor=Fraction(1, 60),
                    aliases=frozenset({"转每分"})))
    reg.add(UnitDef("N", (1, 1, -2, 0, 0, 0, 0), aliases=frozenset({"newton", "牛", "牛顿"})))
    _add_prefixed(reg, reg.get("N"))
    reg.add(UnitDef("Pa", (-1, 1, -2, 0, 0, 0, 0), aliases=frozenset({"pascal", "帕", "帕斯卡"})))
    _add_prefixed(reg, reg.get("Pa"))
    reg.add(UnitDef("bar", (-1, 1, -2, 0, 0, 0, 0), factor=Fraction(100000),
                    aliases=frozenset({"巴"})))
    reg.add(UnitDef("atm", (-1, 1, -2, 0, 0, 0, 0), factor=Fraction(101325),
                    aliases=frozenset({"标准大气压", "大气压"})))
    reg.add(UnitDef("psi", (-1, 1, -2, 0, 0, 0, 0), factor=Fraction(6894757, 100),
                    aliases=frozenset({"磅每平方英寸"})))
    reg.add(UnitDef("Torr", (-1, 1, -2, 0, 0, 0, 0), factor=Fraction(101325, 760),
                    aliases=frozenset({"mmHg", "托"})))

    # ---- 能量/功率 ----
    reg.add(UnitDef("J", (2, 1, -2, 0, 0, 0, 0), aliases=frozenset({"joule", "焦耳"})))
    _add_prefixed(reg, reg.get("J"))
    reg.add(UnitDef("cal", (2, 1, -2, 0, 0, 0, 0), factor=Fraction(4184, 1000),
                    aliases=frozenset({"卡路里"})))
    reg.add(UnitDef("kcal", (2, 1, -2, 0, 0, 0, 0), factor=Fraction(4184),
                    aliases=frozenset({"大卡", "千卡"})))
    reg.add(UnitDef("Wh", (2, 1, -2, 0, 0, 0, 0), factor=Fraction(3600), aliases=frozenset({"瓦时"})))
    reg.add(UnitDef("kWh", (2, 1, -2, 0, 0, 0, 0), factor=Fraction(3_600_000),
                    aliases=frozenset({"度电", "千瓦时"})))
    reg.add(UnitDef("eV", (2, 1, -2, 0, 0, 0, 0), factor=Fraction(1602176634, 10**28),
                    aliases=frozenset({"电子伏"})))
    reg.add(UnitDef("W", (2, 1, -3, 0, 0, 0, 0), aliases=frozenset({"watt", "瓦", "瓦特"})))
    _add_prefixed(reg, reg.get("W"))
    reg.add(UnitDef("hp", (2, 1, -3, 0, 0, 0, 0), factor=Fraction(74569987158227022, 10**14),
                    aliases=frozenset({"马力"})))

    # ---- 电学 ----
    reg.add(UnitDef("C", (0, 0, 1, 1, 0, 0, 0), aliases=frozenset({"coulomb", "库仑"})))
    _add_prefixed(reg, reg.get("C"))
    reg.add(UnitDef("Ah", (0, 0, 1, 1, 0, 0, 0), factor=Fraction(3600), aliases=frozenset({"安时"})))
    reg.add(UnitDef("mAh", (0, 0, 1, 1, 0, 0, 0), factor=Fraction(3600, 1000),
                    aliases=frozenset({"毫安时"})))
    reg.add(UnitDef("V", (2, 1, -3, -1, 0, 0, 0), aliases=frozenset({"volt", "伏", "伏特"})))
    _add_prefixed(reg, reg.get("V"))
    reg.add(UnitDef("Ω", (2, 1, -3, -2, 0, 0, 0),
                    aliases=frozenset({"ohm", "欧姆"})))
    _add_prefixed(reg, reg.get("Ω"))
    reg.add(UnitDef("S", (-2, -1, 3, 2, 0, 0, 0), aliases=frozenset({"siemens", "西门子"})))
    _add_prefixed(reg, reg.get("S"))
    reg.add(UnitDef("F", (-2, -1, 4, 2, 0, 0, 0), aliases=frozenset({"farad", "法拉"})))
    _add_prefixed(reg, reg.get("F"))
    reg.add(UnitDef("Wb", (2, 1, -2, -1, 0, 0, 0), aliases=frozenset({"weber", "韦伯"})))
    reg.add(UnitDef("T", (0, 1, -2, -1, 0, 0, 0), aliases=frozenset({"tesla", "特斯拉"})))
    _add_prefixed(reg, reg.get("T"))
    reg.add(UnitDef("H", (2, 1, -2, -2, 0, 0, 0), aliases=frozenset({"henry", "亨利"})))

    # ---- 角度（无量纲） ----
    reg.add(UnitDef("rad", DIMENSIONLESS, aliases=frozenset({"弧度"})))
    reg.add(UnitDef("°", DIMENSIONLESS, factor=Fraction(3141592653589793, 180 * 10**15),
                    aliases=frozenset({"deg", "degree", "度"}), si_prefix=False))
    reg.add(UnitDef("arcmin", DIMENSIONLESS,
                    factor=Fraction(3141592653589793, 180 * 60 * 10**15),
                    aliases=frozenset({"角分"}), si_prefix=False))
    reg.add(UnitDef("arcsec", DIMENSIONLESS,
                    factor=Fraction(3141592653589793, 180 * 3600 * 10**15),
                    aliases=frozenset({"角秒"}), si_prefix=False))

    return reg
