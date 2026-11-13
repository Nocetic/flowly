---
name: circuit-analysis
description: "Analyze electronic circuits — DC and AC, Ohm's law, KVL/KCL, series/parallel networks, voltage/current dividers, RC/RL/RLC time constants and filter cutoffs, op-amp configurations, impedance and resonance, plus SPICE simulation via ngspice. Includes a stdlib EE calculator (Ohm solve, dividers, equivalent R/C/L, RC cutoff, op-amp gains, dB, resistor color/E-series). Use when the user asks to analyze/solve a circuit, pick resistor values, design a filter, an op-amp stage, decode a resistor, or simulate with SPICE."
metadata: {"flowly":{"emoji":"⚡","tags":["engineering","electronics","circuits","spice","ohms-law","filters","op-amp","ee"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["pcb-kicad","control-systems","engineering-units","mechanical-engineering"]}}
---

# Circuit Analysis — Solve It by Hand, Then Simulate

Most circuit questions are solved with a handful of laws applied carefully, not a simulator. The discipline is: **identify the topology, apply the right law, keep units straight, then sanity-check** (does the power balance? is the op-amp saturating?). SPICE is for when the network is too tangled for hand analysis or you need a frequency/transient sweep.

## What this skill produces

**Chat-first.** Default: the worked solution — the law used, the algebra, the numeric answer with units, and a sanity check — plus chosen real-world component values (nearest E-series). The `ee_calc.py` helper does the arithmetic and component selection. Offer a SPICE netlist + simulation when the circuit warrants it.

## When to use

- "Solve this circuit / find the voltage/current at X."
- "What resistor for an LED / a voltage divider / to set a current?"
- "Design an RC / RL / RLC filter with cutoff fc." / "What's the corner frequency?"
- "Design an op-amp \<inverting/non-inverting/etc.\> stage with gain G."
- "Decode this resistor (color bands)." / "Nearest E12/E24 value?"
- "Simulate this with SPICE." / "Frequency response / transient?"

## DC fundamentals

- **Ohm's law:** V = I·R. **Power:** P = V·I = I²R = V²/R.
- **Series:** R_eq = ΣR; same current; voltages add. **Parallel:** 1/R_eq = Σ(1/R); same voltage; currents add. (Caps are the opposite: parallel add, series reciprocal.)
- **KVL:** ΣV around a loop = 0. **KCL:** ΣI into a node = 0. These solve any linear network (node-voltage or mesh-current method for the messy ones).
- **Voltage divider:** V_out = V_in · R2/(R1+R2). **Current divider:** I through a branch ∝ the *other* branch's conductance.
- **LED resistor:** R = (V_supply − V_LED) / I_LED (e.g. (5−2)/0.02 = 150 Ω; pick nearest E-series ≥, then check power).
- **Thévenin/Norton:** collapse any linear two-terminal network to a source + series/parallel resistance — invaluable for loading analysis.

## AC, reactance & filters

- **Impedance:** Z_R = R, Z_C = 1/(jωC), Z_L = jωL, with ω = 2πf. Magnitudes: |Z_C| = 1/(2πfC), |Z_L| = 2πfL.
- **RC time constant** τ = R·C (RL: τ = L/R); the circuit settles in ~5τ.
- **First-order cutoff (−3 dB):** f_c = 1/(2πRC) (RC) or R/(2πL) (RL). Below/above f_c is pass/stop depending on where you take the output (low-pass across C, high-pass across R).
- **RLC resonance:** f₀ = 1/(2π√(LC)); **Q** sets the bandwidth (BW = f₀/Q) and peaking.
- **Decibels:** voltage gain dB = 20·log₁₀(V_out/V_in); power dB = 10·log₁₀(P_out/P_in). −3 dB ≈ half power.

## Op-amps (ideal-first)

Analyze with the two golden rules (negative feedback assumed): **(1) no current into the inputs, (2) V+ = V− (virtual short).**
- **Inverting:** Gain = −Rf/Rin. (Input impedance = Rin.)
- **Non-inverting:** Gain = 1 + Rf/Rg. (Min gain 1.)
- **Buffer/follower:** gain 1, huge input Z — isolation.
- **Summing, difference, integrator (Rf→C), differentiator** — derive from the golden rules.
- **Reality check:** output can't exceed the rails (saturation), finite GBW limits high-freq gain, slew-rate limits fast edges, offsets/bias matter for precision. Always check the op-amp isn't railing.

## Real component values (E-series)

Resistors/caps come in standard ratios, not arbitrary values: **E12** (10% , 12/decade), **E24** (5%), E48/E96 (1%). After computing an ideal value, snap to the nearest available series value and note the resulting error. `ee_calc.py` does this and decodes/encodes 4-band resistor colors.

## The EE calculator

`scripts/ee_calc.py` covers the common arithmetic so answers are exact and component-realistic. Accepts engineering suffixes (k, M, m, u, n, p).

```bash
python3 scripts/ee_calc.py ohm --v 5 --r 220            # solve I, P (give any 2 of v/i/r/p)
python3 scripts/ee_calc.py divider --vin 12 --r1 10k --r2 3k3
python3 scripts/ee_calc.py req --parallel 10k 22k 47k   # or --series
python3 scripts/ee_calc.py ceq --series 100n 220n
python3 scripts/ee_calc.py rc --r 10k --c 100n          # tau + cutoff
python3 scripts/ee_calc.py led --vs 5 --vf 2.0 --i 20m
python3 scripts/ee_calc.py opamp --type inverting --rf 100k --rin 10k
python3 scripts/ee_calc.py db --vout 2 --vin 0.5
python3 scripts/ee_calc.py eseries 150 --series E24      # nearest standard value(s)
python3 scripts/ee_calc.py rcolor 220                    # value -> color bands (and bands -> value)
```
Stdlib only.

## SPICE (ngspice) — when hand analysis isn't enough

For complex networks, nonlinear parts, or frequency/transient sweeps, write a netlist and simulate:
```spice
* RC low-pass
V1 in 0 AC 1 SIN(0 1 1k)
R1 in out 10k
C1 out 0 100n
.ac dec 20 10 1Meg     ; frequency sweep
.tran 10u 5m           ; transient
.end
```
Run with `ngspice -b circuit.cir`. If ngspice isn't installed: `brew install ngspice` / `apt install ngspice`. Hand the user the netlist + command when the binary is absent. Use SPICE to *confirm* a hand result, not replace understanding.

## Chat output format

```
**LED current-limit resistor** (5V → 2.0V LED @ 20mA)

R = (5 − 2.0)/0.02 = 150 Ω → use 150 Ω (E24, exact) ✅
Power in R = 0.02²×150 = 60 mW → a 1/8 W (125 mW) resistor is fine.
Sanity: LED sees 2.0V, 20mA — within spec.
```

## Workflow

1. **Identify topology & what's asked** (DC operating point? AC response? a value?).
2. **Pick the method:** Ohm/divider for simple; node/mesh for networks; golden rules for op-amps; impedance for AC.
3. **Compute with `ee_calc.py`**; snap to E-series and check power ratings.
4. **Sanity-check:** power balance, op-amp not railing, signs/units right.
5. **Simulate with SPICE** if the network is complex or a sweep is wanted (give netlist + ngspice cmd if not installed).
6. **Deliver** the worked answer + real component values; route board layout to `pcb-kicad`, feedback/stability to `control-systems`.

## Key pitfalls

- **Unit slips.** mA vs A, nF vs µF, kΩ — one prefix error ruins the answer. Use suffixes consistently (the calculator parses them).
- **Ignoring power ratings.** A correct resistance that dissipates 2 W in a 1/4 W part burns up — always check P.
- **Forgetting loading.** A divider's output sags when a real load draws current; use Thévenin and keep load ≫ source impedance (or buffer it).
- **Op-amp railing / GBW.** Ideal gain means nothing if the output hits the supply or the frequency exceeds GBW/gain. Check the rails and bandwidth.
- **Series/parallel mix-up for caps.** Caps add in *parallel*, combine reciprocally in *series* — opposite of resistors.
- **Arbitrary component values.** 1.732 kΩ doesn't exist — snap to E-series and report the error.
- **Simulating instead of understanding.** SPICE confirms; it doesn't excuse skipping the topology analysis (and a wrong netlist gives a confident wrong answer).

## Quick reference

- V=IR · P=VI=I²R=V²/R · series R add / parallel reciprocal (caps opposite).
- Divider: Vout = Vin·R2/(R1+R2). LED: R=(Vs−Vf)/I.
- τ = RC (or L/R), settles in ~5τ · fc = 1/(2πRC) · f₀ = 1/(2π√(LC)).
- |Z_C| = 1/(2πfC), |Z_L| = 2πfL · dB = 20·log₁₀(Vout/Vin).
- Op-amp: inverting −Rf/Rin · non-inverting 1+Rf/Rg · golden rules: no input current, V+=V−.
- Snap values to E12/E24/E96; check power; SPICE (ngspice) for sweeps & complex nets.
