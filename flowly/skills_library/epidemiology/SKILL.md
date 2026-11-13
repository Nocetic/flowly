---
name: epidemiology
description: "Model infectious-disease outbreaks — run SIR and SEIR compartmental models, find the epidemic peak height and timing and the final attack rate, compute the basic reproduction number R0 and the herd-immunity threshold, and convert between growth rate, doubling time, and case counts. Includes a stdlib model runner (RK4 integration). Use when the user asks about an SIR/SEIR curve, R0 or Rt, herd immunity, flattening the curve, epidemic peak or final size, or the doubling time of case counts. Informational modeling, not clinical or public-health advice."
metadata: {"flowly":{"emoji":"🦠","tags":["science","epidemiology","sir","seir","r0","outbreak","modeling","public-health","doubling-time"],"requires":{"bins":["python3"]},"category":"science","related_skills":["statistical-analysis","clinical-evidence","symbolic-math","data-visualization","research-methods"]}}
---

# Epidemiology — Compartments, R₀, and the Shape of the Curve

An outbreak in a closed, well-mixed population is governed by two numbers per
compartment transition: how fast people **infect** (β) and how fast they
**recover** (γ). Their ratio is **R₀ = β/γ** — the average secondary cases from
one case in a fully susceptible population. R₀ > 1 means growth; R₀ < 1 means
fade-out; and 1 − 1/R₀ is the fraction that must be immune to stop it. The
`epi.py` helper integrates the SIR/SEIR equations so the peak and final size are
computed, not eyeballed.

## What this skill produces

**Chat-first.** Default: the answer the model gives — R₀ and what it implies, the
peak height and day, the final attack rate, or the doubling time — with the one
assumption that drives it stated plainly. Offer a plotted curve
(`data-visualization`) for a fuller picture.

## When to use

- "Run an SIR/SEIR model with these parameters." / "When does it peak? How high?"
- "What's R₀? What's the herd-immunity threshold?"
- "What final fraction gets infected?" / "What does flattening the curve do?"
- "Cases went from X to Y in N days — what's the doubling time / growth rate?"
- "If R₀ = 2.5, what % needs to be immune?"

## The model, in one breath

- **SIR:** Susceptible → Infectious → Recovered. `dS=−βSI/N`, `dI=βSI/N−γI`,
  `dR=γI`. Peak infection when S/N falls to 1/R₀.
- **SEIR:** adds an **Exposed** (latent, not yet infectious) stage with rate σ;
  the mean latent period is 1/σ. It delays and slightly flattens the peak.
- **R₀ = β/γ**; infectious period ≈ 1/γ. **Herd immunity threshold = 1 − 1/R₀.**
- **Final size** solves Z = 1 − e^(−R₀·Z): even without intervention, not everyone
  is infected — the epidemic burns out as susceptibles run low.

## The calculator

`scripts/epi.py` (stdlib; RK4 integrator):
```bash
python3 scripts/epi.py sir  --beta 0.4 --gamma 0.1 --N 1000000 --I0 10 --days 160
python3 scripts/epi.py seir --beta 0.6 --sigma 0.2 --gamma 0.1 --N 1000000 --days 200
python3 scripts/epi.py r0   --beta 0.4 --gamma 0.1        # or --r0 2.5, or --doubling 3 --gamma 0.1
python3 scripts/epi.py doubling --rate 0.23              # per-day growth → doubling time
python3 scripts/epi.py doubling --c1 100 --c2 800 --days 6
```

## Chat output format

```
**SIR model** — β=0.4, γ=0.1, N=1,000,000, I₀=10, 160 days

R₀ = β/γ = 4.00   (infectious period ≈ 10 days)
Peak infected ≈ 168,000 on day 46 (16.8% infected at once)
Final size (attack rate) ≈ 98.0%
Herd-immunity threshold = 1 − 1/R₀ = 75.0% immune to halt growth
```

## Workflow

1. **Nail the parameters.** Get β and γ, or derive them: γ = 1/infectious-period,
   and β = R₀·γ if the user gives R₀. Confirm N and the seed I₀.
2. **Run** `epi.py sir`/`seir`; read peak height, peak day, final size, R₀.
3. **Translate the numbers** into meaning: "16% infected at once" → hospital load;
   "herd threshold 75%" → vaccination target; lowering β (distancing) lowers and
   delays the peak without changing the final size much unless it pushes R₀<1.
4. **For raw case data**, use `doubling` to get the growth rate/doubling time, then
   back out an apparent R from `r0 --doubling … --gamma …`.
5. **Caveat honestly** (see pitfalls) and route out: curve plots →
   `data-visualization`; fitting parameters to real data → `statistical-analysis`;
   study design / bias → `research-methods`; evidence appraisal → `clinical-evidence`.

## Key pitfalls

- **Treating the model as a forecast.** It's deterministic, well-mixed, and closed
  — no age structure, behavior change, seasonality, or stochastic die-out. Use it
  for intuition and scenarios, and say so; don't present it as a prediction.
- **R₀ vs Rₜ.** R₀ assumes everyone susceptible. Once immunity builds (or behavior
  changes), the *effective* Rₜ = R₀·(S/N) is what matters; growth stops at Rₜ=1.
- **Units of β and γ must match the time step** (per day here). Mixing per-day and
  per-week silently corrupts R₀.
- **Final size ≠ 100%.** Even unmitigated, the epidemic ends with a susceptible
  remnant (the final-size equation) — don't claim "everyone gets infected."
- **Flattening ≠ shrinking.** Lowering β mainly delays and lowers the *peak*; the
  cumulative total barely moves unless the intervention drives R₀ below 1.
- **Not medical advice.** This is modeling for understanding, not a basis for
  clinical or policy decisions.

## Quick reference

- SIR: dS=−βSI/N, dI=βSI/N−γI, dR=γI. R₀=β/γ, infectious period≈1/γ.
- Herd threshold = 1−1/R₀. Peak when S/N=1/R₀. Final size solves Z=1−e^(−R₀Z).
- SEIR adds latent stage (rate σ, period 1/σ) — delays/flattens the peak.
- `epi.py sir|seir|r0|doubling`. Doubling time = ln2/r.
- Deterministic teaching model, not a forecast. Rₜ=R₀·S/N once immunity builds.
