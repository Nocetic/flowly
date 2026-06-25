#!/usr/bin/env python3
"""Nutrition calculator — BMR/TDEE (Mifflin-St Jeor), calorie target, macro
split, meal-log vs target. Stdlib only. Chat-ready markdown.

General educational estimates only — NOT medical/dietetic advice. Formulas are
population approximations (±10-15%).

Usage:
    nutrition.py tdee --sex m --age 30 --kg 80 --cm 180 --activity moderate
    nutrition.py target --tdee 2600 --goal lose
    nutrition.py macros --calories 2200 --kg 80 --protein-gkg 1.8 --fat-pct 0.25
    nutrition.py meal --calories 2400 --protein 90 --carbs 250 --fat 95 \
        --target-cal 2200 --target-protein 140
"""
from __future__ import annotations

import argparse

ACTIVITY = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725, "very": 1.9}


def bmr_msj(sex, age, kg, cm):
    base = 10 * kg + 6.25 * cm - 5 * age
    return base + (5 if sex.lower().startswith("m") else -161)


def cmd_tdee(a):
    if a.activity not in ACTIVITY:
        raise SystemExit(f"--activity one of {list(ACTIVITY)}")
    bmr = bmr_msj(a.sex, a.age, a.kg, a.cm)
    tdee = bmr * ACTIVITY[a.activity]
    print(f"**Energy expenditure** ({a.sex}, {a.age}y, {a.kg} kg, {a.cm} cm, {a.activity})\n")
    print(f"BMR (Mifflin-St Jeor) ≈ {bmr:.0f} kcal")
    print(f"TDEE = BMR × {ACTIVITY[a.activity]} ≈ **{tdee:.0f} kcal/day** (maintenance)")
    print("_±10–15% estimate — calibrate from your actual weight trend. Not dietetic advice._")


def cmd_target(a):
    g = a.goal
    if g == "lose":
        t = a.tdee * 0.80
        note = "~20% deficit → ~0.5 kg/wk"
    elif g == "gain":
        t = a.tdee * 1.12
        note = "~12% surplus → lean gain"
    else:
        t = a.tdee
        note = "maintenance"
    print(f"**Calorie target** (TDEE {a.tdee:.0f}, goal: {g})\n")
    print(f"Target ≈ **{t:.0f} kcal/day** ({note})")
    if g == "lose":
        print("Keep deficits moderate; very aggressive cuts harm adherence, muscle, and health.")


def cmd_macros(a):
    cal = a.calories
    protein_g = a.protein_gkg * a.kg
    protein_cal = protein_g * 4
    fat_cal = cal * a.fat_pct
    fat_g = fat_cal / 9
    carb_cal = cal - protein_cal - fat_cal
    carb_g = carb_cal / 4
    print(f"**Macros @ {cal:.0f} kcal** (body weight {a.kg} kg)\n")
    if carb_cal < 0:
        print("⚠️ protein + fat already exceed the calorie target — lower fat% or protein g/kg.")
    print(f"Protein {protein_g:.0f} g ({a.protein_gkg} g/kg, {protein_cal/cal*100:.0f}%)")
    print(f"Fat     {fat_g:.0f} g ({a.fat_pct*100:.0f}%)")
    print(f"Carbs   {carb_g:.0f} g ({max(carb_cal,0)/cal*100:.0f}%)")
    print(f"Fiber target ≈ {cal/1000*14:.0f} g")


def cmd_meal(a):
    cal = a.calories
    cals_from = a.protein * 4 + a.carbs * 4 + a.fat * 9
    print(f"**Meal-log analysis**\n")
    print(f"Logged: {cal:.0f} kcal · P {a.protein:.0f}g · C {a.carbs:.0f}g · F {a.fat:.0f}g")
    print(f"(macros imply {cals_from:.0f} kcal — {'matches' if abs(cals_from-cal)<100 else 'differs from'} stated calories)")
    if a.target_cal:
        diff = cal - a.target_cal
        print(f"Calories vs target {a.target_cal:.0f}: {diff:+.0f} kcal "
              + ("✅ on target" if abs(diff) <= 100 else ("⚠️ over" if diff > 0 else "⚠️ under")))
    if a.target_protein:
        pdiff = a.protein - a.target_protein
        print(f"Protein vs target {a.target_protein:.0f}g: {pdiff:+.0f}g "
              + ("✅" if pdiff >= -10 else "⚠️ low — prioritize protein"))
    # rough balance flags
    if cal and a.protein * 4 / cal < 0.15:
        print("Note: protein is a low share of calories.")
    print("_Estimates; not dietetic advice._")


def main():
    ap = argparse.ArgumentParser(description="Nutrition calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tdee"); p.add_argument("--sex", required=True); p.add_argument("--age", type=float, required=True); p.add_argument("--kg", type=float, required=True); p.add_argument("--cm", type=float, required=True); p.add_argument("--activity", default="moderate"); p.set_defaults(fn=cmd_tdee)
    p = sub.add_parser("target"); p.add_argument("--tdee", type=float, required=True); p.add_argument("--goal", choices=["lose", "maintain", "gain"], default="maintain"); p.set_defaults(fn=cmd_target)
    p = sub.add_parser("macros"); p.add_argument("--calories", type=float, required=True); p.add_argument("--kg", type=float, required=True); p.add_argument("--protein-gkg", type=float, default=1.8); p.add_argument("--fat-pct", type=float, default=0.25); p.set_defaults(fn=cmd_macros)
    p = sub.add_parser("meal"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("calories", "protein", "carbs", "fat")]; p.add_argument("--target-cal", type=float); p.add_argument("--target-protein", type=float); p.set_defaults(fn=cmd_meal)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
