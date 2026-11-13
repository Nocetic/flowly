---
name: statistical-analysis
description: "Analyze scientific data: EDA, test choice, effect sizes, confidence intervals, reporting."
metadata: {"flowly":{"tags":["science","statistics","data-analysis","csv","methods","reproducibility"],"requires":{"bins":["python3"]},"category":"science","related_skills":["research-methods","reproducible-research","excel-author"]}}
---

# Statistical Analysis

Use this skill for scientific or experimental data analysis, especially when the user has CSV/Excel data, asks which statistical test to use, or wants a defensible result summary.

## Workflow

1. Clarify the analysis unit.
   - Subject, specimen, run, trial, sample, dataset, or paper.
   - Do not treat repeated measurements as independent without checking.
2. Inspect data before testing.
   - Missingness
   - Numeric ranges and impossible values
   - Group sizes
   - Outliers
   - Distribution shape
3. Match the test to the design.
   - Independent vs paired
   - Number of groups
   - Continuous, ordinal, count, binary, or time-to-event outcome
   - Parametric assumptions
4. Report effect sizes and intervals.
   - Do not report only p-values.
   - State uncertainty and practical meaning.
5. Correct for multiplicity when many hypotheses are tested.
6. Keep a reproducible trail: input file, script, parameters, output date.

## Helper

Run a dependency-free CSV profile:

```bash
python3 flowly/skills/statistical-analysis/scripts/analyze_csv.py data.csv --out analysis.md
```

Optional grouped summary:

```bash
python3 flowly/skills/statistical-analysis/scripts/analyze_csv.py data.csv --by treatment --outcome response
```

## References

- Use `references/test-selection.md` to choose a test.
- Use `references/reporting-checklist.md` before presenting results.
