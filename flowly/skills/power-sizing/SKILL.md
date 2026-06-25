---
name: power-sizing
description: "Size electrical/electromechanical power components — motors (torque, speed, power), batteries (capacity, runtime, pack Wh, C-rate), solar PV arrays (panel count from daily energy and sun-hours), and wiring (gauge from ampacity and voltage drop). Includes a stdlib calculator. Use when the user asks what motor/battery/solar/wire size they need, runtime from a battery, how many panels, what AWG wire, or to size a power system."
metadata: {"flowly":{"emoji":"🔋","tags":["engineering","power","motor-sizing","battery","solar","wire-gauge","electrical","sizing"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["circuit-analysis","mechanical-engineering","fluid-mechanics","engineering-units"]}}
---

# Power Sizing — Motors, Batteries, Solar, and Wire

Power-system sizing is where a design meets reality: an undersized motor stalls, an undersized battery dies early, an undersized wire melts. The discipline is **size for the real load with margin, then check the constraints** (thermal, voltage drop, depth-of-discharge, duty cycle). Round up to standard components, and never run anything at 100% of its rating continuously.

## What this skill produces

**Chat-first.** Default: the required size with the calculation, a recommended standard component, and the constraint check (thermal/voltage-drop/DoD/margin). The `sizing.py` helper does motor, battery, solar, and wire sizing. Offer a fuller writeup for a full system (e.g. an off-grid solar + battery design).

## When to use

- "What motor / power do I need to \<lift/drive/turn\> this?"
- "How long will this battery last?" / "What battery capacity for X hours?"
- "How many solar panels / what array size for my daily usage?"
- "What wire gauge / AWG for this current and length?" / "Voltage drop?"
- "Size the power system for \<device/vehicle/off-grid setup\>."

## Motors

- **Mechanical power:** P = τ·ω, where ω (rad/s) = RPM·2π/60. So P(W) = τ(N·m)·RPM·2π/60. Or P ≈ τ·RPM/9.55.
- **Electrical input:** P_elec = P_mech / efficiency (motor η typically 0.7–0.95). Size the supply/driver to electrical, not mechanical, power.
- **Torque needs:** include the worst case — starting/stall torque, acceleration (τ = I·α), gravity (lifting: τ = F·r), and friction. Steady-state running torque is usually far below peak; size for peak but check continuous thermal rating.
- **Gearing** trades speed for torque (τ_out = τ_in × ratio, speed ÷ ratio; → `mechanical-engineering`). Match the motor's efficient speed band to the load via gearing.
- Always leave **margin** (~25%+) over the computed requirement for losses, wear, and transients.

## Batteries

- **Energy:** Wh = V × Ah. Runtime ≈ usable Wh / average load (W). **Usable** ≠ nameplate: apply **depth of discharge** (DoD — Li-ion ~80–90%, lead-acid ~50%) and a system/inverter efficiency.
- **Runtime = (V × Ah × DoD × η) / P_load.** Conversely, required Ah = P_load × hours / (V × DoD × η).
- **C-rate:** charge/discharge current relative to capacity. 1C = full capacity in 1 hour; a 10 Ah cell at 2C delivers 20 A. The pack must support the load's **peak current** within its C-rate, or it sags/overheats.
- **Pack building:** cells in **series** add voltage, in **parallel** add capacity (Ah). Account for the BMS, cell matching, and temperature.
- Size for the **real duty cycle** and end-of-life capacity fade (derate ~20% for aging), not the fresh nameplate.

## Solar PV

- **Daily energy need (Wh/day)** = Σ(load W × hours/day). This is the target the array must generate.
- **Array size:** Panel Wp needed = daily Wh / (peak-sun-hours × system efficiency). **Peak sun hours** (PSH) is location-specific (~3–6 h/day typical); system efficiency ~0.7–0.8 (wiring, inverter, dirt, temperature, MPPT losses).
- **Panel count** = required Wp / panel rating, rounded up.
- **Battery for autonomy:** size storage for the daily need × days-of-autonomy / DoD (off-grid needs 2–5 days of cloud cover). Pair with the battery sizing above.
- Add margin for winter PSH, panel degradation (~0.5%/yr), and future load growth.

## Wire gauge

Two independent constraints — size for the **worse** of:
1. **Ampacity (thermal):** the wire must carry the current without overheating (set by gauge, insulation temp rating, and bundling/ambient). Undersized = fire risk.
2. **Voltage drop:** V_drop = I × R_wire, where R = ρ·(2L)/A (2L = there-and-back). Keep drop under a budget (commonly ≤3% for power, ≤1–2% for sensitive). Long runs are usually **voltage-drop limited**, not ampacity limited — you'll need a fatter wire than ampacity alone suggests.
- Copper resistivity ρ ≈ 1.72e-8 Ω·m; AWG cross-sections are standard. Round **up** in size (down in AWG number) and respect breaker/fuse coordination.

## The calculator

`scripts/sizing.py` (stdlib):
```bash
python3 scripts/sizing.py motor --torque 2 --rpm 1500 --eff 0.85           # mech + elec power
python3 scripts/sizing.py motor --force 200 --radius 0.05 --rpm 1500       # from a lifting force
python3 scripts/sizing.py battery --voltage 12 --load 60 --hours 5 --dod 0.8 --eff 0.9
python3 scripts/sizing.py battery --voltage 48 --ah 100 --load 500         # -> runtime
python3 scripts/sizing.py solar --daily-wh 2000 --sun-hours 4.5 --panel 400
python3 scripts/sizing.py wire --current 20 --length 10 --voltage 12 --vdrop 0.03  # AWG by drop
```
Stdlib only.

## Chat output format

```
**Battery sizing — 60 W load for 5 h @ 12 V**

Required usable energy = 60 × 5 = 300 Wh
Account for DoD 80% + 90% efficiency → nameplate = 300/(0.8·0.9) = 417 Wh
Required capacity = 417/12 = 34.7 Ah → use a 40 Ah 12 V battery (margin).
Peak current 60/12 = 5 A → 0.15C on 40 Ah, well within limits ✅
```

## Workflow

1. **Define the real load** (W, torque, daily Wh) including peaks and duty cycle — not just the average.
2. **Compute the requirement** with `sizing.py`; convert mechanical→electrical / nameplate→usable via efficiencies and DoD.
3. **Add margin** (~25%) and **round up to a standard component**.
4. **Check the binding constraint:** motor thermal/continuous rating, battery C-rate/DoD, solar winter PSH, wire ampacity vs voltage-drop (size for the worse).
5. **Deliver** the recommended size + the constraint check; route circuit details to `circuit-analysis`, torque/gearing to `mechanical-engineering`, pumping loads to `fluid-mechanics`, unit conversions to `engineering-units`.

## Key pitfalls

- **Sizing for average, not peak.** Motors need start/stall torque; batteries must supply peak current within C-rate; a system that survives the average can still fail the transient.
- **Nameplate = usable (it isn't).** Battery usable energy is nameplate × DoD × efficiency, and fades with age — derate or you run short.
- **Wire by ampacity only.** Long runs are voltage-drop limited; an ampacity-OK wire can still drop too much voltage. Size for the worse constraint.
- **Forgetting efficiency.** Motor electrical input > mechanical output; inverter/charge losses eat solar and battery energy. Always divide by η at each conversion.
- **No margin / running at 100%.** Continuous operation at rated limit overheats and shortens life — leave headroom (~25%).
- **Ignoring temperature.** Battery capacity and wire ampacity drop in heat/cold; motors derate when hot. Account for the environment.
- **Solar at nameplate sun.** Real output uses location PSH × system efficiency (~0.7–0.8) and worst-month sun, not the panel's lab rating.

## Quick reference

- Motor: P(W) = τ(N·m)·RPM·2π/60 ≈ τ·RPM/9.55; P_elec = P_mech/η. Size for peak torque, check continuous.
- Battery: Wh = V·Ah; runtime = V·Ah·DoD·η / P_load; required Ah = P·h/(V·DoD·η). C-rate must cover peak current.
- Series adds voltage, parallel adds Ah. DoD: Li-ion ~80–90%, lead-acid ~50%. Derate ~20% for aging.
- Solar: required Wp = daily Wh /(PSH × ~0.75); panels = Wp/panel rating, round up; autonomy days for storage.
- Wire: size for worse of ampacity (thermal) and voltage drop V=I·ρ·2L/A (≤3% typical). Round up gauge.
- Always: real load + peaks, divide by η at each step, ~25% margin, round to standard parts.
