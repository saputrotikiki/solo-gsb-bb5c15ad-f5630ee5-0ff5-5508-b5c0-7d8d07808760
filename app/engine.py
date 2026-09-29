"""量纲推导引擎：词法分析 → 递归下降解析 → 量纲/数值求值。

支持：
  - 四则运算、幂运算（^ 或 **，右结合）、括号、一元正负号
  - 隐式乘法：``2 m``、``2(1+1)``、``kg m/s^2``、``2π r``
  - 数字后跟单位：``9.8 m/s^2``、``1.5e-3 mol``
  - 上标 Unicode：``m²``、``x⁻¹``
  - 全角/符号运算符：× ÷ · −
  - 函数调用：sqrt / abs / sin / cos / tan / asin / acos / atan / exp / ln / log / log2
  - 变量引用（变量值本身也是带单位的表达式，可相互引用，检测循环）

所有错误都携带 (start, end) 片段位置，指回原始表达式字符串。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction

from .units import (
    CONSTANTS, DIMLESS, DIM_NAMES, FUNCTIONS, UNITS,
    derived_matches, dim_str, format_fraction, si_unit_str,
)

# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

@dataclass
class AnalysisError:
    message: str
    start: int
    end: int
    kind: str = "error"
    source: str = "expression"  # expression / target / var:<name> / uncert:<name>
    extra: dict | None = None   # 附加字段，如扰动来源 {"perturbed": name, "direction": "+" | "-"}

    def to_dict(self) -> dict:
        d = {
            "message": self.message,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "source": self.source,
        }
        if self.extra:
            d.update(self.extra)
        return d


class EngineError(Exception):
    """携带片段位置的引擎内部异常。"""

    def __init__(self, message: str, start: int, end: int, kind: str = "error"):
        super().__init__(message)
        self.error = AnalysisError(message, start, end, kind)


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

@dataclass
class Token:
    kind: str          # num / ident / op / eof
    text: str
    start: int
    end: int
    value: float = 0.0


_NUM_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

_SUPERSCRIPTS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-",
}

# 单字符运算符（含常见全角/符号别名，长度均为 1，不影响片段定位）
_CHAR_OPS = {
    "+": "+", "-": "-", "−": "-",
    "*": "*", "×": "*", "·": "*", "⋅": "*",
    "/": "/", "÷": "/",
    "^": "^", "(": "(", ")": ")", ",": ",",
}

_IDENT_EXTRA_START = "_°%"   # isalpha() 之外的标识符起始字符
_IDENT_EXTRA_CONT = "_"


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        # 注意：'²' 等上标字符的 isdigit() 为真，数字判断必须限定 ASCII
        if ch in "0123456789" or (ch == "." and i + 1 < n and src[i + 1] in "0123456789"):
            m = _NUM_RE.match(src, i)
            assert m is not None
            text = m.group()
            tokens.append(Token("num", text, i, m.end(), float(text)))
            i = m.end()
            continue
        if ch in _SUPERSCRIPTS:
            j = i
            digits = ""
            while j < n and src[j] in _SUPERSCRIPTS:
                digits += _SUPERSCRIPTS[src[j]]
                j += 1
            # 上标序列 → 幂运算：^ <digits>（^ 为零宽 token，指向上标起点）
            tokens.append(Token("op", "^", i, i))
            tokens.append(Token("num", digits, i, j, float(digits)))
            i = j
            continue
        if ch.isalpha() or ch in _IDENT_EXTRA_START:
            j = i + 1
            # 注意：'²' 等上标字符的 isalnum() 为真，必须显式排除
            while j < n and src[j] not in _SUPERSCRIPTS and (
                src[j].isalnum() or src[j] in _IDENT_EXTRA_CONT
            ):
                j += 1
            tokens.append(Token("ident", src[i:j], i, j))
            i = j
            continue
        if ch == "*" and i + 1 < n and src[i + 1] == "*":
            tokens.append(Token("op", "^", i, i + 2))
            i += 2
            continue
        if ch in _CHAR_OPS:
            tokens.append(Token("op", _CHAR_OPS[ch], i, i + 1))
            i += 1
            continue
        raise EngineError(f"无法识别的字符「{ch}」", i, i + 1, "syntax")
    tokens.append(Token("eof", "", n, n))
    return tokens


# ---------------------------------------------------------------------------
# 语法分析（递归下降）
# ---------------------------------------------------------------------------

@dataclass
class Node:
    kind: str                       # num / name / neg / bin / pow / call / group
    start: int
    end: int
    value: float = 0.0
    name: str = ""
    op: str = ""
    left: "Node | None" = None
    right: "Node | None" = None
    args: list = field(default_factory=list)


class Parser:
    """文法（优先级由低到高）：
        add  := mul  (('+'|'-') mul)*
        mul  := unary (('*'|'/'|隐式) unary)*
        unary:= ('-'|'+') unary | pow
        pow  := primary ('^' unary)?          （右结合；指数允许负号）
        primary := NUMBER | IDENT | IDENT '(' args ')' | '(' add ')'
    隐式乘法：primary 之后紧跟 NUMBER / IDENT / '(' 时视为相乘。
    """

    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.i = 0

    def peek(self) -> Token:
        return self.toks[self.i]

    def advance(self) -> Token:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect_op(self, op: str) -> Token:
        tok = self.peek()
        if tok.kind != "op" or tok.text != op:
            raise EngineError(f"此处应为「{op}」", tok.start, max(tok.end, tok.start + 1), "syntax")
        return self.advance()

    def parse(self) -> Node:
        if self.peek().kind == "eof":
            raise EngineError("表达式为空", 0, 0, "syntax")
        node = self.parse_add()
        tok = self.peek()
        if tok.kind != "eof":
            raise EngineError(f"此处有多余的内容「{tok.text}」", tok.start, max(tok.end, tok.start + 1), "syntax")
        return node

    def parse_add(self) -> Node:
        left = self.parse_mul()
        while self.peek().kind == "op" and self.peek().text in ("+", "-"):
            op = self.advance()
            right = self.parse_mul()
            left = Node("bin", left.start, right.end, op=op.text, left=left, right=right)
        return left

    def parse_mul(self) -> Node:
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.text in ("*", "/"):
                self.advance()
                right = self.parse_unary()
                left = Node("bin", left.start, right.end, op=tok.text, left=left, right=right)
            elif tok.kind in ("num", "ident") or (tok.kind == "op" and tok.text == "("):
                right = self.parse_unary()  # 隐式乘法
                left = Node("bin", left.start, right.end, op="*", left=left, right=right)
            else:
                return left

    def parse_unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "op" and tok.text == "-":
            self.advance()
            operand = self.parse_unary()
            return Node("neg", tok.start, operand.end, left=operand)
        if tok.kind == "op" and tok.text == "+":
            self.advance()
            return self.parse_unary()
        return self.parse_pow()

    def parse_pow(self) -> Node:
        base = self.parse_primary()
        tok = self.peek()
        if tok.kind == "op" and tok.text == "^":
            self.advance()
            exponent = self.parse_unary()
            return Node("pow", base.start, exponent.end, left=base, right=exponent)
        return base

    def parse_primary(self) -> Node:
        tok = self.peek()
        if tok.kind == "num":
            self.advance()
            return Node("num", tok.start, tok.end, value=tok.value)
        if tok.kind == "ident":
            self.advance()
            name = tok.text
            nxt = self.peek()
            if name in FUNCTIONS and nxt.kind == "op" and nxt.text == "(":
                self.advance()
                args: list[Node] = []
                if not (self.peek().kind == "op" and self.peek().text == ")"):
                    args.append(self.parse_add())
                    while self.peek().kind == "op" and self.peek().text == ",":
                        self.advance()
                        args.append(self.parse_add())
                rpar = self.expect_op(")")
                return Node("call", tok.start, rpar.end, name=name, args=args)
            return Node("name", tok.start, tok.end, name=name)
        if tok.kind == "op" and tok.text == "(":
            self.advance()
            inner = self.parse_add()
            rpar = self.expect_op(")")
            return Node("group", tok.start, rpar.end, left=inner)
        raise EngineError("此处应为数字、变量、单位或括号", tok.start, max(tok.end, tok.start + 1), "syntax")


# ---------------------------------------------------------------------------
# 求值（数值 + 量纲递归推导）
# ---------------------------------------------------------------------------

@dataclass
class Quantity:
    value: float      # 以 SI 基本单位计的数值
    dim: tuple        # 量纲向量


def _dim_add(a: tuple, b: tuple) -> tuple:
    return tuple(x + y for x, y in zip(a, b))


def _dim_sub(a: tuple, b: tuple) -> tuple:
    return tuple(x - y for x, y in zip(a, b))


def _dim_scale(d: tuple, k: Fraction) -> tuple:
    return tuple(x * k for x in d)


class Evaluator:
    """对 AST 递归求值。resolve(name, node) 由外层注入，用于解析变量/单位/常量。"""

    def __init__(self, resolve):
        self.resolve = resolve

    def eval(self, node: Node) -> Quantity:
        kind = node.kind
        if kind == "num":
            return Quantity(node.value, DIMLESS)
        if kind == "group":
            return self.eval(node.left)
        if kind == "name":
            return self.resolve(node.name, node)
        if kind == "neg":
            q = self.eval(node.left)
            return Quantity(-q.value, q.dim)
        if kind == "call":
            args = [self.eval(a) for a in node.args]
            return self._call(node, args)
        if kind == "bin":
            return self._bin(node)
        if kind == "pow":
            return self._pow(node)
        raise EngineError(f"内部错误：未知节点 {kind}", node.start, node.end)

    # -- 二元运算 -----------------------------------------------------------
    def _bin(self, node: Node) -> Quantity:
        left = self.eval(node.left)
        right = self.eval(node.right)
        op = node.op
        if op in ("+", "-"):
            if left.dim != right.dim:
                word = "加" if op == "+" else "减"
                raise EngineError(
                    f"量纲不一致：「{dim_str(left.dim)}」与「{dim_str(right.dim)}」不能相{word}",
                    node.right.start, node.right.end, "dim_mismatch",
                )
            value = left.value + right.value if op == "+" else left.value - right.value
            return Quantity(value, left.dim)
        if op == "*":
            return Quantity(left.value * right.value, _dim_add(left.dim, right.dim))
        if op == "/":
            if right.value == 0:
                raise EngineError("除数为零", node.right.start, node.right.end, "value")
            return Quantity(left.value / right.value, _dim_sub(left.dim, right.dim))
        raise EngineError(f"内部错误：未知运算符 {op}", node.start, node.end)

    # -- 幂运算 -------------------------------------------------------------
    def _pow(self, node: Node) -> Quantity:
        base = self.eval(node.left)
        expo = self.eval(node.right)
        if expo.dim != DIMLESS:
            raise EngineError(
                f"幂指数必须无量纲（实际量纲为 {dim_str(expo.dim)}）",
                node.right.start, node.right.end, "bad_exponent",
            )
        k = Fraction(expo.value).limit_denominator(1_000_000)
        try:
            value = base.value ** float(k)
        except ZeroDivisionError:
            raise EngineError("零的负数次幂无意义", node.start, node.end, "value") from None
        except OverflowError:
            raise EngineError("数值溢出", node.start, node.end, "value") from None
        if isinstance(value, complex):
            raise EngineError("负数不能取分数次幂", node.start, node.end, "value")
        return Quantity(value, _dim_scale(base.dim, k))

    # -- 函数调用 -----------------------------------------------------------
    def _call(self, node: Node, args: list[Quantity]) -> Quantity:
        name = node.name
        meta = FUNCTIONS[name]
        if len(args) != meta["arity"]:
            raise EngineError(
                f"函数 {name} 需要 {meta['arity']} 个参数，实际给了 {len(args)} 个",
                node.start, node.end, "arity",
            )
        arg_node = node.args[0]
        q = args[0]
        if name == "sqrt":
            if q.value < 0:
                raise EngineError("sqrt 的参数不能为负数", arg_node.start, arg_node.end, "value")
            return Quantity(math.sqrt(q.value), _dim_scale(q.dim, Fraction(1, 2)))
        if name == "abs":
            return Quantity(abs(q.value), q.dim)
        if q.dim != DIMLESS:
            raise EngineError(
                f"函数 {name} 的参数必须无量纲（实际量纲为 {dim_str(q.dim)}）",
                arg_node.start, arg_node.end, "dimless_arg",
            )
        fn = {
            "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "asin": math.asin, "acos": math.acos, "atan": math.atan,
            "exp": math.exp, "ln": math.log, "log": math.log10, "log2": math.log2,
        }[name]
        try:
            return Quantity(fn(q.value), DIMLESS)
        except ValueError:
            raise EngineError(f"函数 {name} 的参数超出定义域", arg_node.start, arg_node.end, "value") from None
        except OverflowError:
            raise EngineError("数值溢出", arg_node.start, arg_node.end, "value") from None


# ---------------------------------------------------------------------------
# 顶层分析入口
# ---------------------------------------------------------------------------

def parse_src(src: str) -> Node:
    """词法 + 语法分析（不做求值），便于复用 AST 反复重算。"""
    return Parser(tokenize(src)).parse()


def _parse_and_eval(src: str, resolve) -> Quantity:
    return Evaluator(resolve).eval(parse_src(src))


def _node_names(node: Node) -> set[str]:
    """收集 AST 中出现的全部标识符。"""
    names: set[str] = set()

    def walk(n: Node) -> None:
        if n.kind == "name":
            names.add(n.name)
        if n.left is not None:
            walk(n.left)
        if n.right is not None:
            walk(n.right)
        for a in n.args:
            walk(a)

    walk(node)
    return names


def _unit_only_resolver(variable_names: set[str]):
    """仅解析内置常量与单位（不确定度表达式中不允许引用变量）。"""
    def resolve(name: str, node: Node) -> Quantity:
        if name in CONSTANTS:
            value, d, _ = CONSTANTS[name]
            return Quantity(value, d)
        if name in UNITS:
            d, factor, _, _ = UNITS[name]
            return Quantity(factor, d)
        if name in variable_names:
            raise EngineError(
                f"不确定度中不能引用变量「{name}」，标准不确定度必须是带单位的独立测量值",
                node.start, node.end, "uncert_ref",
            )
        raise EngineError(f"未定义的单位或常量「{name}」", node.start, node.end, "undefined")
    return resolve


def _make_var_resolver(variables: dict[str, str], var_ast: dict[str, Node],
                       var_qty: dict[str, Quantity], var_err: dict[str, EngineError],
                       stack: list[str], overrides: dict[str, Quantity] | None = None,
                       propagate_inner: bool = False):
    """构造变量解析闭包；overrides 中的变量直接取给定 Quantity（用于扰动）。

    propagate_inner=True 时（扰动重算），传播变量错误会保留内部错误的
    kind / 片段位置 / source，便于精确定位到变量链中真正出错的表达式。
    """
    overrides = overrides or {}

    def wrap_inner(name: str, node: Node) -> EngineError:
        inner = var_err[name].error
        if propagate_inner:
            exc = EngineError(
                f"变量「{name}」计算出错：{inner.message}",
                inner.start, inner.end, inner.kind,
            )
            exc.error.source = inner.source
            return exc
        return EngineError(
            f"变量「{name}」计算出错：{inner.message}",
            node.start, node.end, "var_error",
        )

    def evaluate_variable(name: str) -> None:
        if name in var_qty or name in var_err:
            return
        stack.append(name)
        try:
            var_qty[name] = Evaluator(resolve).eval(var_ast[name])
        except EngineError as exc:
            exc.error.source = f"var:{name}"
            var_err[name] = exc
        except OverflowError:
            err = EngineError("数值溢出", 0, len(variables[name]), "value")
            err.error.source = f"var:{name}"
            var_err[name] = err
        finally:
            stack.pop()

    def resolve(name: str, node: Node) -> Quantity:
        if name in overrides:
            return overrides[name]
        if name in var_qty:
            return var_qty[name]
        if name in var_err:
            raise wrap_inner(name, node)
        if name in variables:
            if name in stack:
                chain = " → ".join(stack + [name])
                raise EngineError(f"变量循环引用：{chain}", node.start, node.end, "var_cycle")
            evaluate_variable(name)
            if name in var_qty:
                return var_qty[name]
            raise wrap_inner(name, node)
        if name in CONSTANTS:
            value, d, _ = CONSTANTS[name]
            return Quantity(value, d)
        if name in UNITS:
            d, factor, _, _ = UNITS[name]
            return Quantity(factor, d)
        raise EngineError(f"未定义的变量或单位「{name}」", node.start, node.end, "undefined")

    return resolve, evaluate_variable


def _perturb_error(name: str, direction: str, exc: EngineError,
                   errors: list[AnalysisError]) -> None:
    """记录扰动重算失败：保留原错误（量纲/定义域等）与定位，标注扰动来源后立即中止。"""
    errors.append(AnalysisError(
        f"对输入「{name}」做 {direction} 侧扰动重算时失败：{exc.error.message}。"
        "不确定度分析中止，不返回合成不确定度。",
        exc.error.start, exc.error.end, exc.error.kind,
        source=exc.error.source,
        extra={"perturbed": name, "direction": direction},
    ))


def _uncertainty_analysis(expression: str, variables: dict[str, str],
                          var_ast: dict[str, Node], var_qty: dict[str, Quantity],
                          uncertainties: dict[str, str], main_ast: Node,
                          nominal: Quantity, errors: list[AnalysisError],
                          ) -> dict | None:
    """以各独立直接输入为扰动源，双侧中心差分求一阶灵敏度，RSS 合成标准不确定度。

    任一校验或扰动重算失败，都会向 errors 追加错误并返回 None，
    绝不返回貌似有效的不确定度数值。
    """
    # ---- 1. 校验每个不确定度输入 -------------------------------------------
    sources: list[dict] = []
    u_resolve = _unit_only_resolver(set(variables))
    for name in sorted(uncertainties):
        src = uncertainties[name]
        if name not in variables:
            errors.append(AnalysisError(
                f"不确定度对应的变量「{name}」不存在（请先在变量列表中定义它）",
                0, max(len(name), 1), "uncert_unknown", f"uncert:{name}",
            ))
            continue
        try:
            uq = _parse_and_eval(src, u_resolve)
        except EngineError as exc:
            exc.error.source = f"uncert:{name}"
            errors.append(exc.error)
            continue
        except OverflowError:
            errors.append(AnalysisError(
                f"变量「{name}」的标准不确定度数值溢出",
                0, len(src), "value", f"uncert:{name}",
            ))
            continue
        if not math.isfinite(uq.value):
            errors.append(AnalysisError(
                f"变量「{name}」的标准不确定度不是有限数值",
                0, len(src), "value", f"uncert:{name}",
            ))
            continue
        if uq.value < 0:
            errors.append(AnalysisError(
                f"变量「{name}」的标准不确定度不能为负数（实际为 {uq.value:g}）",
                0, len(src), "uncert_negative", f"uncert:{name}",
            ))
            continue
        if name in var_qty and uq.dim != var_qty[name].dim:
            errors.append(AnalysisError(
                f"变量「{name}」的标准不确定度量纲为「{dim_str(uq.dim)}」，"
                f"与该变量量纲「{dim_str(var_qty[name].dim)}」不一致",
                0, len(src), "uncert_dim", f"uncert:{name}",
            ))
            continue
        # 只有“直接输入”（定义式不引用任何用户变量）才能作为独立扰动源
        ast = var_ast.get(name)
        referenced = (_node_names(ast) if ast is not None else set()) & set(variables)
        if referenced:
            errors.append(AnalysisError(
                f"变量「{name}」引用了其他变量（{ '、'.join(sorted(referenced)) }），"
                "不是直接测量输入，不能单独填写标准不确定度；请为其引用的直接输入填写",
                0, max(len(variables[name]), 1), "uncert_indirect", f"uncert:{name}",
            ))
            continue
        sources.append({"name": name, "u_si": uq.value, "dim": uq.dim})

    if errors:
        return None

    # ---- 2. 零标称值也要有有效步长：相对步长 + 由不确定度/量级托底 ------------
    STEP_REL = 1e-6

    def step_for(src: dict) -> float:
        x0 = var_qty[src["name"]].value
        scale = max(abs(x0), src["u_si"])
        h = STEP_REL * scale
        if not math.isfinite(h) or h <= 0.0:
            h = STEP_REL
        return h

    # ---- 3. 逐源双侧扰动：完整变量链 + 主表达式全部重算 ----------------------
    contributions: list[dict] = []
    variance = 0.0
    for src in sources:
        name = src["name"]
        u_si = src["u_si"]
        x0 = var_qty[name].value
        h = step_for(src)

        def rerun(direction: str, xv: float) -> Quantity:
            q_overrides = {name: Quantity(xv, var_qty[name].dim)}
            qty: dict[str, Quantity] = {}
            err_map: dict[str, EngineError] = {}
            stk: list[str] = []
            resolver, _ = _make_var_resolver(
                variables, var_ast, qty, err_map, stk, q_overrides,
                propagate_inner=True)
            try:
                out = Evaluator(resolver).eval(main_ast)
            except OverflowError:
                raise EngineError("扰动重算数值溢出",
                                  0, len(expression), "uncert_eval") from None
            if not math.isfinite(out.value):
                raise EngineError("扰动重算结果不是有限数值",
                                  0, len(expression), "uncert_eval")
            if out.dim != nominal.dim:
                raise EngineError(
                    f"扰动后结果量纲变为「{dim_str(out.dim)}」，"
                    f"与标称量纲「{dim_str(nominal.dim)}」不一致",
                    0, len(expression), "uncert_eval")
            return out

        try:
            qp = rerun("+", x0 + h)
        except EngineError as exc:
            _perturb_error(name, "+", exc, errors)
            return None
        try:
            qm = rerun("-", x0 - h)
        except EngineError as exc:
            _perturb_error(name, "-", exc, errors)
            return None

        deriv = (qp.value - qm.value) / (2.0 * h)
        cu = abs(deriv) * u_si
        variance += cu * cu
        if u_si > 0.0:
            contributions.append({
                "name": name,
                "u_si": u_si,
                "step_si": h,
                "sensitivity": deriv,
                "contribution_si": cu,
            })

    # ---- 4. 一阶平方和（RSS）合成 -------------------------------------------
    uc = math.sqrt(variance)
    return {
        "method": "central_difference_rss",
        "step_relative": STEP_REL,
        "si_value": uc,
        "si_unit": si_unit_str(nominal.dim),
        "sources": contributions,
        "target": None,  # 由目标单位换算阶段填充
    }


def analyze(expression: str, variables: dict | None = None,
            target_unit: str | None = None,
            uncertainties: dict | None = None) -> dict:
    """分析表达式，返回可 JSON 序列化的结果字典。

    即使存在错误也会尽量返回已算出的部分；errors 中每项都带有
    source（expression / target / var:<名称> / uncert:<名称>）与 (start, end) 片段位置。
    uncertainties 为 {变量名: 带单位的标准不确定度}，仅可标注在直接输入变量上。
    """
    expression = expression or ""
    variables = {
        str(k).strip(): (v if isinstance(v, str) else str(v))
        for k, v in (variables or {}).items() if str(k).strip()
    }
    uncertainties = {
        str(k).strip(): (v if isinstance(v, str) else str(v)).strip()
        for k, v in (uncertainties or {}).items()
        if str(k).strip() and (v if isinstance(v, str) else str(v)).strip()
    }
    errors: list[AnalysisError] = []
    # 变量名与内置单位/常量同名时给出非致命警告（变量优先级更高）
    warnings = []
    for name in variables:
        if name in UNITS:
            warnings.append({"message": f"变量「{name}」与内置单位同名，所有表达式中的 {name} 将按变量解析",
                             "name": name, "kind": "shadow_unit"})
        elif name in CONSTANTS:
            warnings.append({"message": f"变量「{name}」与内置常量同名，所有表达式中的 {name} 将按变量解析",
                             "name": name, "kind": "shadow_constant"})

    # 预解析全部变量与主表达式（AST 可供扰动重算复用）
    var_ast: dict[str, Node] = {}
    for name, src in variables.items():
        try:
            var_ast[name] = parse_src(src)
        except EngineError as exc:
            exc.error.source = f"var:{name}"
            errors.append(exc.error)

    var_qty: dict[str, Quantity] = {}
    var_err: dict[str, EngineError] = {}
    stack: list[str] = []
    resolver, evaluate_variable = _make_var_resolver(
        variables, var_ast, var_qty, var_err, stack)

    # 先求值全部变量（即使未被引用，也能暴露变量自身的错误）
    for name in variables:
        if name not in var_ast:
            continue
        evaluate_variable(name)
        if name in var_err and var_err[name].error not in errors:
            errors.append(var_err[name].error)

    # 主表达式
    result: Quantity | None = None
    main_ast: Node | None = None
    try:
        main_ast = parse_src(expression)
    except EngineError as exc:
        exc.error.source = "expression"
        errors.append(exc.error)
    except OverflowError:
        err = EngineError("数值溢出", 0, len(expression), "value")
        err.error.source = "expression"
        errors.append(err.error)

    if main_ast is not None:
        try:
            result = Evaluator(resolver).eval(main_ast)
            if not math.isfinite(result.value):
                raise EngineError("计算结果不是有限数值", 0, len(expression), "value")
        except EngineError as exc:
            exc.error.source = "expression"
            errors.append(exc.error)
            result = None
        except OverflowError:
            err = EngineError("数值溢出", 0, len(expression), "value")
            err.error.source = "expression"
            errors.append(err.error)
            result = None

    # 不确定度分析：仅在标称计算完全成功时进行；失败则追加错误且不产出数值
    uncertainty_block = None
    if result is not None and not any(e.source == "expression" for e in errors) \
            and not any(e.source.startswith("var:") for e in errors) and uncertainties:
        uncertainty_block = _uncertainty_analysis(
            expression, variables, var_ast, var_qty, uncertainties,
            main_ast, result, errors)

    # 目标单位换算（标称值与不确定度一起换算）
    target = None
    if target_unit and target_unit.strip():
        try:
            tq = _parse_and_eval(target_unit.strip(), resolver)
            if tq.value == 0:
                raise EngineError("目标单位不能为零", 0, len(target_unit), "value")
            if result is not None:
                if tq.dim != result.dim:
                    raise EngineError(
                        f"目标单位量纲为「{dim_str(tq.dim)}」，与结果量纲「{dim_str(result.dim)}」不一致",
                        0, len(target_unit), "target_dim",
                    )
                target = {"unit": target_unit.strip(), "value": result.value / tq.value}
                if uncertainty_block is not None:
                    uncertainty_block["target"] = {
                        "unit": target_unit.strip(),
                        "value": uncertainty_block["si_value"] / tq.value,
                    }
        except EngineError as exc:
            exc.error.source = "target"
            errors.append(exc.error)

    response: dict = {
        "ok": not errors,
        "errors": [e.to_dict() for e in errors],
        "warnings": warnings,
        "result": None,
    }
    if result is not None:
        response["result"] = {
            "si_value": result.value,
            "dimension": [format_fraction(x) for x in result.dim],
            "dimension_names": DIM_NAMES,
            "dimension_str": dim_str(result.dim),
            "si_unit": si_unit_str(result.dim),
            "derived": derived_matches(result.dim),
            "target": target,
            "uncertainty": uncertainty_block,
        }
    return response


# ---------------------------------------------------------------------------

# 反求（inverse solve）：给定目标结果，二分反解一个直接输入变量
# ---------------------------------------------------------------------------

SOLVE_TOLERANCE = 1e-8
SOLVE_MAX_ITERATIONS = 80

# 形如「12.5 kJ」「-3e-2 m/s」：开头一个数，后面跟单位表达式（可全空白）
_VALUE_PREFIX_RE = re.compile(
    r"^\s*(" + _NUM_RE.pattern + r")(.*)$"
)


def _solve_error(message: str, start: int, end: int, kind: str,
                 source: str = "solve") -> AnalysisError:
    return AnalysisError(message, start, end, kind, source)


def _make_solve_unit_resolver(variable_names: set[str]):
    """解析带单位数值：允许内置常量与单位，禁止引用变量（用于反求的单位与目标输入）。"""
    def resolve(name: str, node: Node) -> Quantity:
        if name in CONSTANTS:
            value, d, _ = CONSTANTS[name]
            return Quantity(value, d)
        if name in UNITS:
            d, factor, _, _ = UNITS[name]
            return Quantity(factor, d)
        if name in variable_names:
            raise EngineError(
                f"此处不能引用变量「{name}」，请给出带单位的数值",
                node.start, node.end, "solve_ref",
            )
        raise EngineError(f"未定义的单位或常量「{name}」", node.start, node.end, "undefined")
    return resolve


def _split_value_unit(src: str):
    """拆出「开头数值 + 剩余单位表达式」。

    返回 (number, unit_src)；无法拆出有限数值时返回 None。
    用于回显实际结果在所给目标单位下的值（例如目标 "200 J"）。
    """
    m = _VALUE_PREFIX_RE.match(src)
    if not m:
        return None
    num_text, unit_src = m.group(1), m.group(2).strip()
    try:
        number = float(num_text)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number, unit_src


def solve_inverse(expression: str, variables: dict | None, solve_for: str,
                  solve_unit: str, lower: float, upper: float,
                  target: str) -> dict:
    """反求直接输入变量：覆盖该变量取值并重算完整变量链 + 主表达式，
    以 SI 残差二分，使主表达式达到给定目标。

    返回与 analyze 同构的 {ok, errors, warnings, result}；
    任何输入无效、区间不包根、中途求值失败或超限未收敛都不返回解。
    """
    expression = expression or ""
    variables = {
        str(k).strip(): (v if isinstance(v, str) else str(v))
        for k, v in (variables or {}).items() if str(k).strip()
    }
    solve_for = (solve_for or "").strip()
    solve_unit = (solve_unit or "").strip()
    target = target or ""
    errors: list[AnalysisError] = []
    warnings = []
    for name in variables:
        if name in UNITS:
            warnings.append({"message": f"变量「{name}」与内置单位同名，所有表达式中的 {name} 将按变量解析",
                             "name": name, "kind": "shadow_unit"})
        elif name in CONSTANTS:
            warnings.append({"message": f"变量「{name}」与内置常量同名，所有表达式中的 {name} 将按变量解析",
                             "name": name, "kind": "shadow_constant"})

    def fail(message: str, start: int = 0, end: int = 1, kind: str = "solve_error",
             source: str = "solve") -> dict:
        errors.append(_solve_error(message, start, max(end, start + 1), kind, source))
        return {"ok": False, "errors": [e.to_dict() for e in errors],
                "warnings": warnings, "result": None}

    # ---- 1. 结构预解析：全部变量定义式与主表达式（暴露语法/结构错误） --------
    var_ast: dict[str, Node] = {}
    for name, src in variables.items():
        try:
            var_ast[name] = parse_src(src)
        except EngineError as exc:
            exc.error.source = f"var:{name}"
            errors.append(exc.error)

    try:
        main_ast = parse_src(expression)
    except EngineError as exc:
        exc.error.source = "expression"
        errors.append(exc.error)
    except OverflowError:
        errors.append(_solve_error("数值溢出", 0, len(expression), "value", "expression"))

    # 结构错误（含待求变量自身的定义式语法错误）先统一报出，不继续语义校验
    if errors:
        return {"ok": False, "errors": [e.to_dict() for e in errors],
                "warnings": warnings, "result": None}

    # ---- 2. 待求变量检查：存在且为直接输入（定义式不引用其他用户变量） -------
    if not solve_for:
        return fail("未指定待求变量（solve_for 不能为空）", kind="solve_var")
    if solve_for not in variables:
        return fail(
            f"待求变量「{solve_for}」不存在（请先在变量列表中定义它）",
            0, len(solve_for), "solve_var",
        )
    referenced = _node_names(var_ast[solve_for]) & set(variables)
    if referenced:
        return fail(
            f"待求变量「{solve_for}」引用了其他变量（{'、'.join(sorted(referenced))}），"
            "不是直接输入变量；反求只能针对定义式不引用其他用户变量的直接输入",
            0, max(len(variables[solve_for]), 1), "solve_indirect",
        )

    # 待求变量自身的量纲：按其原始定义式独立求值（常量与单位，不涉及其他变量）
    unit_resolver = _make_solve_unit_resolver(set(variables))
    try:
        var_q = _parse_and_eval(variables[solve_for], unit_resolver)
    except EngineError as exc:
        exc.error.source = f"var:{solve_for}"
        return fail(exc.error.message, exc.error.start, exc.error.end,
                    exc.error.kind, exc.error.source)
    except OverflowError:
        return fail("待求变量的定义式数值溢出", 0, len(variables[solve_for]),
                    "value", f"var:{solve_for}")
    if not math.isfinite(var_q.value):
        return fail("待求变量的定义式结果不是有限数值",
                    0, len(variables[solve_for]), "value", f"var:{solve_for}")
    var_dim = var_q.dim

    # ---- 3. 变量单位：必须能解析、有限为正、量纲与变量一致 ------------------
    try:
        uq = _parse_and_eval(solve_unit, unit_resolver)
    except EngineError as exc:
        return fail(exc.error.message, exc.error.start, exc.error.end,
                    exc.error.kind, "solve_unit")
    except OverflowError:
        return fail("变量单位表达式数值溢出", 0, len(solve_unit), "value", "solve_unit")
    if not math.isfinite(uq.value) or uq.value <= 0.0:
        return fail(f"变量单位「{solve_unit}」不是有限为正的有效单位",
                    0, max(len(solve_unit), 1), "solve_unit_value", "solve_unit")
    if uq.dim != var_dim:
        return fail(
            f"变量单位量纲为「{dim_str(uq.dim)}」，与待求变量量纲「{dim_str(var_dim)}」不一致",
            0, max(len(solve_unit), 1), "solve_unit_dim", "solve_unit",
        )
    unit_factor = uq.value

    # ---- 4. 上下界：有限数且下界严格小于上界，换算到 SI ---------------------
    if not isinstance(lower, (int, float)) or isinstance(lower, bool) \
            or not math.isfinite(float(lower)):
        return fail("下界必须是有限数值", kind="solve_bounds")
    if not isinstance(upper, (int, float)) or isinstance(upper, bool) \
            or not math.isfinite(float(upper)):
        return fail("上界必须是有限数值", kind="solve_bounds")
    lower, upper = float(lower), float(upper)
    if not lower < upper:
        return fail(f"下界（{lower:g}）必须严格小于上界（{upper:g}）",
                    kind="solve_bounds")
    lo_si, hi_si = lower * unit_factor, upper * unit_factor

    # ---- 5. 目标结果：可求值、有限、量纲与主表达式一致 -----------------------
    try:
        tq = _parse_and_eval(target, unit_resolver)
    except EngineError as exc:
        return fail(exc.error.message, exc.error.start, exc.error.end,
                    exc.error.kind, "solve_target")
    except OverflowError:
        return fail("目标结果数值溢出", 0, len(target), "value", "solve_target")
    if not math.isfinite(tq.value):
        return fail("目标结果不是有限数值", 0, max(len(target), 1),
                    "value", "solve_target")
    target_si = tq.value

    # ---- 6. 试算闭包：以候选值覆盖待求变量，重算完整变量链 + 主表达式 -------
    def trial(xv: float) -> Quantity:
        overrides = {solve_for: Quantity(xv, var_dim)}
        qty: dict[str, Quantity] = {}
        err_map: dict[str, EngineError] = {}
        stk: list[str] = []
        resolver, _ = _make_var_resolver(
            variables, var_ast, qty, err_map, stk, overrides,
            propagate_inner=True)
        out = Evaluator(resolver).eval(main_ast)
        if not math.isfinite(out.value):
            raise EngineError("主表达式结果不是有限数值",
                              0, len(expression), "value")
        return out

    def abort() -> dict:
        return {"ok": False, "errors": [e.to_dict() for e in errors],
                "warnings": warnings, "result": None}

    def trial_context(side_label: str, xv: float,
                      iteration: int | None) -> dict:
        return {"solve_for": solve_for, "solve_at": side_label,
                "candidate_si": xv, "iteration": iteration}

    def trial_error(side_label: str, xv: float, iteration: int | None,
                    exc: EngineError) -> dict:
        """试算失败：保留内部错误的 kind/片段/source，附加反求试算上下文。"""
        where = (f"第 {iteration} 次二分" if iteration is not None
                 else f"区间{side_label}端")
        cand = xv / unit_factor
        errors.append(AnalysisError(
            f"反求试算在{where}（待求变量「{solve_for}」= {cand:g} {solve_unit}）"
            f"失败：{exc.error.message}。反求中止，不返回解。",
            exc.error.start, exc.error.end, exc.error.kind,
            source=exc.error.source,
            extra=trial_context(side_label, xv, iteration),
        ))
        return abort()

    def endpoint_failure(side_label: str, xv: float, exc: EngineError) -> dict:
        return trial_error(side_label, xv, None, exc)

    def dimension_change(side_label: str, iteration: int | None,
                         xv: float, got: tuple) -> dict:
        where = (f"第 {iteration} 次二分" if iteration is not None
                 else f"区间{side_label}端")
        errors.append(AnalysisError(
            f"反求试算在{where}后结果量纲变为「{dim_str(got)}」，"
            f"与主表达式量纲「{dim_str(result_dim)}」不一致。反求中止，不返回解。",
            0, len(expression), "solve_eval_failed", "expression",
            extra=trial_context(side_label, xv, iteration),
        ))
        return abort()

    def overflow_failure(side_label: str, iteration: int | None,
                         xv: float) -> dict:
        where = (f"第 {iteration} 次二分" if iteration is not None
                 else f"区间{side_label}端")
        errors.append(AnalysisError(
            f"反求试算在{where}数值溢出。反求中止，不返回解。",
            0, len(expression), "value", "expression",
            extra=trial_context(side_label, xv, iteration),
        ))
        return abort()

    try:
        q_lo = trial(lo_si)
    except EngineError as exc:
        return endpoint_failure("下", lo_si, exc)
    except OverflowError:
        return overflow_failure("下", None, lo_si)

    if tq.dim != q_lo.dim:
        return fail(
            f"目标量纲为「{dim_str(tq.dim)}」，与主表达式量纲「{dim_str(q_lo.dim)}」不一致",
            0, max(len(target), 1), "solve_target_dim", "solve_target")
    result_dim = q_lo.dim

    try:
        q_hi = trial(hi_si)
    except EngineError as exc:
        return endpoint_failure("上", hi_si, exc)
    except OverflowError:
        return overflow_failure("上", None, hi_si)
    if q_hi.dim != result_dim:
        return dimension_change("上", None, hi_si, q_hi.dim)

    tol = SOLVE_TOLERANCE * max(1.0, abs(target_si))
    f_lo, f_hi = q_lo.value - target_si, q_hi.value - target_si

    def converged(value: float) -> bool:
        return abs(value - target_si) <= tol

    def make_result(x_si: float, out: Quantity, iterations: int,
                    endpoint: str) -> dict:
        actual = {
            "si_value": out.value,
            "residual_si": out.value - target_si,
            "dimension": [format_fraction(x) for x in out.dim],
            "dimension_names": DIM_NAMES,
            "dimension_str": dim_str(out.dim),
            "si_unit": si_unit_str(out.dim),
            "derived": derived_matches(out.dim),
        }
        # 若目标写成「数值 + 单位」（如 "200 J"），回显实际结果在该单位下的值
        split = _split_value_unit(target)
        if split is not None:
            number, unit_src = split
            if unit_src and number != 0.0:
                try:
                    u = _parse_and_eval(unit_src, unit_resolver)
                except EngineError:
                    u = None
                if (u is not None and math.isfinite(u.value) and u.value > 0.0
                        and u.dim == out.dim):
                    expected = number * u.value
                    if math.isclose(expected, target_si, rel_tol=1e-12,
                                    abs_tol=1e-12 * max(1.0, abs(target_si))):
                        actual["in_given_unit"] = {
                            "unit": unit_src, "value": out.value / u.value}
        target_block = {
            "given": target,
            "si_value": target_si,
            "variable_unit": solve_unit,
        }
        return {
            "ok": True,
            "errors": [],
            "warnings": warnings,
            "result": {
                "variable": solve_for,
                "variable_si_value": x_si,
                "value": x_si / unit_factor,
                "unit": solve_unit,
                "bounds_given": {"lower": lower, "upper": upper, "unit": solve_unit},
                "target": target_block,
                "actual": actual,
                "iterations": iterations,
                "endpoint": endpoint,
                "tolerance_si": tol,
                "method": "bisection",
            },
        }

    # ---- 7. 端点已达目标直接返回 -------------------------------------------
    if converged(q_lo.value):
        return make_result(lo_si, q_lo, 0, "lower")
    if converged(q_hi.value):
        return make_result(hi_si, q_hi, 0, "upper")

    # ---- 8. 仅当两端残差异号才二分 ------------------------------------------
    # 端点残差恰为 0 时必已被上面的端点收敛判定命中，故此处只需判同号
    if f_lo * f_hi > 0.0:
        return fail(
            f"区间两端残差同号（下端 {f_lo:g}，上端 {f_hi:g}，SI），"
            "区间不包含使主表达式达到目标的解；请放宽或调整上下界",
            0, max(len(solve_unit), 1), "solve_no_bracket",
        )
    if f_lo == 0.0 or f_hi == 0.0:  # 理论不可达（已被端点收敛覆盖）
        return fail("端点残差为零但未通过收敛判定，请检查输入是否含非有限数值",
                    kind="solve_error")

    a, b, f_a = lo_si, hi_si, f_lo
    mid = 0.5 * (a + b)
    q_mid: Quantity | None = None
    for iterations in range(1, SOLVE_MAX_ITERATIONS + 1):
        try:
            q_mid = trial(mid)
        except EngineError as exc:
            return trial_error("中间", mid, iterations, exc)
        except OverflowError:
            return overflow_failure("中间", iterations, mid)
        if q_mid.dim != result_dim:
            return dimension_change("中间", iterations, mid, q_mid.dim)
        if converged(q_mid.value):
            return make_result(mid, q_mid, iterations, "")
        f_mid = q_mid.value - target_si
        if f_a * f_mid <= 0.0:
            b, f_hi = mid, f_mid
        else:
            a, f_a = mid, f_mid
        mid = 0.5 * (a + b)

    # ---- 9. 超限未收敛：不返回解 -------------------------------------------
    return fail(
        f"二分 {SOLVE_MAX_ITERATIONS} 次后仍未达到收敛容差（SI 残差须 ≤ {tol:g}）",
        0, max(len(solve_unit), 1), "solve_max_iter",
    )
