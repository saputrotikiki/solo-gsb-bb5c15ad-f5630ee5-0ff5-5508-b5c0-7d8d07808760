"""反求（inverse solve）测试：端点直返、二分收敛、单位换算、定义域与错误中止。"""
import math

import pytest

import app.engine as engine
from app.engine import solve_inverse


def solve_ok(expr, variables, *, solve_for, unit, lower, upper, target):
    res = solve_inverse(expr, variables, solve_for, unit, lower, upper, target)
    assert res["ok"], res["errors"]
    assert res["result"] is not None
    return res["result"]


def solve_fail(expr, variables, *, solve_for, unit, lower, upper, target, kind=None):
    res = solve_inverse(expr, variables, solve_for, unit, lower, upper, target)
    assert not res["ok"]
    assert res["result"] is None
    assert res["errors"], "应当返回错误"
    if kind is not None:
        assert res["errors"][0]["kind"] == kind, res["errors"]
    return res["errors"]


ENERGY = dict(expr="0.5*mass*v^2", variables={"mass": "2 kg", "v": "1 m/s"},
              solve_for="v", unit="m/s")


# ---------------- 基本收敛 ----------------

def test_energy_solve_for_velocity():
    r = solve_ok(target="200 J", lower=0, upper=20, **ENERGY)
    assert r["variable"] == "v"
    assert math.isclose(r["variable_si_value"], math.sqrt(200), abs_tol=1e-6)
    assert math.isclose(r["value"], math.sqrt(200), abs_tol=1e-6)
    assert r["unit"] == "m/s"
    assert abs(r["actual"]["residual_si"]) <= 1e-8 * 200
    assert math.isclose(r["actual"]["si_value"], 200, abs_tol=2e-6)
    assert r["actual"]["dimension_str"] == "L^2·M·T^-2"
    assert 1 <= r["iterations"] <= 80
    assert r["endpoint"] == ""
    assert r["method"] == "bisection"


def test_downstream_chain_recomputed():
    # F = mass*a，W = F*d；反求直接输入 a 使 W = 1000 J → a = 50 m/s²
    r = solve_ok(
        "F*d",
        {"mass": "2 kg", "a": "1 m/s^2", "F": "mass*a", "d": "10 m"},
        solve_for="a", unit="m/s^2", lower=0, upper=100, target="1000 J")
    assert math.isclose(r["value"], 50.0, rel_tol=1e-7)


def test_endpoint_hits_target_directly():
    # v = 10 m/s 恰使 E = 100 J（下界），不做二分
    r = solve_ok(target="100 J", lower=10, upper=20, **ENERGY)
    assert r["endpoint"] == "lower"
    assert r["iterations"] == 0
    assert r["variable_si_value"] == 10.0

    r = solve_ok(target="400 J", lower=0, upper=20, **ENERGY)
    assert r["endpoint"] == "upper"
    assert r["iterations"] == 0
    assert r["variable_si_value"] == 20.0


def test_no_bisection_when_same_sign():
    errs = solve_fail(target="200 J", lower=0, upper=1,
                      kind="solve_no_bracket", **ENERGY)
    assert "残差同号" in errs[0]["message"]


# ---------------- 单位与量纲 ----------------

def test_bounds_and_result_in_other_units():
    # 界用 km/h 给出，内部换算到 SI；回显单位值 = sqrt(200)*3.6 km/h
    r = solve_ok(target="200 J", lower=0, upper=100,
                 **{**ENERGY, "unit": "km/h"})
    assert math.isclose(r["value"], math.sqrt(200) * 3.6, rel_tol=1e-6)
    assert math.isclose(r["variable_si_value"], math.sqrt(200), abs_tol=1e-6)


def test_solve_variable_uses_gram_prefix():
    r = solve_ok("x", {"x": "1 kg"}, solve_for="x", unit="g",
                 lower=1000, upper=3000, target="2 kg")
    assert math.isclose(r["value"], 2000.0)
    assert math.isclose(r["variable_si_value"], 2.0)


def test_actual_result_echo_in_given_target_unit():
    r = solve_ok(target="0.2 kJ", lower=0, upper=100, **ENERGY)
    g = r["actual"]["in_given_unit"]
    assert g["unit"] == "kJ"
    assert math.isclose(g["value"], 0.2, abs_tol=2e-7)


def test_dimensionless_with_degrees():
    # sin(x) = 0.5，界以 deg 给出 → x = 30 deg
    r = solve_ok("sin(x)", {"x": "0 rad"}, solve_for="x", unit="deg",
                 lower=0, upper=90, target="0.5")
    assert math.isclose(r["value"], 30.0, abs_tol=1e-6)
    assert math.isclose(r["variable_si_value"], math.pi / 6, abs_tol=1e-7)


def test_zero_target():
    # x² − 1 m² = 0，x ∈ [0, 2 m] → x = 1 m；容差退化为绝对 1e-8
    r = solve_ok("x^2 - 1 m^2", {"x": "5 m"}, solve_for="x", unit="m",
                 lower=0, upper=2, target="0 m^2")
    assert math.isclose(r["value"], 1.0, abs_tol=1e-7)
    assert abs(r["actual"]["residual_si"]) <= 1e-8


# ---------------- 输入校验错误 ----------------

def test_unknown_variable():
    solve_fail("x", {"x": "1 m"}, solve_for="y", unit="m",
               lower=0, upper=1, target="0.5 m", kind="solve_var")


def test_empty_solve_for():
    solve_fail("x", {"x": "1 m"}, solve_for="", unit="m",
               lower=0, upper=1, target="0.5 m", kind="solve_var")


def test_indirect_variable_rejected():
    errs = solve_fail(
        "p", {"mass": "2 kg", "v": "1 m/s", "p": "mass*v"},
        solve_for="p", unit="kg*m/s", lower=0, upper=10,
        target="1 kg*m/s", kind="solve_indirect")
    assert "mass" in errs[0]["message"]


def test_unit_dimension_mismatch():
    solve_fail(target="200 J", lower=0, upper=20,
               kind="solve_unit_dim", **{**ENERGY, "unit": "kg"})


def test_unit_must_be_positive_quantity():
    solve_fail("x", {"x": "1 m"}, solve_for="x", unit="0 m",
               lower=0, upper=1, target="0.5 m", kind="solve_unit_value")


def test_bounds_must_be_finite_and_ordered():
    kw = dict(expr="x", variables={"x": "1 m"}, solve_for="x", unit="m",
              target="3 m")
    solve_fail(lower=5, upper=5, kind="solve_bounds", **kw)
    solve_fail(lower=5, upper=4, kind="solve_bounds", **kw)
    solve_fail(lower=float("nan"), upper=4, kind="solve_bounds", **kw)
    solve_fail(lower=0, upper=float("inf"), kind="solve_bounds", **kw)


def test_target_dimension_mismatch():
    errs = solve_fail(target="200 m", lower=0, upper=20,
                      kind="solve_target_dim", **ENERGY)
    assert errs[0]["source"] == "solve_target"


def test_target_reference_to_variable_rejected():
    solve_fail("x", {"x": "1 m"}, solve_for="x", unit="m",
               lower=0, upper=2, target="x", kind="solve_ref")


def test_target_non_finite():
    solve_fail("x", {"x": "1 m"}, solve_for="x", unit="m",
               lower=0, upper=2, target="1e999 m", kind="value")


def test_broken_expression_reported():
    errs = solve_fail("1 m +", {"x": "1 m"}, solve_for="x", unit="m",
                      lower=0, upper=1, target="0.5 m", kind="syntax")
    assert errs[0]["source"] == "expression"


def test_solve_variable_definition_error():
    solve_fail("x", {"x": "1 m + 1 s"}, solve_for="x", unit="m",
               lower=0, upper=2, target="0.5 m", kind="dim_mismatch")


# ---------------- 试算失败与超限 ----------------

def test_domain_error_during_endpoint_trial():
    # sqrt(x) 下端取 −1 m 触发定义域错误：保留内部 value kind 与试算上下文
    errs = solve_fail("sqrt(x)", {"x": "1 m"}, solve_for="x", unit="m",
                      lower=-1, upper=4, target="1.5", kind="value")
    e = errs[0]
    assert e["solve_for"] == "x"
    assert e["solve_at"] == "下"
    assert e["iteration"] is None
    assert e["source"] == "expression"
    assert "反求试算" in e["message"]


def test_dimension_error_during_trial_preserved():
    # 量纲错误在下端试算时暴露，保留 dim_mismatch 与表达式片段 source
    errs = solve_fail("1 m + 1 s", {"x": "1 m"}, solve_for="x", unit="m",
                      lower=0, upper=1, target="1 m", kind="dim_mismatch")
    assert errs[0]["source"] == "expression"


def test_non_finite_endpoint_result():
    # exp(1000) 为 inf：上端结果非有限
    errs = solve_fail("exp(x)", {"x": "0"}, solve_for="x", unit="1",
                      lower=0, upper=1000, target="2", kind="value")
    assert errs[0]["solve_at"] == "上"


def test_max_iterations_without_convergence(monkeypatch):
    monkeypatch.setattr(engine, "SOLVE_MAX_ITERATIONS", 2)
    errs = solve_fail(target="200 J", lower=0, upper=20,
                      kind="solve_max_iter", **ENERGY)
    assert "2" in errs[0]["message"]


def test_tolerance_scales_with_target_magnitude():
    # 目标 1e12 J：容差为 1e4 J，远大于机器精度，二分仍应正常收敛
    r = solve_ok(target="1e12 J", lower=0, upper=2e6, **ENERGY)
    assert abs(r["actual"]["residual_si"]) <= 1e-8 * 1e12
    assert r["tolerance_si"] == pytest.approx(1e4)


def test_nominal_value_is_overridden():
    # 变量原始取值 9 m 与目标无关，界 [0,1] m 内反求 2x = 1 m → 0.5 m
    r = solve_ok("2*x", {"x": "9 m"}, solve_for="x", unit="m",
                 lower=0, upper=1, target="1 m")
    assert math.isclose(r["value"], 0.5, abs_tol=1e-9)
