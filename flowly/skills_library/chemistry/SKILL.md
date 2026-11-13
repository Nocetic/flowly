---
name: chemistry
description: "Solve general chemistry problems — balance equations, compute molar mass and moles, stoichiometry (limiting reagent, theoretical/percent yield), concentration (molarity, dilution), gas laws (ideal gas, combined), and basic solution/acid-base math. Includes a stdlib calculator (molar mass from formula, equation balancing, stoichiometry, molarity). Use when the user asks to balance a reaction, find molar mass/moles, do stoichiometry, compute concentration or a dilution, or solve a gas-law problem."
metadata: {"flowly":{"emoji":"⚗️","tags":["science","chemistry","stoichiometry","molar-mass","balancing","concentration","gas-laws"],"requires":{"bins":["python3"]},"category":"science","related_skills":["engineering-units","physics-solver","statistical-analysis","lab-notebook"]}}
---

# Chemistry — Balance, Convert to Moles, Then Reason

Most general-chemistry problems follow one spine: **balance the equation → convert everything to moles → use the mole ratios → convert back to the units asked.** Moles are the currency; grams and liters are just denominations. Keep significant figures honest and watch the limiting reagent — it sets the ceiling on product.

## What this skill produces

**Chat-first.** Default: the worked solution — balanced equation, the mole conversions, the ratio step, and the answer with units and sig figs. The `chem.py` helper does molar mass, balancing, stoichiometry, and molarity so the arithmetic is exact.

## When to use

- "Balance this equation." / "Is this balanced?"
- "What's the molar mass of \<formula\>?" / "How many moles in X g?"
- "Stoichiometry: how much \<product\> from \<reactant\>?" / "Limiting reagent? Percent yield?"
- "What's the molarity / how do I dilute to X M?"
- "Ideal gas: find P/V/n/T." / "Combined gas law."

## The mole-centric workflow

1. **Balance** the equation (conserve atoms of each element; coefficients only, never change subscripts). The balanced coefficients ARE the mole ratios.
2. **Convert givens to moles:** moles = mass / molar mass; moles = molarity × volume(L); moles = PV/RT (gas).
3. **Apply the mole ratio** from the balanced equation to get moles of the target.
4. **Convert back** to the requested unit (grams, liters, molarity, particles via Avogadro 6.022e23).
5. **Limiting reagent:** when two+ reactants are given, compute moles-of-product each could make; the smallest wins — that reactant is limiting and caps the yield.

## Key relationships

- **Molar mass** = Σ (atomic mass × count) over the formula (g/mol). `chem.py mass H2SO4` → 98.08.
- **Moles** n = m/M = C·V = N/N_A.
- **Concentration:** Molarity M = mol solute / L solution. **Dilution:** C₁V₁ = C₂V₂.
- **Ideal gas:** PV = nRT (R = 0.08206 L·atm/mol·K, or 8.314 J/mol·K — match your units; T in **Kelvin**). Combined: P₁V₁/T₁ = P₂V₂/T₂.
- **Percent yield** = actual / theoretical × 100. **Theoretical yield** comes from the limiting reagent via stoichiometry.

## The calculator

`scripts/chem.py` (stdlib; parses formulas with parentheses and hydrates):
```bash
python3 scripts/chem.py mass H2SO4                       # molar mass
python3 scripts/chem.py mass "Ca(OH)2"                   # parentheses ok
python3 scripts/chem.py moles --mass 10 --formula NaCl   # grams -> moles
python3 scripts/chem.py balance "H2 + O2 -> H2O"         # balanced equation
python3 scripts/chem.py stoich "N2 + H2 -> NH3" --given N2 --grams 28 --want NH3
python3 scripts/chem.py molarity --moles 0.5 --liters 2  # or --mass+--formula
python3 scripts/chem.py dilute --c1 6 --v1 0.05 --c2 1   # solve V2
```
Stdlib only.

## Chat output format

```
**Stoichiometry — NH3 from 28 g N2** (Haber process)

Balanced: N2 + 3 H2 → 2 NH3
Moles N2 = 28 g / 28.02 g/mol = 1.00 mol
Ratio N2:NH3 = 1:2 → 2.00 mol NH3
Mass NH3 = 2.00 mol × 17.03 g/mol = 34.1 g (3 sig figs)
(Assumes H2 in excess; if H2 is also given, check the limiting reagent.)
```

## Workflow

1. **Balance the equation** (`balance`) — coefficients only; verify atom counts.
2. **Convert givens to moles** (mass/M, C·V, PV/RT) with the right molar masses (`mass`/`moles`).
3. **Apply mole ratios**; for multiple reactants find the **limiting reagent**.
4. **Convert to the asked unit**; apply **percent yield** if actual is given.
5. **Sig figs** to match the least-precise input; **Kelvin** for gas laws.
6. **Deliver** the worked steps + answer; route unit conversions to `engineering-units`, lab documentation to `lab-notebook`, data/stats to `statistical-analysis`.

## Key pitfalls

- **Changing subscripts to "balance".** Only coefficients change — altering a formula makes it a different compound. Balance by coefficients.
- **Skipping the mole conversion.** You can't use grams in a ratio; everything goes through moles first.
- **Ignoring the limiting reagent.** With two+ reactants, the smaller product-capacity governs; using the wrong one overstates yield.
- **Celsius in gas laws.** PV=nRT and combined gas law need **Kelvin** (K = °C + 273.15).
- **Mismatched R units.** Use 0.08206 (L·atm) or 8.314 (J) consistently with your P, V units.
- **Sig-fig inflation.** Don't report more precision than the data supports.
- **Forgetting state/conditions** (STP assumptions, solution vs solvent volume in molarity).

## Quick reference

- Spine: balance → moles → mole ratio → back-convert. Coefficients = mole ratios.
- n = m/M = C·V = PV/RT = N/6.022e23. Molarity = mol/L. Dilution C₁V₁=C₂V₂.
- Ideal gas PV=nRT (R 0.08206 L·atm or 8.314 J; **T in K**). Combined P₁V₁/T₁=P₂V₂/T₂.
- Limiting reagent = the one making the least product. % yield = actual/theoretical×100.
- `chem.py mass|moles|balance|stoich|molarity|dilute`; units → engineering-units.
