"""测量不确定度分析测试：校验、双侧中心差分、RSS 合成与错误中止。"""
import math

import pytest

from app.engine import analyze


def analyze_ok(expr, variables, uncertainties, target=None):
    res = analyze(expr, variables, target, uncertainties)
    assert res["ok"], res["errors"]
    assert res["result"] is not None
    return res["result"], res


def uc_of(expr, variables, uncertainties, target=None):
    r, _ = analyze_ok(expr, variables, uncertainties, target)
    assert r["uncertainty"] is not None
    return r["uncertainty"]


# ---------------- 基本 RSS 合成 ----------------

def test_linear_sum_independent_inputs():
    # y = a + b，u(a)=0.1 m，u(b)=0.2 m → uc = sqrt(.01+.04)
    u = uc_of("a + b", {"a": "1 m", "b": "2 m"}, {"a": "0.1 m", "b": "0.2 m"})
    assert u["si_value"] == pytest.approx(math.sqrt(0.05))
    assert u["method"] == "central_difference_rss"
    assert {s["name"] for s in u["sources"]} == {"a", "b"}


def test_energy_rss_matches_analytic_partials():
    # E = 0.5 m v^2 + m g0 h
    variables = {"mass": "2 kg", "v": "10 m/s", "h0": "5 m"}
    uncertainties = {"mass": "0.01 kg", "v": "0.1 m/s", "h0": "0.02 m"}
    u = uc_of("0.5*mass*v^2 + mass*g0*h0", variables, uncertainties)
    dm = 0.5 * 10 ** 2 + 9.80665 * 5
    dv = 2 * 10
    dh = 2 * 9.80665
    expect = math.sqrt((dm * 0.01) ** 2 + (dv * 0.1) ** 2 + (dh * 0.02) ** 2)
    assert u["si_value"] == pytest.approx(expect, rel=1e-7)
    by_name = {s["name"]: s for s in u["sources"]}
    assert by_name["mass"]["sensitivity"] == pytest.approx(dm, abs=1e-6)
    assert by_name["v"]["sensitivity"] == pytest.approx(dv, abs=1e-6)
    assert by_name["h0"]["sensitivity"] == pytest.approx(dh, abs=1e-6)


def test_uncertainty_unit_prefix_conversion():
    # 不确定度用 cm 给出，引擎内部换算到 SI(m)
    u = uc_of("x", {"x": "1 m"}, {"x": "2 cm"})
    assert u["si_value"] == pytest.approx(0.02)


def test_target_unit_converts_nominal_and_uncertainty_together():
    variables = {"mass": "2 kg", "v": "10 m/s"}
    u = uc_of("0.5*mass*v^2", variables, {"mass": "0.01 kg", "v": "0.1 m/s"},
              target="kJ")
    # SI: dE/dm=50, dE/dv=20 → uc = sqrt(.5^2 + 2^2) J = 2.06155 J → kJ
    assert u["target"]["unit"] == "kJ"
    assert u["target"]["value"] == pytest.approx(u["si_value"] / 1000)
    assert u["target"]["value"] == pytest.approx(0.0020615528128, rel=1e-7)


def test_full_variable_chain_recomputed_under_perturbation():
    # F = mass*g0；P = F/A。直接输入只有 mass 与 A
    variables = {"mass": "80 kg", "F": "mass * g0", "A": "0.05 m^2"}
    uncertainties = {"mass": "0.5 kg", "A": "0.0001 m^2"}
    r, _ = analyze_ok("F/A", variables, uncertainties, target="kPa")
    assert "Pa" in r["derived"]
    u = r["uncertainty"]
    dm = 9.80665 / 0.05
    da = 80 * 9.80665 / 0.05 ** 2
    expect_pa = math.sqrt((dm * 0.5) ** 2 + (da * 0.0001) ** 2)
    assert u["si_value"] == pytest.approx(expect_pa, rel=1e-7)
    assert u["target"]["value"] == pytest.approx(expect_pa / 1000, rel=1e-7)


def test_dimensionless_input_and_angle_units():
    u = uc_of("2*x", {"x": "10"}, {"x": "0.5"})
    assert u["si_value"] == pytest.approx(1.0)
    # 角度单位（无量纲）：sin(theta) 在 0 处灵敏度为 1，u 用 deg 给出
    u2 = uc_of("sin(theta)", {"theta": "0 rad"}, {"theta": "1 deg"})
    assert u2["si_value"] == pytest.approx(math.radians(1), rel=1e-8)


def test_nonlinear_function_central_difference():
    # y = x^2, x=3, u=0.01 → 2*3*0.01 = 0.06
    u = uc_of("x^2", {"x": "3"}, {"x": "0.01"})
    assert u["si_value"] == pytest.approx(0.06, abs=1e-9)


# ---------------- 零标称值：必须使用有效步长 ----------------

def test_zero_nominal_uses_effective_step():
    # y = 5 x，x 标称 0、u = 1 mm。结果标称 0，但不确定度必须为 5 mm
    u = uc_of("5*x", {"x": "0 m"}, {"x": "1 mm"})
    assert u["si_value"] == pytest.approx(0.005)
    src = u["sources"][0]
    assert src["step_si"] > 0
    # 步长由不确定度托底：1e-6 * max(0, 0.001)
    assert src["step_si"] == pytest.approx(1e-9)


def test_zero_nominal_zero_uncertainty_has_finite_step():
    res = analyze("5*x", {"x": "0 m"}, None, {"x": "0 m"})
    assert res["ok"]
    u = res["result"]["uncertainty"]
    assert u["si_value"] == 0.0
    assert u["sources"] == []


# ---------------- 校验错误：不返回不确定度数值 ----------------

def test_negative_uncertainty_rejected():
    res = analyze("x", {"x": "1 m"}, None, {"x": "-1 m"})
    assert not res["ok"]
    e = res["errors"][0]
    assert e["kind"] == "uncert_negative"
    assert e["source"] == "uncert:x"
    # 标称值仍展示，但不给出合成不确定度
    assert res["result"]["si_value"] == pytest.approx(1.0)
    assert res["result"]["uncertainty"] is None


def test_uncertainty_dimension_mismatch_rejected():
    res = analyze("x", {"x": "1 m"}, None, {"x": "1 s"})
    assert not res["ok"]
    assert res["errors"][0]["kind"] == "uncert_dim"
    assert res["errors"][0]["source"] == "uncert:x"
    assert res["result"]["uncertainty"] is None


def test_uncertainty_cannot_reference_variable():
    res = analyze("length", {"length": "2 m", "delta": "0.1 m"},
                  None, {"length": "delta"})
    assert not res["ok"]
    assert res["errors"][0]["kind"] == "uncert_ref"
    assert res["errors"][0]["source"] == "uncert:length"


def test_uncertainty_unknown_unit():
    res = analyze("x", {"x": "2 m"}, None, {"x": "1 furlong"})
    assert not res["ok"]
    assert res["errors"][0]["kind"] == "undefined"
    assert res["errors"][0]["source"] == "uncert:x"


def test_uncertainty_for_missing_variable():
    res = analyze("x", {"x": "2 m"}, None, {"y": "1 m"})
    assert not res["ok"]
    assert res["errors"][0]["kind"] == "uncert_unknown"
    assert res["errors"][0]["source"] == "uncert:y"


def test_indirect_variable_cannot_be_perturbation_source():
    res = analyze("F", {"mass": "2 kg", "F": "mass*g0"}, None, {"F": "0.1 N"})
    assert not res["ok"]
    assert res["errors"][0]["kind"] == "uncert_indirect"
    assert res["errors"][0]["source"] == "uncert:F"


def test_multiple_uncertainty_errors_all_reported():
    res = analyze("a + b", {"a": "1 m", "b": "2 m"},
                  None, {"a": "-1 m", "b": "1 s"})
    assert not res["ok"]
    kinds = {e["kind"] for e in res["errors"]}
    assert {"uncert_negative", "uncert_dim"} <= kinds
    assert res["result"]["uncertainty"] is None


# ---------------- 扰动期定义域/量纲错误：指出输入并停止汇总 ----------------

def test_perturbation_domain_error_stops_aggregation():
    # sqrt 在 x=0 边界：负向扰动使参数为负，必须报错且不返回合成值
    res = analyze("sqrt(x)", {"x": "0 m^2"}, None, {"x": "1 m^2"})
    assert not res["ok"]
    assert res["result"] is not None
    assert res["result"]["uncertainty"] is None
    e = res["errors"][0]
    assert e["perturbed"] == "x"
    assert e["direction"] == "-"
    assert "sqrt" in e["message"] or "负" in e["message"]


def test_perturbation_dimension_change_stops_aggregation():
    # (1 m)^x：x 出现在幂指数中，扰动后量纲改变 → 量纲错误，指出 x
    res = analyze("(1 m)^x", {"x": "2"}, None, {"x": "0.1"})
    assert not res["ok"]
    assert res["result"]["uncertainty"] is None
    e = res["errors"][0]
    assert e["kind"] == "uncert_eval"
    assert e["perturbed"] == "x"
    # 标称值仍然有效且保留
    assert res["result"]["si_value"] == pytest.approx(1.0)


def test_perturbation_chain_error_location_inside_variable():
    res = analyze("y", {"a": "0 m^2", "y": "sqrt(a)"}, None, {"a": "1 m^2"})
    assert not res["ok"]
    e = res["errors"][0]
    assert e["perturbed"] == "a"
    # 错误定位落在变量 y 定义式中的 sqrt 参数片段 a
    assert e["source"] == "var:y"
    assert "sqrt(a)"[e["start"]:e["end"]] == "a"


def test_nominal_failure_skips_uncertainty_and_returns_no_number():
    res = analyze("1 m + 1 s", {}, None, {})
    assert not res["ok"]
    assert res["result"] is None


def test_no_uncertainties_is_backward_compatible():
    res = analyze("2 m", {"x": "1 m"}, None, None)
    assert res["ok"]
    assert res["result"]["uncertainty"] is None
