---
name: uncertainty-propagation
description: "Propagate measurement uncertainty through a calculation — given a formula and each input as value ± uncertainty, compute the result's uncertainty two ways (first-order partial-derivative / GUM method and Monte Carlo), rank which input dominates the error budget, format a measurement to correct significant figures, and take the inverse-variance-weighted mean of repeated measurements. Includes a stdlib calculator. Use when the user asks how errors combine or propagate, what the uncertainty on a computed quantity is, how many significant figures to report, or how to average measurements that each have their own error."
metadata: {"flowly":{"emoji":"📏","tags":["science","uncertainty","error-propagation","significant-figures","metrology","measurement","gum","monte-carlo"],"requires":{"bins":["python3"]},"category":"science","related_skills":["statistical-analysis","physics-solver","chemistry","lab-notebook","engineering-units"]}}
---

# Uncertainty Propagation — Every Measured Number Carries an Error

A measurement without an uncertainty is half a number. When you compute with
measured quantities, the errors combine — and *how* they combine depends on the
operation: absolute errors add in quadrature for sums, **relative** errors add in
quadrature for products and quotients. The general rule is the first-order
(GUM) formula: **u_f² = Σ (∂f/∂xᵢ · uᵢ)²** for independent inputs. The `unc.py`
helper evaluates that exactly (numerical partials) and cross-checks it with a
Monte Carlo simulation, so nonlinear cases don't fool you.

## What this skill produces

**Chat-first.** Default: the result as **value ± uncertainty**, rounded to honest
significant figures, plus which input dominates the error budget (so the user
knows what to measure better). The dual analytic + Monte Carlo check flags when a
formula is too nonlinear for the simple rule.

## When to use

- "I measured V = 2.00 ± 0.05 and I = 0.50 ± 0.01 — what's P = VI ± ?"
- "How do errors propagate through this formula?"
- "How many significant figures should I report for 9.8124 ± 0.032?"
- "How do I combine / average these measurements with different errors?"
- "Which input contributes most to my result's uncertainty?"

## The rules in one place

- **Add/subtract:** absolute uncertainties add in quadrature.
  u = √(u_a² + u_b²).
- **Multiply/divide:** *relative* uncertainties add in quadrature.
  (u_f/f)² = (u_a/a)² + (u_b/b)².
- **Power xⁿ:** relative uncertainty scales by |n|:  u_f/f = |n|·(u_x/x).
- **General f(x₁…):** u_f² = Σ (∂f/∂xᵢ · uᵢ)²  (independent inputs).
- **Reporting:** uncertainty to **1 sig fig** (2 if it leads with 1–2); round the
  value to the *same* decimal place. Never quote the value more precisely than its
  error.

## The calculator

`scripts/unc.py` (stdlib; formula uses Python syntax + `math` names):
```bash
python3 scripts/unc.py propagate "V*I" --var V=2.00:0.05 --var I=0.50:0.01
python3 scripts/unc.py propagate "0.5*m*v**2" --var m=2.0:0.1 --var v=3.00:0.05
python3 scripts/unc.py sigfig --value 9.8124 --unc 0.032
python3 scripts/unc.py weighted "9.81:0.02,9.78:0.05,9.83:0.03"
```
Each `--var` is `name=value:uncertainty`. Monte Carlo is seeded, so results are
reproducible.

## Chat output format

```
**Uncertainty propagation** — V*I

Result = 1.00 ± 0.03 W        (2.6% relative)
  analytic (1st-order): σ = 0.0269
  Monte Carlo (n=100k):  σ = 0.0269   (agree → linear regime)

Error budget: V 82%, I 18%  → tighten V to shrink the result's error.
```

## Workflow

1. **Collect each input as value ± standard uncertainty** (1σ). If given a range
   or "±" that means 95%/2σ, halve it to 1σ first, and state that.
2. **Propagate** with `unc.py propagate`; read the result, the analytic σ, and the
   Monte Carlo σ.
3. **Check agreement.** If analytic and MC differ by >~15%, the formula is
   nonlinear over these errors — report the Monte Carlo value and say why.
4. **Round honestly** with the sig-fig rule (or `unc.py sigfig`); attach units.
5. **Read the error budget** aloud: name the dominant input so the user knows the
   one measurement worth improving.
6. **Route out:** distributions, hypothesis tests, regression → `statistical-analysis`;
   the underlying physics → `physics-solver`; unit conversions and dimensional
   checks → `engineering-units`; recording it → `lab-notebook`.

## Key pitfalls

- **Adding absolute errors linearly.** Independent errors add in **quadrature**,
  not by simple sum (that overstates the error); only correlated worst-case
  stack-ups add linearly.
- **Absolute vs relative confusion.** Sums use absolute; products/quotients use
  relative. Mixing them is the most common propagation mistake.
- **Over-precise reporting.** "12.34567 ± 0.1" is nonsense — the error kills every
  digit past the first decimal. Round the value to the uncertainty.
- **Assuming independence when inputs are correlated.** Shared systematic errors
  (same miscalibrated instrument) don't cancel and need a covariance term; this
  tool assumes independence — flag it if that's wrong.
- **1σ vs 2σ mismatch.** Don't mix standard uncertainties with 95% confidence
  half-widths; convert everything to 1σ first.
- **Nonlinearity.** Near a zero, a divide, or a steep curve, the first-order
  formula underestimates — that's exactly when the Monte Carlo cross-check earns
  its keep.

## Quick reference

- Sums: absolute errors in quadrature. Products: relative errors in quadrature.
- General: u_f² = Σ(∂f/∂xᵢ·uᵢ)². Power xⁿ → relative error ×|n|.
- Report: uncertainty 1 sig fig (2 if lead 1–2); value to the same place.
- `unc.py propagate|sigfig|weighted`. Analytic vs MC disagree >15% ⇒ trust MC.
- Weighted mean = inverse-variance; assumes independent inputs.
