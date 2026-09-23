"""/resample 端到端测试（FastAPI TestClient）。"""
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def resample(samples, unit="m", step=1.0, max_gap=2.0, **extra):
    payload = {"samples": samples, "unit": unit, "step": step,
               "maxGap": max_gap, **extra}
    # 用原生 dumps 以便测试用例可携带 NaN/Infinity（服务端 json 解析接受）
    return client.post("/resample",
                       content=json.dumps(payload),
                       headers={"Content-Type": "application/json"})


def test_basic_regular_grid_with_interpolation():
    # 乱序提交，服务端排序；0 和 2 为真实样本，1 线性插值
    r = resample([
        {"timestamp": 2, "value": 30, "unit": "m"},
        {"timestamp": 0, "value": 10, "unit": "m"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "m"
    assert body["time_range"] == {"start": 0.0, "end": 2.0}
    assert body["gaps"] == []
    assert [(p["timestamp"], p["value"], p["interpolated"]) for p in body["points"]] == [
        (0.0, 10.0, False),
        (1.0, 20.0, True),
        (2.0, 30.0, False),
    ]
    assert body["sample_count"] == 2
    assert body["grid_count"] == 3
    assert body["interpolated_count"] == 1
    assert body["missing_count"] == 0


def test_grid_point_on_sample_is_not_interpolated():
    # 非对齐原始间隔（1.5），网格每 1 个点，1.0 落在样本上不算插值
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "m"},
        {"timestamp": 1.0, "value": 10.0, "unit": "m"},
        {"timestamp": 1.5, "value": 20.0, "unit": "m"},
        {"timestamp": 3.0, "value": 30.0, "unit": "m"},
    ], step=1.0, max_gap=2.0)
    assert r.status_code == 200, r.text
    pts = r.json()["points"]
    # 0=真实, 1=真实, 2=插值(1.5@20 -> 3@30 => 23.333...), 3=真实
    assert pts[0] == {"timestamp": 0.0, "value": 0.0, "interpolated": False}
    assert pts[1] == {"timestamp": 1.0, "value": 10.0, "interpolated": False}
    assert pts[2]["interpolated"] is True
    assert abs(pts[2]["value"] - 23.333333333333332) < 1e-9
    assert pts[3]["interpolated"] is False


def test_gap_is_preserved_as_missing_and_not_interpolated_across():
    # 样本 0 与 5 间隔 5 > maxGap 2：网格 1..4 全部 missing
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "m"},
        {"timestamp": 5, "value": 50.0, "unit": "m"},
    ], step=1.0, max_gap=2.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gaps"] == [{"start": 0.0, "end": 5.0}]
    assert body["missing_count"] == 4
    values = [p["value"] for p in body["points"]]
    assert values == [0.0, None, None, None, None, 50.0]
    assert all(p["interpolated"] is False for p in body["points"])
    # 缺口端点仍是真实样本，不是 missing
    assert body["points"][0] == {"timestamp": 0.0, "value": 0.0,
                                 "interpolated": False}
    assert body["points"][-1] == {"timestamp": 5.0, "value": 50.0,
                                  "interpolated": False}


def test_two_gaps_and_interpolation_between_close_samples():
    # 0@0, 2@20(间隔2 可插值), 6@60(间隔4 缺口)
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "m"},
        {"timestamp": 2, "value": 20.0, "unit": "m"},
        {"timestamp": 6, "value": 60.0, "unit": "m"},
    ], step=1.0, max_gap=2.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gaps"] == [{"start": 2.0, "end": 6.0}]
    flags = [(p["value"], p["interpolated"]) for p in body["points"]]
    assert flags == [
        (0.0, False), (10.0, True), (20.0, False),
        (None, False), (None, False), (None, False),
        (60.0, False),
    ]


def test_gap_boundary_is_exclusive_interpolation_at_exactly_max_gap():
    # 间隔恰好等于 maxGap（2）：允许插值，不形成缺口
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "m"},
        {"timestamp": 2, "value": 10.0, "unit": "m"},
    ], step=1.0, max_gap=2.0)
    body = r.json()
    assert body["gaps"] == []
    assert body["points"][1] == {"timestamp": 1.0, "value": 5.0,
                                 "interpolated": True}


def test_units_converted_to_target_before_grid():
    # km 与 m 混用，目标 km：1000 m = 1 km，中点 0.5 km
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "km"},
        {"timestamp": 2, "value": 1000.0, "unit": "m"},
    ], unit="km", step=1.0, max_gap=2.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "km"
    assert [p["value"] for p in body["points"]] == [0.0, 0.5, 1.0]


def test_temperature_affine_units_interpolate_in_target_scale():
    # 0 °C 与 100 °C 中点 = 50 °C（仿射变换保持线性插值）
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "°C"},
        {"timestamp": 2, "value": 100.0, "unit": "°C"},
    ], unit="°C", step=1.0, max_gap=2.0)
    assert r.status_code == 200, r.text
    points = r.json()["points"]
    assert points[1]["value"] == 50.0


def test_fractional_step_no_drift():
    # step=0.1 在浮点下会漂移；精确有理网格应恰好落在 0.3 上（真实样本）
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "m"},
        {"timestamp": 0.3, "value": 3.0, "unit": "m"},
        {"timestamp": 0.5, "value": 5.0, "unit": "m"},
    ], step=0.1, max_gap=1.0)
    assert r.status_code == 200, r.text
    pts = r.json()["points"]
    assert len(pts) == 6
    assert pts[3]["timestamp"] == 0.3
    assert pts[3]["value"] == 3.0
    assert pts[3]["interpolated"] is False
    assert pts[5]["timestamp"] == 0.5
    assert pts[5]["interpolated"] is False


def test_single_sample_yields_single_point():
    r = resample([{"timestamp": 4, "value": 7.5, "unit": "s"}],
                 unit="s", step=1.0, max_gap=2.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["time_range"] == {"start": 4.0, "end": 4.0}
    assert body["points"] == [{"timestamp": 4.0, "value": 7.5,
                               "interpolated": False}]
    assert body["gaps"] == []


def test_last_grid_point_may_precede_end_when_step_does_not_divide_span():
    # 跨度 2，步长 0.6 -> k=0..3，最后点 1.8，不补 2.4
    r = resample([
        {"timestamp": 0, "value": 0.0, "unit": "m"},
        {"timestamp": 2, "value": 20.0, "unit": "m"},
    ], step=0.6, max_gap=2.0)
    assert r.status_code == 200, r.text
    ts = [p["timestamp"] for p in r.json()["points"]]
    assert ts == [0.0, 0.6, 1.2, 1.8]


# ---- 422：时间戳重复 / 步长 / 单位非法（均带 field 定位） ----

def test_duplicate_timestamp_returns_422_with_field():
    r = resample([
        {"timestamp": 1, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_TIMESTAMP"
    assert err["field"] == "samples[1].timestamp"


def test_duplicate_timestamp_after_numeric_normalization():
    # 1 与 1.0 经精确有理比较仍视为重复
    r = resample([
        {"timestamp": 1.0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_TIMESTAMP"


def test_step_zero_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ], step=0.0)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["field"] == "step"


def test_step_negative_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ], step=-1.0)
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "step"


def test_step_nan_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ], step=float("nan"))
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "step"


def test_negative_max_gap_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ], max_gap=-0.1)
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "maxGap"


def test_missing_step_field_returns_422():
    r = client.post("/resample", json={
        "samples": [{"timestamp": 0, "value": 1.0, "unit": "m"}],
        "unit": "m", "maxGap": 2.0,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    assert r.json()["error"]["field"] == "step"


def test_empty_samples_returns_422():
    r = resample([])
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "samples"


def test_unknown_target_unit_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ], unit="zzz")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNKNOWN_UNIT"
    assert err["field"] == "unit"


def test_unknown_sample_unit_returns_422_with_indexed_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "nope"},
    ])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNKNOWN_UNIT"
    assert err["field"] == "samples[1].unit"


def test_inconsistent_sample_dimensions_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "s"},
    ])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DIMENSION_MISMATCH"
    assert err["field"] == "samples[1].unit"


def test_target_unit_dimension_mismatch_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": 1.0, "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ], unit="kg")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNIT_DIM_MISMATCH"
    assert err["field"] == "unit"


def test_non_finite_sample_value_returns_422_with_field():
    r = resample([
        {"timestamp": 0, "value": float("inf"), "unit": "m"},
        {"timestamp": 1, "value": 2.0, "unit": "m"},
    ])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_VALUE"
    assert err["field"] == "samples[0].value"


def test_camelcase_and_snake_case_max_gap_both_accepted():
    payload = {
        "samples": [
            {"timestamp": 0, "value": 0.0, "unit": "m"},
            {"timestamp": 1, "value": 10.0, "unit": "m"},
        ],
        "unit": "m", "step": 1.0, "max_gap": 2.0,
    }
    r = client.post("/resample", json=payload)
    assert r.status_code == 200, r.text
