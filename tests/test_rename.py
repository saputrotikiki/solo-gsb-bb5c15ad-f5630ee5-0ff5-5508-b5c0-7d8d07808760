"""变量改名（rename）测试：整体改写、交换名称、内置遮蔽拒绝与全量校验。"""
import math

import pytest

from app.engine import rename_recipe


def renamed(r):
    assert r["ok"], r["errors"]
    assert r["result"] is not None
    return r["result"]


def rejected(r, kind=None):
    assert not r["ok"]
    assert r["result"] is None
    assert r["errors"], "改名被拒绝时必须给出错误"
    if kind is not None:
        kinds = [e["kind"] for e in r["errors"]]
        assert kind in kinds, kinds
    return r["errors"]


RECIPE = dict(
    expression="0.5*mass*v^2 + mass*g0*h0",
    variables={"mass": "2 kg", "v": "10 m/s", "h0": "5 m"},
    uncertainties={"mass": "0.01 kg", "v": "0.1 m/s"},
)


# ---------------- 基本改写 ----------------

def test_basic_rename_rewrites_everything():
    r = rename_recipe(RECIPE["expression"], RECIPE["variables"],
                      RECIPE["uncertainties"], {"mass": "M", "v": "vel"})
    res = renamed(r)
    assert res["expression"] == "0.5*M*vel^2 + M*g0*h0"
    assert res["variables"] == {"M": "2 kg", "vel": "10 m/s", "h0": "5 m"}
    # 不确定度取值不变，键随变量名迁移
    assert res["uncertainties"] == {"M": "0.01 kg", "vel": "0.1 m/s"}
    assert res["renames"] == [{"old": "mass", "new": "M"},
                              {"old": "v", "new": "vel"}]
    # 改名后完整校验通过，结果数值不变
    check = res["check"]
    assert check["ok"]
    assert check["result"]["si_value"] == pytest.approx(198.0665)
    assert {s["name"] for s in check["result"]["uncertainty"]["sources"]} == {"M", "vel"}


def test_rename_follows_indirect_variable_chain():
    r = rename_recipe("F", {"F": "mass*g0", "mass": "2 kg"},
                      {"mass": "0.1 kg"}, {"mass": "m_tot"})
    res = renamed(r)
    assert res["expression"] == "F"
    assert res["variables"] == {"F": "m_tot*g0", "m_tot": "2 kg"}
    assert res["uncertainties"] == {"m_tot": "0.1 kg"}
    assert res["check"]["result"]["si_value"] == pytest.approx(2 * 9.80665)


def test_only_identifiers_change_numbers_units_calls_untouched():
    r = rename_recipe("2*sqrt(x) m", {"x": "4 m^2"}, None, {"x": "side"})
    res = renamed(r)
    assert res["expression"] == "2*sqrt(side) m"


# ---------------- 同时生效 / 交换名称 ----------------

def test_swap_two_variable_names():
    r = rename_recipe("a*b + a", {"a": "2", "b": "3"}, None,
                      {"a": "b", "b": "a"})
    res = renamed(r)
    # 映射按原名同时生效：原 a 的引用变 b，原 b 的引用变 a；定义随变量一起迁移
    assert res["expression"] == "b*a + b"
    assert res["variables"] == {"b": "2", "a": "3"}
    assert res["check"]["result"]["si_value"] == pytest.approx(8.0)


def test_partial_rename_keeps_unmapped_variables():
    r = rename_recipe(RECIPE["expression"], RECIPE["variables"], None,
                      {"v": "speed"})
    res = renamed(r)
    assert res["expression"] == "0.5*mass*speed^2 + mass*g0*h0"
    assert set(res["variables"]) == {"mass", "speed", "h0"}


def test_identity_rename_is_allowed():
    r = rename_recipe("a", {"a": "1 m"}, None, {"a": "a"})
    res = renamed(r)
    assert res["expression"] == "a"
    assert res["variables"] == {"a": "1 m"}
    assert res["renames"] == [{"old": "a", "new": "a"}]


# ---------------- 映射校验 ----------------

def test_old_name_must_exist():
    errs = rejected(rename_recipe("a", {"a": "1 m"}, None, {"b": "z"}),
                    "rename_missing_old")
    assert errs[0]["source"] == "rename:b"
    assert errs[0]["old"] == "b"


def test_new_name_must_follow_identifier_rules():
    for bad in ["1a", "a b", "a-b", "x²", "", "  a", "a  "]:
        errs = rejected(rename_recipe("a", {"a": "1 m"}, None, {"a": bad}),
                        "rename_bad_new")
        assert errs[0]["source"] == "rename:a"


def test_new_names_must_not_be_duplicated():
    errs = rejected(
        rename_recipe("a + b", {"a": "1 m", "b": "2 m"}, None,
                      {"a": "z", "b": "z"}),
        "rename_duplicate",
    )
    assert errs[0]["new"] == "z"
    assert errs[0]["conflicts_with"] == "a"


def test_rename_into_unmapped_existing_name_is_duplicate():
    errs = rejected(
        rename_recipe("a + b", {"a": "1 m", "b": "2 m"}, None, {"a": "b"}),
        "rename_duplicate",
    )
    # a→b 且 b 未改名，最终出现两个 b
    assert errs[0]["new"] == "b"


def test_all_mapping_errors_reported_atomically():
    r = rename_recipe("a", {"a": "1 m"}, None, {"b": "z", "a": "9bad"})
    errs = rejected(r)
    assert {e["kind"] for e in errs} == {"rename_missing_old", "rename_bad_new"}


# ---------------- 内置单位 / 常量遮蔽 ----------------

def test_shadow_builtin_constant_is_rejected_with_location():
    r = rename_recipe("x*g0", {"x": "1 kg"}, None, {"x": "g0"})
    errs = rejected(r, "rename_shadow")
    err = errs[0]
    assert err["source"] == "expression"
    # 定位到改写后串中未改写的内置常量 g0 片段（x→g0 后位置平移 +1）
    assert "g0*g0"[err["start"]:err["end"]] == "g0"
    assert err["start"] == 3
    assert err["shadowed"] == "g0"
    assert err["builtin_kind"] == "constant"


def test_shadow_builtin_unit_is_rejected():
    r = rename_recipe("x + 1 m", {"x": "1 m"}, None, {"x": "m"})
    errs = rejected(r, "rename_shadow")
    # 主表达式与 x 的定义式「1 m」各有一处冲突
    assert any(e["source"] == "expression" for e in errs)
    assert any(e["source"] == "var:x" for e in errs)
    assert all(e["shadowed"] == "m" for e in errs)
    assert all(e["builtin_kind"] == "unit" for e in errs)


def test_shadow_inside_variable_definition_is_rejected():
    r = rename_recipe("F", {"F": "x*g0", "x": "1 kg"}, None, {"x": "g0"})
    errs = rejected(r, "rename_shadow")
    err = errs[0]
    assert err["source"] == "var:F"
    assert "g0*g0"[err["start"]:err["end"]] == "g0"



def test_shadow_inside_uncertainty_is_rejected():
    r = rename_recipe("x", {"x": "1 m"}, {"x": "1 m"}, {"x": "m"})
    errs = rejected(r, "rename_shadow")
    assert any(e["source"] == "uncert:x" for e in errs)
    # 整次改名拒绝，不输出任何配方
    assert r["result"] is None


def test_shadow_error_span_survives_length_shift():
    # 新名更长，其后的未改写常量位置须随编辑平移；不遮蔽时正常通过
    r = rename_recipe("x*g0", {"x": "1 kg"}, None, {"x": "longer_name"})
    res = renamed(r)
    assert res["expression"] == "longer_name*g0"

    r2 = rename_recipe("xx*g0", {"xx": "1 kg"}, None, {"xx": "g0"})
    errs2 = rejected(r2, "rename_shadow")
    # xx→g0 等长：冲突位置不变，指向改写后串中保留的 g0
    assert "g0*g0"[errs2[0]["start"]:errs2[0]["end"]] == "g0"
    assert errs2[0]["start"] == 3

    # 非等长改名后位置平移：longer→g0（长度 -2），冲突 g0 位置前移
    r3 = rename_recipe("longer*g0", {"longer": "1 kg"}, None,
                       {"longer": "g0"})
    errs3 = rejected(r3, "rename_shadow")
    assert "g0*g0"[errs3[0]["start"]:errs3[0]["end"]] == "g0"
    assert errs3[0]["start"] == 3


def test_rename_to_function_name_keeps_call_syntax():
    # 函数调用 sqrt(...) 不是变量引用；变量改名为 sin 不影响 sqrt 调用
    r = rename_recipe("sqrt(x)", {"x": "4 m^2"}, None, {"x": "sin"})
    res = renamed(r)
    assert res["expression"] == "sqrt(sin)"
    assert res["check"]["result"]["si_value"] == pytest.approx(2.0)

def test_unused_builtin_name_only_warns():
    # 没有任何未改写的 m 引用 → 允许改名，仅给出与现有口径一致的遮蔽警告
    r = rename_recipe("x", {"x": "1"}, None, {"x": "m"})
    res = renamed(r)
    assert res["expression"] == "m"
    assert any(w["kind"] == "shadow_unit" and w["name"] == "m"
               for w in r["warnings"])


# ---------------- 改名前无效 / 改名后校验 ----------------

def test_invalid_recipe_before_rename_is_rejected_with_origin():
    r = rename_recipe("1 m + 1 s", {}, None, {})
    errs = rejected(r, "dim_mismatch")
    assert errs[0]["source"] == "expression"
    assert errs[0]["stage"] == "before_rename"
    assert "1 m + 1 s"[errs[0]["start"]:errs[0]["end"]] == "1 s"


def test_uncertainty_error_before_rename_keeps_source():
    r = rename_recipe("x", {"x": "1 m"}, {"x": "-1 m"}, {"x": "y"})
    errs = rejected(r, "uncert_negative")
    assert errs[0]["source"] == "uncert:x"
    assert errs[0]["stage"] == "before_rename"


def test_renamed_uncertainty_still_dimensionally_validated():
    # 改名后配方等价：不确定度量纲仍须匹配（这里匹配，应通过）
    r = rename_recipe("x", {"x": "1 m"}, {"x": "1 cm"}, {"x": "length"})
    res = renamed(r)
    assert res["uncertainties"] == {"length": "1 cm"}
    assert res["check"]["ok"]


# ---------------- 空映射与等价性 ----------------

def test_empty_rename_returns_unchanged_recipe():
    r = rename_recipe(RECIPE["expression"], RECIPE["variables"],
                      RECIPE["uncertainties"], {})
    res = renamed(r)
    assert res["expression"] == RECIPE["expression"]
    assert res["variables"] == RECIPE["variables"]
    assert res["uncertainties"] == RECIPE["uncertainties"]
    assert res["renames"] == []


def test_rename_preserves_nominal_value_and_uncertainty():
    r = rename_recipe(RECIPE["expression"], RECIPE["variables"],
                      RECIPE["uncertainties"], {"mass": "mass2", "v": "v2"})
    res = renamed(r)
    before = res["check"]
    assert before["result"]["si_value"] == pytest.approx(198.0665)
    uc = before["result"]["uncertainty"]["si_value"]
    # 与未改名配方的不确定度一致
    from app.engine import analyze
    ref = analyze(RECIPE["expression"], RECIPE["variables"], None,
                  RECIPE["uncertainties"])
    assert uc == pytest.approx(ref["result"]["uncertainty"]["si_value"])
