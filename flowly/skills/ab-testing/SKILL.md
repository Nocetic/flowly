---
name: ab-testing
description: "Design and analyze A/B tests and experiments — frame the hypothesis and metric, compute the required sample size and power, run the significance test (proportions z-test, means t-test), report confidence intervals and lift, and avoid the classic traps (peeking, multiple comparisons, underpowered tests, p-hacking). Includes a stdlib calculator for sample size, significance, and CIs. Use when the user is planning or analyzing an experiment, asks about statistical significance, sample size, conversion-rate tests, or whether a result is real."
metadata: {"flowly":{"emoji":"🧪","tags":["data","ab-testing","experimentation","statistics","significance","sample-size","conversion"],"requires":{"bins":["python3"]},"category":"data","related_skills":["statistical-analysis","data-visualization","sql-query","startup-unit-economics"]}}
---

# A/B Testing — Decide Before You Look, Then Look Once

The hard part of A/B testing isn't the math — it's the discipline that keeps the math honest: **decide the metric, effect size, and sample size before you start**, then run to that sample and analyze once. Most "significant" results in the wild are artifacts of peeking, tiny samples, or testing twenty things and reporting the lucky one. This skill plans tests properly and analyzes them without fooling itself.

## What this skill produces

**Chat-first.** Default: for planning — the required sample size with the assumptions stated; for analysis — the test result (lift, p-value, confidence interval) with a plain verdict and the caveats. The `abtest.py` helper does sample size, the proportion z-test, and CIs. Be explicit that statistical significance ≠ practical/business significance.

## When to use

- "How many users / how long do I need for this A/B test?"
- "Is this result statistically significant?" / "Did B beat A?"
- "Design an experiment to test \<change\>."
- "What's the confidence interval / lift / uplift?"
- "Can I stop the test early?" / "Why shouldn't I peek?"
- "We tested 10 variants — which won?" (multiple comparisons)

## Design first (this is where tests are won or lost)

1. **One clear hypothesis & primary metric.** "Changing the button to green increases checkout conversion." Pick **one primary metric** decided up front; everything else is secondary/guardrail. Define it precisely (per user? per session? over what window?).
2. **Minimum Detectable Effect (MDE).** The smallest lift worth detecting — set by what's *business-meaningful*, not by hope. Smaller MDE ⇒ much larger sample.
3. **Significance (α) and power (1−β).** Conventionally α = 0.05 (5% false-positive rate) and power = 80% (20% chance of missing a real effect). Power matters as much as α — an **underpowered test that shows "no effect" is uninformative**, not evidence of no effect.
4. **Compute sample size BEFORE running** (`abtest.py size`). It depends on baseline rate, MDE, α, power. Then run until you hit it — and roughly how long that takes at your traffic.
5. **Randomize properly** at the right unit (usually per-user, sticky across sessions), check the split is even, and watch for sample-ratio mismatch (SRM) — a skewed split signals a bug.

## Analyze once, correctly

- **Proportions (conversion rates):** two-proportion **z-test**; report each rate, the absolute and relative lift, the p-value, and the **confidence interval on the difference**. The CI is more informative than the p-value alone — it shows the plausible range of the effect (and whether it includes zero).
- **Continuous metrics (revenue, time):** **t-test** on means (or a non-parametric/bootstrap test for skewed data like revenue, which is rarely normal — means can be dominated by outliers/whales; consider trimming or a rank test).
- **Verdict:** p < α AND the CI excludes a trivial effect AND the lift is practically meaningful → ship. State all three; significance alone isn't enough.
- **One-sided vs two-sided:** default two-sided unless you genuinely only care about one direction (and decided so up front).

## The traps (where false results come from)

- **Peeking / optional stopping.** Repeatedly checking and stopping when it's significant **massively inflates the false-positive rate** (checking continuously can hit "p<0.05" ~> 20–50% of the time even with no real effect). Fix: fixed sample size decided up front, or a proper **sequential test** (e.g. always-valid p-values / group sequential boundaries) designed for early stopping.
- **Multiple comparisons.** Testing many variants or metrics inflates false positives (test 20 at α=0.05 → ~1 false win expected). Correct (Bonferroni / Benjamini-Hochberg) or pre-register one primary metric.
- **Underpowered tests.** Too-small samples can't detect real effects and produce noisy, exaggerated "wins" (winner's curse). Compute power first.
- **p-hacking / HARKing.** Slicing by segments until something is significant, or inventing the hypothesis after seeing the data. Pre-specify.
- **Novelty/primacy & seasonality.** Early behavior differs; run at least full business cycles (often ≥1–2 weeks), not just until significant.
- **Confounds & SRM.** Unequal splits, overlapping experiments, bot traffic — validate the randomization.

## The calculator

`scripts/abtest.py` (stdlib):
```bash
# sample size per arm for a conversion test
python3 scripts/abtest.py size --baseline 0.10 --mde 0.02 --power 0.8 --alpha 0.05
python3 scripts/abtest.py size --baseline 0.10 --mde-rel 0.10        # 10% relative lift
# analyze a finished conversion test
python3 scripts/abtest.py prop --a 1000 --conv-a 100 --b 1000 --conv-b 130
# analyze means (summary stats)
python3 scripts/abtest.py means --mean-a 50 --sd-a 12 --n-a 500 --mean-b 53 --sd-b 13 --n-b 500
```
Stdlib only (normal/t approximations).

## Chat output format

```
**A/B result — conversion (B vs A)**

A: 100/1000 = 10.0%   B: 130/1000 = 13.0%
Absolute lift +3.0pp · relative +30.0%
z = 2.05, p = 0.040 (two-sided) → significant at α=0.05 ✅
95% CI on difference: [+0.1pp, +5.9pp] (excludes 0)

Verdict: B wins, but the CI is wide (lift could be ~0.1pp to ~5.9pp) — the
effect is real but its size is uncertain. Confirm it clears your MDE and ran
a full week. Did you peek / test other variants? If so, discount accordingly.
```

## Workflow

1. **Design:** one hypothesis + primary metric, MDE (business-driven), α/power. **Compute sample size first** (`size`); estimate duration.
2. **Run** to the planned sample over full cycles; validate the split (SRM), no peeking.
3. **Analyze once:** proportions z-test or means t-test (`prop`/`means`); report rates, lift, p-value, and **CI**.
4. **Judge on three things:** significance, CI excluding trivial effect, and practical/business meaning — not p alone.
5. **Caveat** honestly (power, peeking, multiple comparisons, novelty/seasonality).
6. **Deliver** verdict + numbers + caveats; route deeper stats to `statistical-analysis`, the data pull to `sql-query`, charts to `data-visualization`, business impact to `startup-unit-economics`.

## Key pitfalls

- **Peeking and stopping early.** The biggest source of false wins — fix the sample up front or use a sequential method built for it.
- **No power / sample-size calc.** Underpowered tests miss real effects and exaggerate the ones they "find." Compute before running.
- **Multiple comparisons uncorrected.** Many variants/metrics → inflated false positives. Pre-register a primary metric or correct.
- **Significance = importance (it isn't).** A statistically significant 0.1% lift may not be worth shipping; report the CI and practical size.
- **Revenue treated as normal.** Skewed/whale-driven; use bootstrap/non-parametric or trim, not a naive t-test.
- **Too-short tests.** Novelty effects and weekday/weekend swings — run full cycles, not "until significant."
- **Ratio/SRM blindness.** A skewed split or overlapping experiments silently bias results — validate randomization.
- **Restating the hypothesis post-hoc.** HARKing/segment-fishing invents significance — pre-specify.

## Quick reference

- Decide up front: one primary metric, MDE (business-driven), α (0.05), power (0.80). **Sample size before running.**
- Proportions → two-proportion z-test; means → t-test (bootstrap for skewed revenue).
- Report rate(s), absolute & relative lift, p-value, **and the CI on the difference**.
- Ship if: significant AND CI excludes a trivial effect AND it's practically meaningful.
- Don't: peek/optional-stop, run underpowered, test many things uncorrected, stop "when significant", ignore seasonality/SRM.
- `abtest.py size|prop|means`; deeper stats → statistical-analysis.
