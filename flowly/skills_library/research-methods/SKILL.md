---
name: research-methods
description: "Design scientific studies: hypotheses, controls, confounders, preregistration, validity."
metadata: {"flowly":{"tags":["science","research-methods","study-design","experiments","causal-inference"],"requires":{"bins":["python3"]},"category":"science","related_skills":["statistical-analysis","lab-notebook","scientific-peer-review"]}}
---

# Research Methods

Use this skill when the user is designing or critiquing a study, experiment, survey, benchmark, simulation, assay, or observational analysis.

## Workflow

1. Turn the question into a testable claim.
   - Define exposure/intervention, outcome, unit, setting, and timeframe.
2. Choose a design.
   - Experiment, randomized trial, observational study, simulation, benchmark, case study, qualitative study, or mixed method.
3. Identify threats to validity.
   - Confounding
   - Selection bias
   - Measurement error
   - Leakage
   - Multiple comparisons
   - External validity limits
4. Specify controls and comparisons.
   - Positive control
   - Negative control
   - Baseline
   - Placebo/sham, if relevant
5. Define the analysis plan before looking at outcomes.
6. Write down decision rules and stopping rules.
7. Prefer a preregistration-style protocol when results will matter.

## Helper

Create a study design canvas:

```bash
python3 flowly/skills/research-methods/scripts/study_design_canvas.py "study title" --out ./study-protocol.md
```

## References

- Use `references/design-canvas.md` to structure a protocol.
- Use `references/validity-threats.md` to audit a design.
