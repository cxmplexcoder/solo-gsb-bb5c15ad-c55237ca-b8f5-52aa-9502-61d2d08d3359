"""POST /resample 端到端测试（FastAPI TestClient）。"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ts(seconds: float) -> str:
    """以 2026-01-01T00:00:00Z 为起点偏移 seconds 秒的 ISO 时间戳。"""
    return (_BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sample(timestamp, value, unit="m"):
    return {"timestamp": timestamp, "value": value, "unit": unit}


def payload(samples, unit="m", step=None, max_gap=None):
    return {
        "samples": samples,
        "unit": unit,
        "step": step or {"value": 5, "unit": "s"},
        "max_gap": max_gap or {"value": 15, "unit": "s"},
    }


def resample(payload_dict):
    return client.post("/resample", json=payload_dict)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------

def test_regular_grid_linear_interpolation():
    r = resample(payload([sample(ts(0), 0), sample(ts(10), 10),
                          sample(ts(20), 20), sample(ts(30), 30)]))
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "m"
    assert body["dimension"] == [1, 0, 0, 0, 0, 0, 0]
    assert body["dimension_text"] == "m"
    assert body["step_seconds"] == 5.0
    assert body["max_gap_seconds"] == 15.0
    assert body["sample_count"] == 4
    assert body["range"] == {"start": ts(0), "end": ts(30)}
    assert body["gaps"] == []
    assert body["point_count"] == 7
    assert body["missing_count"] == 0
    assert [p["value"] for p in body["points"]] == [0, 5, 10, 15, 20, 25, 30]
    assert [p["interpolated"] for p in body["points"]] == \
        [False, True, False, True, False, True, False]
    assert all(not p["missing"] for p in body["points"])
    assert body["points"][1]["timestamp"] == ts(5)


def test_samples_sorted_and_converted_to_target_unit():
    # 乱序 + 混合单位：200 cm = 2 m
    r = resample(payload([
        sample(ts(20), 200, unit="cm"),
        sample(ts(0), 0),
        sample(ts(10), 1),
    ], step={"value": 10, "unit": "s"}))
    assert r.status_code == 200
    body = r.json()
    assert [p["timestamp"] for p in body["points"]] == [ts(0), ts(10), ts(20)]
    assert [p["value"] for p in body["points"]] == [0.0, 1.0, 2.0]

    # 目标单位为 cm：全部样本换算后输出
    r2 = resample(payload([sample(ts(0), 1), sample(ts(10), 2)], unit="cm"))
    assert r2.status_code == 200
    assert r2.json()["unit"] == "cm"
    assert [p["value"] for p in r2.json()["points"]] == [100.0, 150.0, 200.0]


def test_gap_segments_kept_missing_without_cross_gap_interpolation():
    r = resample(payload([
        sample(ts(0), 0), sample(ts(10), 10),
        sample(ts(100), 100), sample(ts(110), 110),
    ]))
    assert r.status_code == 200
    body = r.json()
    assert body["point_count"] == 23
    assert body["missing_count"] == 17
    assert body["gaps"] == [
        {"start": ts(10), "end": ts(100), "duration_seconds": 90.0},
    ]
    points = body["points"]
    # 缺口内（t=15..95）：保留 missing，不跨缺口插值
    for p in points[3:20]:
        assert p["value"] is None
        assert p["missing"] is True
        assert p["interpolated"] is False
    # 缺口边界的样本点仍是真实值
    assert points[2] == {"timestamp": ts(10), "value": 10.0,
                         "interpolated": False, "missing": False}
    assert points[20] == {"timestamp": ts(100), "value": 100.0,
                          "interpolated": False, "missing": False}
    # 缺口之后恢复正常插值
    assert points[21] == {"timestamp": ts(105), "value": 105.0,
                          "interpolated": True, "missing": False}
    assert points[22]["value"] == 110.0


def test_gap_equal_to_max_gap_still_interpolates():
    # 间隔 == max_gap：不超过上限，允许插值
    r = resample(payload([sample(ts(0), 0), sample(ts(15), 15)],
                         max_gap={"value": 15, "unit": "s"}))
    assert r.status_code == 200
    body = r.json()
    assert body["gaps"] == []
    assert [p["value"] for p in body["points"]] == [0.0, 5.0, 10.0, 15.0]


def test_gap_just_above_max_gap_is_missing():
    r = resample(payload([sample(ts(0), 0), sample(ts(16), 16)],
                         max_gap={"value": 15, "unit": "s"}))
    assert r.status_code == 200
    body = r.json()
    assert body["gaps"] == [
        {"start": ts(0), "end": ts(16), "duration_seconds": 16.0},
    ]
    # 网格为 t=0,5,10,15（t=16 不是 5 的整数倍，不在网格上）
    assert [p["value"] for p in body["points"]] == [0.0, None, None, None]
    assert body["range"]["end"] == ts(16)


def test_epoch_seconds_timestamps_accepted():
    r = resample(payload([sample(0, 0), sample(10, 10), sample(20, 20)],
                         step={"value": 10, "unit": "s"}))
    assert r.status_code == 200
    body = r.json()
    assert body["range"] == {"start": "1970-01-01T00:00:00Z",
                             "end": "1970-01-01T00:00:20Z"}
    assert [p["value"] for p in body["points"]] == [0.0, 10.0, 20.0]


def test_timezone_offsets_normalized_to_utc():
    r = resample(payload([
        sample("2026-01-01T00:00:10Z", 10),
        sample("2026-01-01T08:00:00+08:00", 0),  # 即 2026-01-01T00:00:00Z
    ]))
    assert r.status_code == 200
    body = r.json()
    assert body["range"]["start"] == "2026-01-01T00:00:00Z"
    assert [p["value"] for p in body["points"]] == [0.0, 5.0, 10.0]


def test_single_sample_yields_single_point():
    r = resample(payload([sample(ts(0), 42)]))
    assert r.status_code == 200
    body = r.json()
    assert body["points"] == [{"timestamp": ts(0), "value": 42.0,
                               "interpolated": False, "missing": False}]
    assert body["range"] == {"start": ts(0), "end": ts(0)}
    assert body["gaps"] == []


def test_step_larger_than_range_yields_first_point_only():
    r = resample(payload([sample(ts(0), 1), sample(ts(10), 2)],
                         step={"value": 1, "unit": "min"}))
    assert r.status_code == 200
    body = r.json()
    assert body["step_seconds"] == 60.0
    assert body["point_count"] == 1
    assert body["points"][0]["value"] == 1.0


def test_affine_temperature_units_interpolated_in_target_unit():
    r = resample(payload([sample(ts(0), 0, unit="°C"),
                          sample(ts(10), 10, unit="°C")],
                         unit="K"))
    assert r.status_code == 200
    values = [p["value"] for p in r.json()["points"]]
    assert len(values) == 3
    assert abs(values[0] - 273.15) < 1e-9
    assert abs(values[1] - 278.15) < 1e-9
    assert abs(values[2] - 283.15) < 1e-9


def test_root_lists_resample_endpoint():
    r = client.get("/")
    assert r.status_code == 200
    assert "/resample" in r.json()["endpoints"]


# ---------------------------------------------------------------------------
# 422：时间戳 / 步长 / 单位非法（含字段定位）
# ---------------------------------------------------------------------------

def test_duplicate_timestamp_rejected_with_field():
    r = resample(payload([sample(ts(0), 1), sample(ts(10), 2), sample(ts(0), 3)]))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_TIMESTAMP"
    assert err["field"] == "samples[2].timestamp"
    assert "samples[0]" in err["message"]


def test_same_instant_different_notation_is_duplicate():
    r = resample(payload([
        sample("2026-01-01T08:00:00+08:00", 1),
        sample("2026-01-01T00:00:00Z", 2),
    ]))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_TIMESTAMP"


def test_invalid_timestamp_string_rejected():
    r = resample(payload([sample("not-a-time", 1)]))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_TIMESTAMP"
    assert err["field"] == "samples[0].timestamp"


def test_non_positive_step_rejected():
    for bad in (0, -5):
        r = resample(payload([sample(ts(0), 1)], step={"value": bad, "unit": "s"}))
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "INVALID_STEP"
        assert err["field"] == "step.value"


def test_step_unit_must_be_time():
    r = resample(payload([sample(ts(0), 1)], step={"value": 5, "unit": "m"}))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNIT_DIM_MISMATCH"
    assert err["field"] == "step.unit"

    r = resample(payload([sample(ts(0), 1)], step={"value": 5, "unit": "foo"}))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNKNOWN_UNIT"
    assert err["field"] == "step.unit"


def test_non_positive_max_gap_rejected():
    r = resample(payload([sample(ts(0), 1)], max_gap={"value": 0, "unit": "s"}))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_MAX_GAP"
    assert err["field"] == "max_gap.value"


def test_unknown_target_unit_rejected():
    r = resample(payload([sample(ts(0), 1)], unit="foo"))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNKNOWN_UNIT"
    assert err["field"] == "unit"


def test_target_unit_dimension_mismatch():
    r = resample(payload([sample(ts(0), 1)], unit="s"))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNIT_DIM_MISMATCH"
    assert err["field"] == "unit"


def test_inconsistent_sample_units_rejected():
    r = resample(payload([sample(ts(0), 1), sample(ts(10), 2),
                          sample(ts(20), 3, unit="kg")]))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DIMENSION_MISMATCH"
    assert err["field"] == "samples[2].unit"


def test_unknown_sample_unit_rejected():
    r = resample(payload([sample(ts(0), 1, unit="foo")]))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNKNOWN_UNIT"
    assert err["field"] == "samples[0].unit"


def test_non_finite_sample_value_rejected():
    # httpx 的 json 编码器拒绝 NaN，改用原始请求体模拟非严格 JSON 客户端
    body = ('{"samples": [{"timestamp": "%s", "value": NaN, "unit": "m"}],'
            ' "unit": "m", "step": {"value": 5, "unit": "s"},'
            ' "max_gap": {"value": 15, "unit": "s"}}' % ts(0))
    r = client.post("/resample", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_VALUE"
    assert err["field"] == "samples[0].value"


def test_too_many_grid_points_rejected():
    r = resample(payload([sample(ts(0), 1), sample(ts(10), 2)],
                         step={"value": 0.000001, "unit": "s"}))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_STEP"
    assert err["field"] == "step.value"


def test_missing_required_fields_located():
    r = client.post("/resample", json={})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert "samples" in err["message"]

    # 至少 1 个样本
    r = client.post("/resample", json={"samples": [], "unit": "m",
                                       "step": {"value": 5},
                                       "max_gap": {"value": 15}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
