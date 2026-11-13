---
name: engineering-units
description: "Convert units and reason about dimensions for engineering and science — length, mass, force, pressure, energy, power, temperature, volume, flow, torque, speed, angle, data, and more, across SI and US/Imperial. Handles dimensional analysis (catching unit mismatches), significant figures, SI prefixes, and common physical constants. Includes a stdlib converter. Use when the user needs a unit conversion, asks 'how many X in Y', wants to check that an equation's units balance, or needs an engineering constant."
metadata: {"flowly":{"emoji":"📏","tags":["engineering","units","conversion","dimensional-analysis","si","imperial","measurement","constants"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["mechanical-engineering","circuit-analysis","control-systems","statistical-analysis"]}}
---

# Engineering Units — Convert, and Make the Dimensions Balance

Unit errors are the silent killers of engineering — the Mars Climate Orbiter was lost to a pound-force/newton mix-up. This skill does two jobs: **convert accurately** (with the right precision), and **reason dimensionally** so an equation's units actually balance before you trust the number. The habit: carry units through every calculation; if the units don't come out right, the formula is wrong — no matter how clean the arithmetic looks.

## What this skill produces

**Chat-first.** Default: the converted value with appropriate significant figures and the conversion factor shown, or a dimensional-analysis check that confirms/flags an equation. The `units.py` helper does conversions across many quantity types. Quick, inline, no file needed.

## When to use

- "Convert X \<unit\> to \<unit\>." / "How many \<unit\> in \<unit\>?"
- "What's \<value\> in SI / metric / imperial?"
- "Do the units balance in this equation?" / "Is this dimensionally consistent?"
- "How many sig figs should this answer have?"
- "What's the value of \<constant\> (g, c, R, ...)?"
- Embedded in any calc where a conversion or unit check is needed (often called by `mechanical-engineering`, `circuit-analysis`).

## Dimensional analysis (the discipline, not just conversion)

Every physical quantity has dimensions built from the base set: **mass (M), length (L), time (T), current (I), temperature (Θ), amount (N), luminous intensity (J).**
- **Both sides of an equation must have identical dimensions.** Force [M·L·T⁻²], energy [M·L²·T⁻²], pressure [M·L⁻¹·T⁻²]. If your derived units don't match the expected ones, the equation is wrong.
- **Only add/compare like dimensions.** You can't add a length to an area; you can't add 5 m to 3 s.
- **Carry units through the algebra.** Treat units as multiplicative factors that cancel: (kg·m/s²)·m = kg·m²/s² = J. This catches errors arithmetic alone won't.
- **Use it to derive/recall relations:** if you need a quantity in [L/T] (speed) from things in [L] and [T], the form is forced.

## Conversion done right

- **Convert via the base/SI unit** to avoid chained rounding. (e.g. inches → meters → target.)
- **Mind the difference between *factor* and *offset* conversions.** Temperature is affine: °C↔°F↔K need offsets (°F = °C·9/5 + 32; K = °C + 273.15), not just a multiply. A *temperature difference* converts differently from a *temperature* (ΔT of 1°C = 1 K = 1.8°F).
- **Don't confuse mass and force.** kg is mass; kgf and lbf are *force* (mass × g). lb (pound-mass) ≠ lbf in any system where they're distinguished — be explicit. This is the classic catastrophic error.
- **Gauge vs absolute pressure** (psig vs psia, barg vs bara) differ by 1 atm — state which.
- **US vs Imperial gallons** differ (~20%); US fluid units ≠ UK. Specify.
- **Significant figures:** the result can't be more precise than the least-precise input. Don't report 3.28084 ft for a value known to 2 sig figs — round to match, and keep the exact factor only for intermediate steps.

## The converter

`scripts/units.py` converts within a quantity type (it won't let you convert a length to a mass — a built-in dimensional guard). SI prefixes and common US/Imperial units included.

```bash
python3 scripts/units.py 12 in mm                 # 304.8 mm
python3 scripts/units.py 100 hp kW                 # power
python3 scripts/units.py 30 psi bar               # pressure
python3 scripts/units.py 1 "kWh" J                 # energy
python3 scripts/units.py 60 mph "m/s"             # speed
python3 scripts/units.py 25 degC degF             # temperature (offset-aware)
python3 scripts/units.py 50 "N*m" "lbf*ft"        # torque
python3 scripts/units.py --list pressure           # show supported units in a category
python3 scripts/units.py --constants               # common physical constants
```
Stdlib only. Pass `--sig N` to control output significant figures.

## Common constants (quick recall)

- g (standard gravity) = 9.80665 m/s²
- c (speed of light) = 2.998×10⁸ m/s
- R (gas constant) = 8.314 J/(mol·K)
- N_A (Avogadro) = 6.022×10²³ /mol
- k_B (Boltzmann) = 1.381×10⁻²³ J/K
- atmospheric pressure = 101.325 kPa = 14.696 psi = 1 atm
- water density ≈ 1000 kg/m³ (1 g/cm³); air ≈ 1.225 kg/m³ (sea level)
- e (elementary charge) = 1.602×10⁻¹⁹ C
`units.py --constants` prints these with units.

## Chat output format

```
12 in → **304.8 mm**  (×25.4 exact)

60 mph → **26.8 m/s**  (×0.44704)   [2 sig figs from input]

Dimensional check — kinetic energy ½mv²:
[M]·[L/T]² = M·L²·T⁻² = Joule ✅  (matches energy)
```

## Workflow

1. **Identify the quantity type** (length/force/pressure/...) and the from/to units; note gauge/absolute, mass/force, US/UK if ambiguous.
2. **Convert via SI** with `units.py` (or by hand showing the factor).
3. **Apply significant figures** matching the input precision.
4. **For equations, run the dimensional check** — confirm both sides match before trusting the number.
5. **Deliver** value + factor (+ a precision note); feed results back into `mechanical-engineering`/`circuit-analysis` as needed.

## Key pitfalls

- **Mass vs force (kg vs kgf/lbf, lb vs lbf).** The classic disaster — always distinguish, and multiply/divide by g when crossing.
- **Temperature offsets.** °C↔°F↔K aren't pure scale factors; and a *difference* converts unlike an *absolute* temperature.
- **Gauge vs absolute pressure.** psig vs psia differ by ~1 atm — state which the value is.
- **US vs Imperial (gallons, tons, fluid ounces).** ~20% apart; specify the system.
- **Chained rounding.** Convert through SI in one shot; don't round at each hop.
- **False precision.** Output sig figs must not exceed input precision.
- **Skipping the dimensional check.** A formula that "looks right" but whose units don't balance is wrong — verify dimensions before computing.
- **Ambiguous unit symbols.** "ton" (metric/long/short), "oz" (mass/fluid), "cal" vs "kcal" — disambiguate.

## Quick reference

- Base dimensions: M, L, T, I, Θ, N, J. Force [MLT⁻²], energy [ML²T⁻²], pressure [ML⁻¹T⁻²], power [ML²T⁻³].
- Both sides of an equation must share dimensions; carry units through and let them cancel.
- Convert via SI; temperature is offset-based; distinguish mass vs force and gauge vs absolute.
- Sig figs of the answer ≤ least-precise input.
- Exact: 1 in = 25.4 mm · 1 atm = 101.325 kPa = 14.696 psi · 1 hp ≈ 745.7 W · 1 kWh = 3.6 MJ.
- `units.py` guards against cross-dimension conversions and is offset-aware for temperature.
