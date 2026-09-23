"""端到端 API 测试（FastAPI TestClient）。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def calc(expression: str, variables=None, unit=None):
    payload: dict = {"expression": expression, "variables": variables or []}
    if unit is not None:
        payload["unit"] = unit
    return client.post("/calculate", json=payload)


def test_health_and_units_loaded_on_startup():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert int(r.json()["units_loaded"]) > 100

    r = client.get("/units")
    names = {u["name"] for u in r.json()["units"]}
    assert {"m", "kg", "s", "A", "K", "Pa", "N", "kW", "mm", "°C"} <= names


def test_pure_arithmetic_and_parentheses():
    r = calc("2 + 3 * (4 - 1)")
    assert r.status_code == 200
    body = r.json()
    assert body["value"] == 11
    assert body["unit"] == "1"


def test_variables_with_compatible_units_auto_convert():
    # 1 km + 500 m = 1500 m
    r = calc("a + b", [
        {"name": "a", "value": 1, "unit": "km"},
        {"name": "b", "value": 500, "unit": "m"},
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["value"] == 1500
    assert body["unit"] == "m"


def test_inline_unit_constants_and_implicit_multiplication():
    # 3 m + 2 mm = 3.002 m
    r = calc("3 m + 2 mm")
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "m"
    assert abs(body["value"] - 3.002) < 1e-12

    # 2000 mm = 2 m
    r2 = calc("2000 mm")
    assert r2.json()["value"] == 2


def test_imperial_to_si_conversion():
    r = calc("12 in + 1 ft")
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "m"
    assert abs(body["value"] - 0.6096) < 1e-12


def test_result_unit_conversion_mass():
    r = calc("2 kg", unit="g")
    assert r.status_code == 200
    assert r.json()["value"] == 2000
    assert r.json()["unit"] == "g"


def test_dimension_mismatch_addition_rejected_with_position():
    expr = "10 m + 5 s"
    r = calc(expr)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DIMENSION_MISMATCH"
    # '+' 位于索引 5
    assert err["position"]["offset"] == 5
    assert err["position"]["line"] == 1
    assert err["position"]["column"] == 6


def test_add_mass_to_length_rejected():
    r = calc("x + y", [
        {"name": "x", "value": 1, "unit": "kg"},
        {"name": "y", "value": 1, "unit": "m"},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DIMENSION_MISMATCH"


def test_division_by_dimensional_variable_rejected():
    # 距离 / 时间 = 速度：按工程约束禁止“带单位的变量之间相除”
    r = calc("d / t", [
        {"name": "d", "value": 100, "unit": "m"},
        {"name": "t", "value": 10, "unit": "s"},
    ])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DIVISION_WITH_UNIT"
    # 错误定位到出问题的除数变量 t（索引 4）
    assert err["position"]["offset"] == 4
    assert "t" in err["message"]


def test_division_by_compound_divisor_with_variable_rejected():
    r = calc("100 m / (2 * t)", [{"name": "t", "value": 5, "unit": "s"}])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DIVISION_WITH_UNIT"


def test_division_by_dimensionless_coefficient_allowed():
    r = calc("100 m / 4")
    assert r.status_code == 200
    body = r.json()
    assert body["value"] == 25
    assert body["unit"] == "m"


def test_compound_unit_built_from_unit_constants():
    # 单位常量相除允许：m/s
    r = calc("100 m / 10 s")
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "m/s"
    assert body["value"] == 10


def test_division_by_dimensionless_user_variable_allowed():
    r = calc("100 m / k", [{"name": "k", "value": 4, "unit": "1"}])
    assert r.status_code == 200
    assert r.json()["value"] == 25
    assert r.json()["unit"] == "m"


def test_division_by_dimensionless_number_variable_allowed():
    # 变量不指定单位时无量纲
    r = calc("100 m / k", [{"name": "k", "value": 4}])
    assert r.status_code == 200
    assert r.json()["value"] == 25


def test_division_by_zero_rejected_at_operator():
    r = calc("10 / 0")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DIVISION_BY_ZERO"
    assert err["position"]["offset"] == 3


def test_multiplication_propagates_dimensions():
    # 5 A * 2 h -> 36000 C
    r = calc("5 A * 2 h")
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "C"
    assert body["value"] == 36000


def test_force_mass_times_acceleration():
    r = calc("2 kg * 9.8 (m/s/s)")
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "N"
    assert abs(body["value"] - 19.6) < 1e-9


def test_temperature_absolute_conversion():
    # 100 °C -> 373.15 K
    r = calc("100 °C")
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "K"
    assert abs(body["value"] - 373.15) < 1e-9

    r2 = calc("212 °F")
    assert r2.status_code == 200
    assert abs(r2.json()["value"] - 373.15) < 1e-9


def test_temperature_addition_uses_absolute_scale():
    # 0 °C + 273.15 K = 546.3 K
    r = calc("0 °C + 273.15 K")
    assert r.status_code == 200
    assert abs(r.json()["value"] - 546.3) < 1e-9


def test_temperature_result_in_celsius():
    r = calc("373.15 K", unit="°C")
    assert r.status_code == 200
    assert abs(r.json()["value"] - 100.0) < 1e-9


def test_result_unit_wrong_dimension_rejected():
    r = calc("10 m", unit="s")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_DIM_MISMATCH"


def test_unknown_unit_in_variable():
    r = calc("x", [{"name": "x", "value": 1, "unit": "foo"}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_UNIT"


def test_undefined_variable_with_suggestion():
    r = calc("12 met")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNDEFINED_VARIABLE"
    assert err["position"]["offset"] == 3
    assert "metre" in err["message"] or "m" in err["message"]


def test_adjacent_identifiers_are_syntax_error():
    # a b 不被隐式成 a*b
    r = calc("a b", [{"name": "a", "value": 1, "unit": "m"}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNEXPECTED_TOKEN"


def test_missing_closing_parenthesis():
    r = calc("(2 + 3")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "MISSING_PAREN"
    # 定位到表达式末尾
    assert err["position"]["offset"] == 6


def test_invalid_character_position():
    r = calc("10 @ 3")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_CHAR"
    assert err["position"]["offset"] == 3


def test_unary_minus_and_double_neg():
    r = calc("-5 m - -3 m")
    assert r.status_code == 200
    assert r.json()["value"] == -2


def test_empty_expression_rejected():
    r = client.post("/calculate", json={"expression": "   "})
    assert r.status_code in (400, 422)


def test_power_keyword_not_supported():
    # m/s^2 中的 '^' 是非法字符，正确写法 m/s/s
    r = calc("1 m/s^2")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CHAR"


def test_percent_dimensionless():
    r = calc("50% + 0.5")
    assert r.status_code == 200
    assert r.json()["value"] == 1.0


def test_multiline_error_position():
    expr = "100 m\n+ 5 s"
    r = calc(expr)
    assert r.status_code == 422
    pos = r.json()["error"]["position"]
    assert pos["line"] == 2
    assert pos["column"] == 1


def test_chinese_aliases():
    r = calc("1 公里 + 500 m")
    assert r.status_code == 200
    assert r.json()["value"] == 1500


def test_duplicate_variable_rejected():
    r = calc("a", [
        {"name": "a", "value": 1, "unit": "m"},
        {"name": "a", "value": 2, "unit": "m"},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "DUPLICATE_VARIABLE"


def test_mass_si_prefixes():
    # SI：词头加在克上 —— 1 Mg = 1000 kg，1 mg = 1e-6 kg
    r = calc("1 Mg")
    assert r.status_code == 200
    assert r.json()["value"] == 1000
    r = calc("1 mg", unit="µg")
    assert r.status_code == 200
    assert r.json()["value"] == 1000
