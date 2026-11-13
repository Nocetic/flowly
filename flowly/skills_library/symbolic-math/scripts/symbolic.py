#!/usr/bin/env python3
"""Symbolic math — calculus and linear algebra via SymPy.

Differentiate, integrate (indefinite/definite), solve equations, simplify,
limits, Taylor series, matrix ops (det/inv/eig/linear solve), and ODEs.
Exact symbolic results; chat-ready markdown.

SymPy is not stdlib. If it's missing this prints the one-line install and exits
cleanly, so the calling agent can surface it instead of crashing.

Usage:
    symbolic.py diff "x**2*sin(x)" --var x [--order 2]
    symbolic.py integrate "1/(1+x**2)" --var x            # indefinite
    symbolic.py integrate "x**2" --var x --from 0 --to 1  # definite
    symbolic.py solve "x**2 - 5*x + 6" --var x            # expr = 0 (or use '=')
    symbolic.py simplify "sin(x)**2 + cos(x)**2"
    symbolic.py limit "sin(x)/x" --var x --to 0 [--dir +|-]
    symbolic.py series "exp(x)" --var x --at 0 --n 6
    symbolic.py matrix det "[[1,2],[3,4]]"                # det|inv|eig|rank
    symbolic.py linsolve "[[2,1],[1,3]]" "[3,5]"          # A x = b
    symbolic.py ode "f(x).diff(x,2) + f(x)" --func f --var x
"""
from __future__ import annotations

import argparse
import ast
import sys

try:
    import sympy as sp
except ModuleNotFoundError:
    sys.exit("SymPy is required for symbolic math. Install it with:\n"
             "    pip install sympy")


def _pretty(expr) -> str:
    """Unicode pretty-print for chat, fenced so it survives markdown."""
    return sp.pretty(expr, use_unicode=True)


def _parse(expr_str: str):
    return sp.sympify(expr_str)


def cmd_diff(a):
    x = sp.Symbol(a.var)
    expr = _parse(a.expr)
    d = sp.diff(expr, x, a.order)
    ds = sp.simplify(d)
    ord_txt = f"d^{a.order}/d{a.var}^{a.order}" if a.order > 1 else f"d/d{a.var}"
    print(f"**Derivative** ({ord_txt})\n")
    print(f"{a.expr}  →  {ds}")
    if ds != d:
        print(f"\n(unsimplified: {d})")


def cmd_integrate(a):
    x = sp.Symbol(a.var)
    expr = _parse(a.expr)
    if a.frm is not None and a.to is not None:
        lo, hi = _parse(a.frm), _parse(a.to)
        val = sp.integrate(expr, (x, lo, hi))
        print(f"**Definite integral**  ∫ from {a.frm} to {a.to}\n")
        print(f"∫ ({a.expr}) d{a.var} = {sp.simplify(val)}")
        num = sp.N(val)
        if num.is_real:
            print(f"≈ {float(num):.6g}")
    else:
        val = sp.integrate(expr, x)
        print("**Indefinite integral**\n")
        print(f"∫ ({a.expr}) d{a.var} = {val} + C")


def _approx(expr) -> str:
    """' ≈ <decimal>' for a real scalar, else '' (tuples/parametric skip it)."""
    try:
        val = sp.N(expr)
        if getattr(val, "is_real", False):
            return f"  ≈ {float(val):.6g}"
    except (TypeError, AttributeError):
        pass
    return ""


def cmd_solve(a):
    expr_str = a.expr
    if "=" in expr_str:
        lhs, rhs = expr_str.split("=", 1)
        eq = sp.Eq(_parse(lhs), _parse(rhs))
    else:
        eq = _parse(expr_str)  # solved == 0
    syms = [sp.Symbol(v) for v in a.var.split(",")] if a.var else None
    sols = sp.solve(eq, syms) if syms else sp.solve(eq)
    print(f"**Solve**  {expr_str} {'' if '=' in expr_str else '= 0'}\n")
    if not sols:
        print("No closed-form solution found.")
        return
    if isinstance(sols, dict):
        for k, v in sols.items():
            print(f"{k} = {v}{_approx(v)}")
        return
    for s in sols:
        if isinstance(s, (tuple, sp.Tuple)) and syms:
            print(", ".join(f"{sym} = {val}" for sym, val in zip(syms, s)))
        else:
            label = syms[0] if syms else "root"
            print(f"{label} = {s}{_approx(s)}")


def cmd_simplify(a):
    expr = _parse(a.expr)
    s = sp.simplify(expr)
    print("**Simplify**\n")
    print(f"{a.expr}  →  {s}")
    fac = sp.factor(s)
    if fac != s:
        print(f"factored: {fac}")


def cmd_limit(a):
    x = sp.Symbol(a.var)
    expr = _parse(a.expr)
    to = sp.oo if a.to in ("oo", "inf", "+oo") else (-sp.oo if a.to in ("-oo", "-inf") else _parse(a.to))
    lim = sp.limit(expr, x, to, dir=a.dir)
    print(f"**Limit**  {a.var} → {a.to}" + (f" ({a.dir})" if a.dir != '+' else "") + "\n")
    print(f"lim ({a.expr}) = {lim}")


def cmd_series(a):
    x = sp.Symbol(a.var)
    expr = _parse(a.expr)
    at = _parse(a.at)
    s = sp.series(expr, x, at, a.n)
    print(f"**Taylor series**  about {a.var}={a.at}, to O({a.var}^{a.n})\n")
    print(f"{s}")


def _matrix(text):
    return sp.Matrix(ast.literal_eval(text))


def cmd_matrix(a):
    M = _matrix(a.data)
    print(f"**Matrix {a.op}**  ({M.rows}×{M.cols})\n")
    print("```\n" + _pretty(M) + "\n```")
    if a.op == "det":
        print(f"\ndet = {M.det()}")
    elif a.op == "inv":
        if M.det() == 0:
            print("\nSingular (det = 0) — no inverse.")
        else:
            print("\ninverse =\n```\n" + _pretty(M.inv()) + "\n```")
    elif a.op == "rank":
        print(f"\nrank = {M.rank()}")
    elif a.op == "eig":
        print("\neigenvalues (value: multiplicity):")
        for val, mult in M.eigenvals().items():
            approx = ""
            if not val.free_symbols:
                nv = complex(sp.N(val))
                approx = (f"  ≈ {nv.real:.4g}" if abs(nv.imag) < 1e-9
                          else f"  ≈ {nv.real:.4g}{nv.imag:+.4g}j")
            print(f"  {val}  ×{mult}{approx}")


def cmd_linsolve(a):
    A = _matrix(a.matrix)
    b = sp.Matrix(ast.literal_eval(a.rhs))
    print(f"**Linear solve**  A x = b  ({A.rows}×{A.cols})\n")
    if A.det() == 0:
        sols = sp.linsolve((A, b))
        print(f"det(A) = 0 — not a unique solution. Solution set: {sols}")
        return
    x = A.LUsolve(b)
    for i in range(x.rows):
        xi = x[i]
        print(f"x{i+1} = {sp.simplify(xi)}"
              + (f"  ≈ {float(sp.N(xi)):.6g}" if getattr(sp.N(xi), 'is_real', False) else ""))


def cmd_ode(a):
    x = sp.Symbol(a.var)
    f = sp.Function(a.func)
    expr = eval(a.expr, {a.func: f, a.var: x, "sp": sp})  # noqa: S307 trusted CLI input
    eq = sp.Eq(expr, 0)
    sol = sp.dsolve(eq, f(x))
    print(f"**ODE**  {a.expr} = 0\n")
    print(f"{sol}")
    print("\n(C1, C2… are integration constants set by initial/boundary conditions.)")


def main():
    ap = argparse.ArgumentParser(description="Symbolic math (SymPy)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("diff"); p.add_argument("expr"); p.add_argument("--var", default="x"); p.add_argument("--order", type=int, default=1); p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("integrate"); p.add_argument("expr"); p.add_argument("--var", default="x"); p.add_argument("--from", dest="frm"); p.add_argument("--to"); p.set_defaults(fn=cmd_integrate)
    p = sub.add_parser("solve"); p.add_argument("expr"); p.add_argument("--var", default=""); p.set_defaults(fn=cmd_solve)
    p = sub.add_parser("simplify"); p.add_argument("expr"); p.set_defaults(fn=cmd_simplify)
    p = sub.add_parser("limit"); p.add_argument("expr"); p.add_argument("--var", default="x"); p.add_argument("--to", required=True); p.add_argument("--dir", default="+", choices=["+", "-", "+-"]); p.set_defaults(fn=cmd_limit)
    p = sub.add_parser("series"); p.add_argument("expr"); p.add_argument("--var", default="x"); p.add_argument("--at", default="0"); p.add_argument("--n", type=int, default=6); p.set_defaults(fn=cmd_series)
    p = sub.add_parser("matrix"); p.add_argument("op", choices=["det", "inv", "eig", "rank"]); p.add_argument("data"); p.set_defaults(fn=cmd_matrix)
    p = sub.add_parser("linsolve"); p.add_argument("matrix"); p.add_argument("rhs"); p.set_defaults(fn=cmd_linsolve)
    p = sub.add_parser("ode"); p.add_argument("expr"); p.add_argument("--func", default="f"); p.add_argument("--var", default="x"); p.set_defaults(fn=cmd_ode)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
