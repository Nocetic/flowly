---
name: thermodynamics
description: "Solve thermodynamics and heat-transfer problems — the laws, ideal-gas relations, heat-engine and refrigeration/heat-pump cycles (Carnot efficiency, COP), conduction (Fourier), convection (Newton), radiation (Stefan-Boltzmann), thermal-resistance networks, and heat-sink/temperature-rise sizing. Includes a stdlib calculator. Use when the user asks about efficiency, COP, heat transfer, thermal resistance, a heat sink, temperature rise, an ideal-gas state, or a thermodynamic cycle."
metadata: {"flowly":{"emoji":"🔥","tags":["engineering","thermodynamics","heat-transfer","carnot","cop","conduction","convection","thermal"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["fluid-mechanics","mechanical-engineering","engineering-units","circuit-analysis"]}}
---

# Thermodynamics & Heat Transfer — Energy, Efficiency, and Where the Heat Goes

Two questions cover most practical thermo: **how efficient can this be?** (cycles, the second law) and **how does heat move and how hot does it get?** (the three transfer modes + thermal resistance). The discipline is rigorous unit-keeping (Kelvin for any ratio/radiation!), tracking the system boundary, and respecting the second law — no cycle beats Carnot.

## What this skill produces

**Chat-first.** Default: the worked answer — formula, numbers in consistent units, the result, and a sanity check (e.g. "efficiency below the Carnot limit ✅"). The `thermo.py` helper does the standard formulas. Offer a fuller writeup for multi-stage systems.

## When to use

- "What's the efficiency / COP of this engine / fridge / heat pump?"
- "How much heat flows through this wall / material?" (conduction)
- "Temperature rise of this component / heat sink?" / "Do I need a heat sink?"
- "Ideal gas: find P/V/T/n." / "Compress this gas — what's the new temperature?"
- "Thermal resistance of this stack." / "Radiated heat from a surface?"
- "Explain entropy / the second law / a Carnot cycle."

## The laws (the rules you can't cheat)

- **First law (energy conservation):** ΔU = Q − W. Energy in as heat, out as work; the internal energy change is the balance. Track signs and the system boundary.
- **Second law:** heat flows hot→cold spontaneously; you can't convert all heat to work; **entropy of an isolated system never decreases.** This caps every engine.
- **Carnot limit:** the *maximum* efficiency between two reservoirs is **η_Carnot = 1 − T_cold/T_hot** (temperatures in **Kelvin**). Any claimed real efficiency above this is wrong. Real engines hit a fraction of it.

## Cycles, efficiency & COP

- **Heat engine efficiency:** η = W_net/Q_in = 1 − Q_out/Q_in ≤ η_Carnot.
- **Refrigerator COP** = Q_cold/W_in; **heat pump COP** = Q_hot/W_in (= fridge COP + 1). COP > 1 is normal (you move more heat than the work you put in). Carnot limits: COP_fridge ≤ T_c/(T_h−T_c), COP_HP ≤ T_h/(T_h−T_c).
- Always compare a real number to the Carnot ceiling as a sanity check.

## Ideal gas

- **PV = nRT** (R = 8.314 J/mol·K), all in SI; or PV = mRT with specific R. Solve for any one given the others.
- **Process relations:** isothermal (PV const), adiabatic (PV^γ const, TV^(γ−1) const), isobaric, isochoric. Compression heats a gas (adiabatic): T₂ = T₁(P₂/P₁)^((γ−1)/γ).
- γ (c_p/c_v) ≈ 1.4 for air/diatomic, 1.67 monatomic.

## Heat transfer — the three modes

| Mode | Law | Formula | Driver |
|---|---|---|---|
| **Conduction** | Fourier | Q = k·A·ΔT / L | through solids; k = conductivity |
| **Convection** | Newton's cooling | Q = h·A·ΔT | surface↔fluid; h = coefficient |
| **Radiation** | Stefan-Boltzmann | Q = ε·σ·A·(T₁⁴−T₂⁴) | EM; **T in Kelvin**, σ=5.67e-8 |

- **Thermal resistance** turns these into a circuit (exactly like Ohm's law — see `circuit-analysis`): **R_th = ΔT/Q**. Conduction R = L/(kA); convection R = 1/(hA). **Resistances in series add** (e.g. junction→case→heatsink→air); parallel paths combine reciprocally. Total: ΔT = Q·ΣR_th.
- **Heat-sink / component sizing:** T_junction = T_ambient + P·(R_jc + R_cs + R_sa). Solve for the R_sa (sink-to-air) you need given a max junction temp — the core electronics-cooling calculation.

## The calculator

`scripts/thermo.py` (SI units; **temperatures in K** for ratios/radiation — it warns if a value looks like °C):
```bash
python3 scripts/thermo.py carnot --thot 800 --tcold 300        # max efficiency
python3 scripts/thermo.py engine --win 0 --qin 1000 --qout 600 # real efficiency
python3 scripts/thermo.py cop --type fridge --qcold 500 --win 200
python3 scripts/thermo.py gas --p 101325 --v 0.0224 --t 273.15 # solve for n (give any 3)
python3 scripts/thermo.py conduction --k 0.04 --area 10 --dt 20 --l 0.1
python3 scripts/thermo.py convection --h 25 --area 0.5 --dt 30
python3 scripts/thermo.py radiation --emiss 0.9 --area 0.5 --t1 350 --t2 300
python3 scripts/thermo.py rnetwork --power 50 --rth 0.5 1.2 2.0 --tamb 25   # series R, junction temp
```
Stdlib only.

## Chat output format

```
**Heat-sink sizing** (50 W chip, max Tj 90°C, 25°C ambient)

Budget: ΔT = 90 − 25 = 65°C over 50 W → total R_th ≤ 1.30 °C/W
Given R_jc 0.3 + R_cs 0.2 = 0.5 → sink R_sa ≤ 0.80 °C/W
→ pick a heat sink rated ≤ 0.8 °C/W (with the planned airflow). ✅
Sanity: passive sinks ~1–5 °C/W; 0.8 likely needs forced air.
```

## Workflow

1. **Identify the problem type:** cycle/efficiency, ideal-gas state, or heat transfer.
2. **Convert to consistent SI; use Kelvin** for any temperature ratio or radiation.
3. **Pick the formula** (Carnot/COP, PV=nRT + process, Fourier/Newton/Stefan-Boltzmann, R_th network).
4. **Compute with `thermo.py`**; for cooling, build the R_th series and solve for junction temp or required sink.
5. **Sanity-check vs the second law** (η ≤ Carnot) and physical ranges (h, k, R_sa typical values).
6. **Deliver** result + check; route fluid/airflow to `fluid-mechanics`, the R_th↔circuit analogy to `circuit-analysis`, structural/thermal-stress to `mechanical-engineering`.

## Key pitfalls

- **Celsius in a ratio or radiation.** η = 1 − Tc/Th and T⁴ radiation **demand Kelvin**. Using °C gives nonsense. (Temperature *differences* are the same in °C and K — those are fine.)
- **Claiming above-Carnot efficiency.** If a real number exceeds 1 − Tc/Th, it's wrong — recheck.
- **Forgetting COP > 1 is normal.** Heat pumps/fridges move more heat than the work input; that's not a violation.
- **Mode confusion.** Conduction needs k & thickness; convection needs h; radiation needs ε & Kelvin⁴ — don't mix the formulas.
- **Sign errors in the first law.** Be explicit about heat/work in vs out and the system boundary.
- **Ignoring radiation at high T or convection at low airflow.** All three modes can matter; for hot surfaces radiation isn't negligible.
- **Adiabatic compression heating.** Compressing a gas raises its temperature — don't assume isothermal unless it's slow/cooled.

## Quick reference

- 1st law: ΔU = Q − W. 2nd law: entropy ↑; η ≤ Carnot.
- η_Carnot = 1 − T_c/T_h (Kelvin) · COP_fridge = Q_c/W · COP_HP = Q_h/W = COP_fridge + 1.
- PV = nRT (R = 8.314). Adiabatic: PV^γ = const, T₂=T₁(P₂/P₁)^((γ−1)/γ), γ_air≈1.4.
- Conduction Q = kAΔT/L · Convection Q = hAΔT · Radiation Q = εσA(T₁⁴−T₂⁴), σ=5.67e-8, **K**.
- R_th = ΔT/Q; conduction L/(kA), convection 1/(hA); **series adds**; Tj = Tamb + P·ΣR_th.
- Heat ↔ electrical analogy (ΔT↔V, Q↔I, R_th↔R) → circuit-analysis.
