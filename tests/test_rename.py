"""变量改名预览测试：映射校验、同时改名、遮蔽冲突与配方改写。"""
import pytest

from app.engine import analyze, rename_recipe


def rename_ok(expr, variables, renames, uncertainties=None):
    res = rename_recipe(expr, variables, uncertainties, renames)
    assert res["ok"], res["errors"]
    assert res["result"] is not None
    return res["result"], res


def rename_fail(expr, variables, renames, uncertainties=None):
    res = rename_recipe(expr, variables, uncertainties, renames)
    assert not res["ok"]
    assert res["result"] is None, "失败时不得输出部分配方"
    assert res["errors"], "应当返回错误"
    return res["errors"]


# ---------------- 基本改写 ----------------

def test_basic_rename_rewrites_everything():
    result, res = rename_ok(
        "0.5*mass*v^2 + mass*g0*h0",
        {"mass": "2 kg", "v": "10 m/s", "h0": "5 m"},
        {"mass": "m0"},
        {"mass": "0.01 kg", "v": "0.1 m/s"},
    )
    assert result["expression"] == "0.5*m0*v^2 + m0*g0*h0"
    assert result["variables"] == {"m0": "2 kg", "v": "10 m/s", "h0": "5 m"}
    # 不确定度键随变量名迁移，取值不变
    assert result["uncertainties"] == {"m0": "0.01 kg", "v": "0.1 m/s"}
    # 旧新名称对照
    assert result["renames"] == {"mass": "m0"}
    assert res["errors"] == []


def test_variable_definitions_rewritten():
    result, _ = rename_ok(
        "F / A",
        {"mass": "2 kg", "F": "mass * g0", "A": "0.5 m^2"},
        {"mass": "m0"},
    )
    assert result["variables"] == {"m0": "2 kg", "F": "m0 * g0", "A": "0.5 m^2"}
    assert result["expression"] == "F / A"


def test_only_identifiers_rewritten():
    # 数字、单位、函数调用、幂指数原样保留
    result, _ = rename_ok(
        "0.5*mass*v^2 + sqrt(mass*4 kg)*v^2",
        {"mass": "2 kg", "v": "10 m/s"},
        {"mass": "mass2"},
    )
    assert result["expression"] == "0.5*mass2*v^2 + sqrt(mass2*4 kg)*v^2"
    assert result["variables"]["mass2"] == "2 kg"


def test_builtin_unit_and_constant_references_kept():
    result, _ = rename_ok(
        "2*pi*r + 1 cm",
        {"r": "3 m"},
        {"r": "radius"},
    )
    # pi 与 cm 原本指向内置常量/单位，保持原样
    assert result["expression"] == "2*pi*radius + 1 cm"
    assert result["variables"] == {"radius": "3 m"}


def test_rename_result_is_equivalent_after_check():
    variables = {"mass": "2 kg", "v": "10 m/s", "h0": "5 m"}
    uncertainties = {"mass": "0.01 kg", "v": "0.1 m/s", "h0": "0.02 m"}
    expr = "0.5*mass*v^2 + mass*g0*h0"
    before = analyze(expr, variables, "kJ", uncertainties)
    assert before["ok"]
    result, _ = rename_ok(expr, variables, {"mass": "m0"}, uncertainties)
    after = analyze(result["expression"], result["variables"],
                    "kJ", result["uncertainties"])
    assert after["ok"]
    assert after["result"]["si_value"] == pytest.approx(before["result"]["si_value"])
    assert (after["result"]["uncertainty"]["si_value"]
            == pytest.approx(before["result"]["uncertainty"]["si_value"]))


# ---------------- 同时生效：交换与链式 ----------------

def test_swap_two_names():
    result, _ = rename_ok(
        "a * b",
        {"a": "1 m", "b": "2 s"},
        {"a": "b", "b": "a"},
        {"a": "0.1 m"},
    )
    assert result["variables"] == {"b": "1 m", "a": "2 s"}
    assert result["expression"] == "b * a"
    assert result["uncertainties"] == {"b": "0.1 m"}
    assert result["renames"] == {"a": "b", "b": "a"}


def test_chained_rename_is_simultaneous():
    # a→b、b→c 同时生效：a 的内容落到 b，b 的内容落到 c
    result, _ = rename_ok(
        "a + b",
        {"a": "1 m", "b": "2 m"},
        {"a": "b", "b": "c"},
    )
    assert result["variables"] == {"b": "1 m", "c": "2 m"}
    assert result["expression"] == "b + c"


def test_identity_rename_is_noop():
    result, _ = rename_ok("x + 1 m", {"x": "2 m"}, {"x": "x"})
    assert result["expression"] == "x + 1 m"
    assert result["variables"] == {"x": "2 m"}
    assert result["renames"] == {"x": "x"}


def test_empty_rename_map_returns_normalized_recipe():
    result, _ = rename_ok("x + 1 m", {"x": "2 m"}, {})
    assert result["expression"] == "x + 1 m"
    assert result["variables"] == {"x": "2 m"}
    assert result["uncertainties"] == {}
    assert result["renames"] == {}


# ---------------- 映射结构校验 ----------------

def test_old_name_must_exist():
    errors = rename_fail("x + 1 m", {"x": "2 m"}, {"ghost": "y"})
    assert errors[0]["kind"] == "rename_unknown"
    assert errors[0]["source"] == "rename"


def test_new_name_must_be_valid_identifier():
    for bad in ("2x", "x y", "m²", "x(", ""):
        errors = rename_fail("a + 1 m", {"a": "2 m"}, {"a": bad})
        assert errors[0]["kind"] == "rename_ident", bad
        assert errors[0]["source"] == "rename"


def test_new_name_must_not_be_function():
    errors = rename_fail("a + 1 m", {"a": "2 m"}, {"a": "sqrt"})
    assert errors[0]["kind"] == "rename_ident"


def test_renamed_names_must_not_collide():
    # 新名与未改名的现有变量重复
    errors = rename_fail("a * b", {"a": "1 m", "b": "2 s"}, {"a": "b"})
    assert errors[0]["kind"] == "rename_duplicate"
    # 两个旧名映射到同一个新名
    errors = rename_fail("a * b", {"a": "1 m", "b": "2 s"}, {"a": "x", "b": "x"})
    assert errors[0]["kind"] == "rename_duplicate"


# ---------------- 遮蔽冲突 ----------------

def test_shadow_conflict_in_expression():
    # 「1 kg」中的 kg 是内置单位引用，改名 x→kg 会遮蔽它
    errors = rename_fail("x * 1 kg", {"x": "2 m"}, {"x": "kg"})
    assert errors[0]["kind"] == "rename_shadow"
    assert errors[0]["source"] == "expression"
    assert "x * 1 kg"[errors[0]["start"]:errors[0]["end"]] == "kg"


def test_shadow_conflict_in_variable_definition():
    errors = rename_fail(
        "x * y",
        {"x": "1 m", "y": "2 kg"},
        {"x": "kg"},
    )
    assert errors[0]["kind"] == "rename_shadow"
    assert errors[0]["source"] == "var:y"
    assert "2 kg"[errors[0]["start"]:errors[0]["end"]] == "kg"


def test_shadow_conflict_with_constant():
    errors = rename_fail("x * pi", {"x": "2"}, {"x": "pi"})
    assert errors[0]["kind"] == "rename_shadow"
    assert errors[0]["source"] == "expression"


def test_rename_to_unit_name_without_reference_is_allowed():
    # kg 未在任何表达式中被引用：允许改名，仅给出遮蔽警告
    result, res = rename_ok("x", {"x": "1 m"}, {"x": "kg"})
    assert result["variables"] == {"kg": "1 m"}
    assert any(w["kind"] == "shadow_unit" for w in res["warnings"])


# ---------------- 改名前 / 改名后校验 ----------------

def test_invalid_original_recipe_is_rejected():
    # 量纲不一致
    errors = rename_fail("1 m + 1 s", {}, {})
    assert errors[0]["kind"] == "dim_mismatch"
    assert errors[0]["source"] == "expression"
    # 未定义名称
    errors = rename_fail("x + 1", {}, {})
    assert errors[0]["kind"] == "undefined"
    # 语法错误
    errors = rename_fail("1 +", {}, {})
    assert errors[0]["kind"] == "syntax"


def test_invalid_original_uncertainty_is_rejected():
    errors = rename_fail(
        "x", {"x": "1 m"}, {}, {"x": "-1 m"},
    )
    assert errors[0]["kind"] == "uncert_negative"
    assert errors[0]["source"] == "uncert:x"


def test_invalid_original_variable_definition_is_rejected():
    errors = rename_fail("y", {"y": "1 m + 1 s"}, {"y": "z"})
    assert errors[0]["kind"] == "dim_mismatch"
    assert errors[0]["source"] == "var:y"


def test_failure_does_not_leak_partial_result():
    res = rename_recipe("x * 1 kg", {"x": "2 m"}, {}, {"x": "kg"})
    assert res["ok"] is False
    assert res["result"] is None
