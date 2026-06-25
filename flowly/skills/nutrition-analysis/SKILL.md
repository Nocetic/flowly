---
name: nutrition-analysis
description: "Analyze nutrition and energy balance — estimate BMR and TDEE, set calorie targets for a goal (cut/maintain/gain), compute macronutrient splits (protein/carb/fat) and protein needs, analyze a meal log against targets, and give evidence-aware guidance. Includes a stdlib calculator (BMR/TDEE, macro targets, calorie math). Use when the user asks about calories, BMR/TDEE, macros, a meal/diet analysis, protein needs, or a cutting/bulking calorie target."
metadata: {"flowly":{"emoji":"🥗","tags":["health","nutrition","calories","bmr","tdee","macros","diet","fitness"],"requires":{"bins":["python3"]},"category":"health","related_skills":["clinical-evidence","statistical-analysis","engineering-units"]}}
---

# Nutrition Analysis — Energy Balance and Macros, Evidence-Aware

Most nutrition questions reduce to **energy balance** (calories in vs out) and **macronutrient adequacy** (especially protein). The math is simple and worth doing precisely; the surrounding advice should be evidence-based and humble — individual needs vary, and this isn't a substitute for a professional. Compute the numbers, then frame them as estimates with sensible ranges.

> **Not medical or dietetic advice.** General educational estimates only. Calorie/macro formulas are population approximations with real individual variance; anyone with a medical condition, eating disorder history, pregnancy, or specific clinical needs should consult a registered dietitian or physician. Don't give restrictive targets to someone who shouldn't have them.

## What this skill produces

**Chat-first.** Default: the estimate — BMR/TDEE, a calorie target for the goal, a macro split with protein anchored to body weight, and (if a meal log is given) how it stacks up against targets — all framed as approximations. The `nutrition.py` helper does the math. Keep guidance evidence-aware and non-prescriptive.

## When to use

- "How many calories do I need / to lose/gain weight?"
- "What's my BMR / TDEE / maintenance calories?"
- "What macros should I eat?" / "How much protein?"
- "Analyze my meal / day of eating against my goal."
- "Is this diet/meal balanced?"

## Energy balance

- **BMR (basal metabolic rate)** — calories at complete rest. **Mifflin-St Jeor** (most accurate for most people):
  - Men: 10·kg + 6.25·cm − 5·age + 5
  - Women: 10·kg + 6.25·cm − 5·age − 161
- **TDEE (total daily energy expenditure)** = BMR × activity factor:
  - sedentary 1.2 · light 1.375 · moderate 1.55 · very active 1.725 · extra 1.9
  TDEE is maintenance — eat that to hold weight. (These are estimates; real expenditure varies ±10–15%, so adjust from observed weight trend, not the formula alone.)
- **Goal targets:**
  - **Lose:** deficit of ~15–25% (or ~250–500 kcal/day → ~0.25–0.5 kg/wk; ~7,700 kcal ≈ 1 kg fat). Avoid aggressive deficits.
  - **Maintain:** = TDEE.
  - **Gain:** surplus ~10–20% (~250–500 kcal/day for lean gain).

## Macronutrients

- **Protein** (4 kcal/g) — anchor to body weight: ~1.6–2.2 g/kg for active people / muscle retention (higher end when cutting); ~0.8 g/kg is the RDA minimum. The most important macro for body composition and satiety.
- **Fat** (9 kcal/g) — ~20–35% of calories; don't go very low (hormones, fat-soluble vitamins). ~0.6–1 g/kg as a floor.
- **Carbohydrate** (4 kcal/g) — the remainder; fuels training and brain. Not inherently "bad" — fill the calorie balance after protein and fat.
- **Fiber** ~14 g per 1,000 kcal (~25–38 g/day); **alcohol** 7 kcal/g (empty).
- Compute: set protein (g/kg) and fat (% or g/kg), fill the rest with carbs to hit the calorie target.

## Meal-log analysis

Sum calories and macros from the log, compare to targets, and report the gaps (e.g. "protein 80 g vs ~140 g target — low"; "calories 300 over target"). Flag obvious imbalances (very low protein/fiber, excessive added sugar/sat-fat) without moralizing food. Food-database lookups (USDA FoodData Central) give per-item values; the bot can use provided values or look them up.

## Evidence-aware guidance

- **Energy balance governs weight**; no macro split overrides it. Adherence beats the "optimal" diet.
- Be skeptical of fad claims; cite the consensus (protein for satiety/muscle, fiber for health, deficit for fat loss) and flag where evidence is weak. (→ `clinical-evidence` for appraising specific claims.)
- **Individualize and stay non-prescriptive** — present ranges, suggest adjusting from real-world results, and defer clinical situations to professionals.

## The calculator

`scripts/nutrition.py` (stdlib):
```bash
python3 scripts/nutrition.py tdee --sex m --age 30 --kg 80 --cm 180 --activity moderate
python3 scripts/nutrition.py target --tdee 2600 --goal lose                 # calorie target
python3 scripts/nutrition.py macros --calories 2200 --kg 80 --protein-gkg 1.8 --fat-pct 0.25
python3 scripts/nutrition.py meal --calories 2400 --protein 90 --carbs 250 --fat 95 --target-cal 2200 --target-protein 140
```
Stdlib only.

## Chat output format

```
**Energy & macros** (male, 30, 80 kg, 180 cm, moderate activity)

BMR (Mifflin-St Jeor) ≈ 1,780 kcal · TDEE ≈ 2,759 kcal (maintenance)
Goal = lose: target ≈ 2,200 kcal/day (~20% deficit → ~0.5 kg/wk)
Macros @ 2,200 kcal:
  Protein 144 g (1.8 g/kg, 26%) · Fat 61 g (25%) · Carbs 268 g (49%)
Note: estimates ±10–15%; adjust from your actual weight trend over 2–3 weeks.
⚠️ General info, not dietetic advice — see a professional for medical needs.
```

## Workflow

1. **Gather** sex, age, weight, height, activity, and the goal (and a meal log if analyzing).
2. **BMR → TDEE** (`tdee`); set the **calorie target** for the goal (`target`) with a sensible (non-aggressive) deficit/surplus.
3. **Macros** (`macros`): anchor protein to g/kg, set fat, fill carbs.
4. **Analyze the log** (`meal`) vs targets; flag gaps and imbalances without moralizing.
5. **Frame as estimates** with ranges; advise adjusting from real results; defer clinical cases.
6. **Deliver** with the not-advice guard; route claim appraisal to `clinical-evidence`, data trends to `statistical-analysis`, unit conversions to `engineering-units`.

## Key pitfalls

- **False precision.** BMR/TDEE formulas are ±10–15% estimates — present ranges and tell the user to calibrate from observed weight change, not to trust the number to the calorie.
- **Ignoring protein.** Under-eating protein is the most common gap; anchor it to body weight, not a flat %.
- **Aggressive deficits.** Crash targets harm adherence, muscle, and health — keep deficits moderate (~0.5–1% body weight/week).
- **Moralizing food / "good vs bad".** Analyze against targets neutrally; demonizing foods or extreme restriction is harmful and out of scope.
- **One-size-fits-all.** Needs vary by genetics, training, and conditions — individualize and present options.
- **Overstepping into clinical advice.** Eating disorders, pregnancy, diabetes, kidney disease, etc. → defer to a professional; don't hand out restrictive plans.
- **Forgetting alcohol / liquid calories** in the log — common blind spot.

## Quick reference

- BMR (Mifflin-St Jeor): 10·kg + 6.25·cm − 5·age (+5 male / −161 female). TDEE = BMR × activity (1.2–1.9).
- Goal: lose = −15–25% (~0.5 kg/wk, 7,700 kcal≈1 kg) · maintain = TDEE · gain = +10–20%.
- Protein 1.6–2.2 g/kg (4 kcal/g) · fat 20–35% (9 kcal/g) · carbs = remainder (4 kcal/g); fiber ~14 g/1000 kcal.
- Energy balance governs weight; adherence > "optimal" diet. Estimates ±10–15% — calibrate from results.
- `nutrition.py tdee|target|macros|meal`. Not medical/dietetic advice; defer clinical cases.
