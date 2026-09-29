"""FastAPI 接口测试（使用临时 SQLite 数据库）。"""
import os
import tempfile

import pytest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="dimcheck-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "量纲校验台" in r.text


def test_check_ok():
    r = client.post("/api/check", json={
        "expression": "0.5*mass*v^2",
        "variables": {"mass": "2 kg", "v": "10 m/s"},
        "target_unit": "kJ",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["result"]["dimension_str"] == "L^2·M·T^-2"
    assert data["result"]["target"]["value"] == 0.1
    assert "J" in data["result"]["derived"]


def test_check_error_with_span():
    r = client.post("/api/check", json={"expression": "1 m + 1 s"})
    data = r.json()
    assert data["ok"] is False
    err = data["errors"][0]
    assert err["kind"] == "dim_mismatch"
    assert "1 m + 1 s"[err["start"]:err["end"]] == "1 s"


def test_check_undefined_variable():
    r = client.post("/api/check", json={"expression": "x + 1"})
    data = r.json()
    assert data["ok"] is False
    assert data["errors"][0]["kind"] == "undefined"


def test_units_catalog():
    r = client.get("/api/units")
    assert r.status_code == 200
    data = r.json()
    names = {u["name"] for u in data["units"]}
    assert {"m", "kg", "s", "N", "J", "km", "kJ"} <= names
    assert any(f["name"] == "sqrt" for f in data["functions"])
    assert any(c["name"] == "g0" for c in data["constants"])


def test_cases_crud_and_export():
    # 创建
    r = client.post("/api/cases", json={
        "name": "自由落体",
        "expression": "0.5*g0*t^2",
        "variables": {"t": "3 s"},
        "target_unit": "m",
    })
    assert r.status_code == 201
    case = r.json()
    assert case["id"] >= 1
    assert case["variables"] == {"t": "3 s"}

    # 列表
    r = client.get("/api/cases")
    assert any(c["name"] == "自由落体" for c in r.json()["cases"])

    # 读取
    r = client.get(f"/api/cases/{case['id']}")
    assert r.status_code == 200
    assert r.json()["expression"] == "0.5*g0*t^2"

    # 导出
    r = client.get("/api/export")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    payload = r.json()
    assert payload["kind"] == "cases"
    assert any(c["name"] == "自由落体" for c in payload["cases"])

    # 删除
    r = client.delete(f"/api/cases/{case['id']}")
    assert r.status_code == 204
    r = client.get(f"/api/cases/{case['id']}")
    assert r.status_code == 404


def test_case_not_found():
    assert client.get("/api/cases/99999").status_code == 404
    assert client.delete("/api/cases/99999").status_code == 404


def test_check_with_uncertainties_and_target():
    r = client.post("/api/check", json={
        "expression": "0.5*mass*v^2",
        "variables": {"mass": "2 kg", "v": "10 m/s"},
        "uncertainties": {"mass": "0.01 kg", "v": "0.1 m/s"},
        "target_unit": "J",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    u = data["result"]["uncertainty"]
    # dE/dm = 50 → 0.5 J；dE/dv = 20 → 2 J；RSS = sqrt(4.25)
    assert u["si_value"] == pytest.approx((0.5 ** 2 + 2 ** 2) ** 0.5)
    assert u["target"]["unit"] == "J"
    assert u["target"]["value"] == pytest.approx(u["si_value"])
    assert {s["name"] for s in u["sources"]} == {"mass", "v"}


def test_check_uncertainty_validation_error():
    r = client.post("/api/check", json={
        "expression": "x",
        "variables": {"x": "1 m"},
        "uncertainties": {"x": "-1 m"},
    })
    data = r.json()
    assert data["ok"] is False
    assert data["errors"][0]["kind"] == "uncert_negative"
    assert data["errors"][0]["source"] == "uncert:x"
    # 标称值照常返回，但没有不确定度块
    assert data["result"]["si_value"] == 1.0
    assert data["result"]["uncertainty"] is None


def test_case_persists_uncertainties():
    r = client.post("/api/cases", json={
        "name": "不确定度案例",
        "expression": "0.5*mass*v^2",
        "variables": {"mass": "2 kg", "v": "10 m/s"},
        "uncertainties": {"mass": "0.01 kg", "v": "0.1 m/s"},
        "target_unit": "J",
    })
    assert r.status_code == 201
    cid = r.json()["id"]
    got = client.get(f"/api/cases/{cid}").json()
    assert got["uncertainties"] == {"mass": "0.01 kg", "v": "0.1 m/s"}
    assert any(c.get("uncertainties") == {"mass": "0.01 kg", "v": "0.1 m/s"}
               for c in client.get("/api/cases").json()["cases"])
    client.delete(f"/api/cases/{cid}")


# ---------------- 反求 /api/solve ----------------

def test_solve_ok():
    r = client.post("/api/solve", json={
        "expression": "0.5*mass*v^2",
        "variables": {"mass": "2 kg", "v": "1 m/s"},
        "solve_for": "v", "solve_unit": "m/s",
        "lower": 0, "upper": 20, "target": "200 J",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    res = data["result"]
    assert res["variable"] == "v"
    assert res["value"] == pytest.approx(200 ** 0.5, abs=1e-6)
    assert res["variable_si_value"] == pytest.approx(200 ** 0.5, abs=1e-6)
    assert res["unit"] == "m/s"
    assert abs(res["actual"]["residual_si"]) <= 1e-8 * 200
    assert res["actual"]["in_given_unit"]["unit"] == "J"
    assert res["actual"]["in_given_unit"]["value"] == pytest.approx(200.0, abs=2e-6)
    assert res["bounds_given"] == {"lower": 0.0, "upper": 20.0, "unit": "m/s"}
    assert 1 <= res["iterations"] <= 80


def test_solve_with_prefixed_units():
    # 区间用 km/h、目标用 kJ
    r = client.post("/api/solve", json={
        "expression": "0.5*mass*v^2",
        "variables": {"mass": "2 kg", "v": "1 m/s"},
        "solve_for": "v", "solve_unit": "km/h",
        "lower": 0, "upper": 100, "target": "0.2 kJ",
    })
    data = r.json()
    assert data["ok"] is True, data["errors"]
    res = data["result"]
    assert res["value"] == pytest.approx(200 ** 0.5 * 3.6, rel=1e-6)
    assert res["actual"]["in_given_unit"]["unit"] == "kJ"


def test_solve_no_bracket_error():
    r = client.post("/api/solve", json={
        "expression": "0.5*mass*v^2",
        "variables": {"mass": "2 kg", "v": "1 m/s"},
        "solve_for": "v", "solve_unit": "m/s",
        "lower": 0, "upper": 1, "target": "200 J",
    })
    data = r.json()
    assert data["ok"] is False
    assert data["result"] is None
    assert data["errors"][0]["kind"] == "solve_no_bracket"


def test_solve_validation_error():
    # 变量单位量纲不符
    r = client.post("/api/solve", json={
        "expression": "x",
        "variables": {"x": "1 m"},
        "solve_for": "x", "solve_unit": "kg",
        "lower": 0, "upper": 1, "target": "0.5 m",
    })
    data = r.json()
    assert data["ok"] is False
    assert data["errors"][0]["kind"] == "solve_unit_dim"
    assert data["errors"][0]["source"] == "solve_unit"


def test_solve_missing_field_is_http_422():
    r = client.post("/api/solve", json={
        "expression": "x", "variables": {"x": "1 m"},
        "solve_for": "x", "solve_unit": "m",
        "lower": 0, "target": "0.5 m",  # 缺 upper
    })
    assert r.status_code == 422


# ---------------- 改名预览 /api/rename ----------------

def test_rename_ok():
    r = client.post("/api/rename", json={
        "expression": "0.5*mass*v^2 + mass*g0*h0",
        "variables": {"mass": "2 kg", "v": "10 m/s", "h0": "5 m"},
        "uncertainties": {"mass": "0.01 kg"},
        "renames": {"mass": "m0"},
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    result = data["result"]
    assert result["expression"] == "0.5*m0*v^2 + m0*g0*h0"
    assert result["variables"] == {"m0": "2 kg", "v": "10 m/s", "h0": "5 m"}
    assert result["uncertainties"] == {"m0": "0.01 kg"}
    assert result["renames"] == {"mass": "m0"}


def test_rename_swap_via_api():
    r = client.post("/api/rename", json={
        "expression": "a * b",
        "variables": {"a": "1 m", "b": "2 s"},
        "renames": {"a": "b", "b": "a"},
    })
    data = r.json()
    assert data["ok"] is True
    assert data["result"]["variables"] == {"b": "1 m", "a": "2 s"}
    assert data["result"]["expression"] == "b * a"


def test_rename_unknown_old_name():
    r = client.post("/api/rename", json={
        "expression": "x + 1 m",
        "variables": {"x": "2 m"},
        "renames": {"ghost": "y"},
    })
    data = r.json()
    assert data["ok"] is False
    assert data["result"] is None
    assert data["errors"][0]["kind"] == "rename_unknown"


def test_rename_shadow_conflict():
    r = client.post("/api/rename", json={
        "expression": "x * 1 kg",
        "variables": {"x": "2 m"},
        "renames": {"x": "kg"},
    })
    data = r.json()
    assert data["ok"] is False
    assert data["result"] is None
    err = data["errors"][0]
    assert err["kind"] == "rename_shadow"
    assert err["source"] == "expression"
    assert "x * 1 kg"[err["start"]:err["end"]] == "kg"


def test_rename_invalid_original_recipe():
    r = client.post("/api/rename", json={
        "expression": "1 m + 1 s",
        "renames": {},
    })
    data = r.json()
    assert data["ok"] is False
    assert data["result"] is None
    assert data["errors"][0]["kind"] == "dim_mismatch"


def test_rename_defaults_allow_empty_body():
    # renames 缺省为空映射：原配方有效时返回规范化后的原样配方
    r = client.post("/api/rename", json={
        "expression": "x + 1 m",
        "variables": {"x": "2 m"},
    })
    data = r.json()
    assert data["ok"] is True
    assert data["result"]["renames"] == {}
    assert data["result"]["variables"] == {"x": "2 m"}


def test_rename_bad_type_is_http_422():
    r = client.post("/api/rename", json={
        "expression": "x", "variables": {"x": "1 m"},
        "renames": ["x", "y"],  # 应为对象
    })
    assert r.status_code == 422

