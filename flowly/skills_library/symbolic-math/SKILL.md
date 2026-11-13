---
name: symbolic-math
description: "Do exact symbolic mathematics — differentiate, integrate (indefinite and definite), solve equations and systems, simplify/factor, evaluate limits, expand Taylor series, and linear algebra (determinant, inverse, eigenvalues, rank, solve Ax=b), plus ordinary differential equations. Includes a SymPy calculator so results are exact, not floating-point guesses. Use when the user asks for a derivative or integral, to solve an equation symbolically, simplify or factor an expression, take a limit, find a Taylor/Maclaurin series, do matrix algebra, or solve an ODE."
metadata: {"flowly":{"emoji":"🧮","tags":["science","math","calculus","algebra","linear-algebra","symbolic","sympy","derivative","integral","ode"],"requires":{"bins":["python3"]},"category":"science","related_skills":["physics-solver","statistical-analysis","signal-processing","control-systems","engineering-units"]}}
---

# Symbolic Math — Exact Answers, Not Floating-Point Guesses

Calculus and algebra reward *exact* work: an integral is `π/4`, not `0.7853…`; a
root is `(5 ± √13)/2`, not a decimal. Reason symbolically first, then hand a
decimal only when the user wants a number. The `symbolic.py` helper (SymPy) does
the manipulation exactly so you never fat-finger a chain rule or a sign.

## What this skill produces

**Chat-first.** Default: the exact result with the key step shown — the
antiderivative, the factored roots, the simplified form — and a decimal only when
it helps. `symbolic.py` guarantees the algebra is right; your job is to set the
problem up correctly and explain the step that matters.

## When to use

- "Differentiate / integrate \<expression\>." (indefinite **or** definite)
- "Solve \<equation\> for x." / "Solve this system."
- "Simplify / factor / expand \<expression\>."
- "What's the limit of \<expr\> as x→…?"
- "Taylor/Maclaurin series of \<expr\> to n terms."
- "Determinant / inverse / eigenvalues of this matrix." / "Solve Ax=b."
- "Solve this differential equation."

## The calculator

`scripts/symbolic.py` needs SymPy (`pip install sympy` — the script prints this
and exits cleanly if it's missing). Expressions use Python syntax: `**` for
powers, `sin`/`cos`/`exp`/`log`/`sqrt`, `pi`, `oo` for ∞.

```bash
python3 scripts/symbolic.py diff "x**2*sin(x)" --var x --order 1
python3 scripts/symbolic.py integrate "1/(1+x**2)" --var x            # indefinite
python3 scripts/symbolic.py integrate "x**2" --var x --from 0 --to 1  # definite
python3 scripts/symbolic.py solve "x**2 - 5*x + 6" --var x            # expr = 0
python3 scripts/symbolic.py solve "2*x + y = 3" --var x,y             # one eq, solve for x in terms of y
python3 scripts/symbolic.py simplify "sin(x)**2 + cos(x)**2"
python3 scripts/symbolic.py limit "sin(x)/x" --var x --to 0
python3 scripts/symbolic.py series "exp(x)" --var x --at 0 --n 6
python3 scripts/symbolic.py matrix eig "[[2,0],[0,3]]"                # det|inv|eig|rank
python3 scripts/symbolic.py linsolve "[[2,1],[1,3]]" "[3,5]"          # A x = b
python3 scripts/symbolic.py ode "f(x).diff(x,2) + f(x)" --func f --var x
```

## Chat output format

```
**Definite integral**  ∫₀¹ x² dx

∫ x² dx = x³/3, evaluated 0→1 = 1/3 ≈ 0.3333
```

Show the antiderivative (or the factored form, or the pivotal algebra step), then
the exact answer, then a decimal only if asked or if it aids intuition.

## Workflow

1. **Restate** the expression in unambiguous form; confirm the variable and, for
   definite integrals/limits, the bounds/point.
2. **Compute exactly** with `symbolic.py`; keep π, √, e symbolic.
3. **Show the one step that matters** (the substitution, the factoring, the rule),
   not every mechanical line.
4. **Give the exact answer** with `+ C` on indefinite integrals; add a decimal via
   `sp.N`/`--from/--to` only when a number is the point.
5. **Sanity-check:** differentiate your integral back; plug a root into the
   equation; check units if the symbols are physical.
6. **Route out:** a physics word-problem → `physics-solver`; data fitting/stats →
   `statistical-analysis`; transfer functions/stability → `control-systems`;
   spectra/transforms → `signal-processing`; unit conversion → `engineering-units`.

## Key pitfalls

- **Dropping `+ C`** on indefinite integrals — it's part of the answer.
- **Decimalizing too early.** `π/4` is the answer; `0.785` is a rounding of it.
  Keep it exact until a number is explicitly wanted.
- **Ambiguous notation.** `1/1+x` is `(1/1)+x`; write `1/(1+x)`. `e^x` is `exp(x)`;
  `ln` is `log`. Confirm before computing.
- **Wrong branch / domain.** `sqrt(x**2)` is `|x|`, `solve` may return complex
  roots, and limits can differ by direction (`--dir + / -`). State assumptions.
- **Singular matrices.** No inverse and no unique `Ax=b` solution when det = 0 —
  report the solution set, don't invent one.
- **Forgetting to verify.** Differentiating the antiderivative or back-substituting
  a root is a five-second check that catches most setup errors.

## Quick reference

- Exact first, decimal last. `+ C` on indefinite integrals.
- `symbolic.py diff|integrate|solve|simplify|limit|series|matrix|linsolve|ode`.
- Syntax: `**` powers, `exp/log/sqrt`, `pi`, `oo`=∞. Verify by differentiating back.
- SymPy needed (`pip install sympy`); det=0 ⇒ no inverse / non-unique solve.
- Physics → physics-solver · stats → statistical-analysis · control → control-systems.
