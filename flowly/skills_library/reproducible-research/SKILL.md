---
name: reproducible-research
description: "Reproducible research: environment capture, data provenance, seeds, manifests, rerun reports."
metadata: {"flowly":{"tags":["science","reproducibility","research","data","environment","audit"],"requires":{"bins":["python3"]},"category":"science","related_skills":["statistical-analysis","paper-deep-dive","lab-notebook","scientific-peer-review"]}}
---

# Reproducible Research

Use this skill when the user wants an analysis, experiment, paper result, dataset, or codebase to be reproducible or independently auditable.

## Workflow

1. Capture the claim to reproduce.
   - Result, figure, table, metric, or conclusion.
2. Record inputs.
   - Data files, source URLs, preprocessing steps, exclusions, labels, instruments.
3. Capture environment.
   - OS, Python/runtime, package files, git commit, scripts, model versions.
4. Make execution deterministic where possible.
   - Seeds
   - Fixed splits
   - Versioned data
   - Stable random number generators
5. Create a rerun path.
   - Single command or short sequence
   - Expected outputs
   - Tolerance for numeric differences
6. Write a reproduction report.
   - What matched
   - What differed
   - What could not be checked
   - What is missing from the original source

## Helper

Generate a local reproducibility audit:

```bash
python3 flowly/skills/reproducible-research/scripts/repro_audit.py . --out ./repro-audit
```

## References

- Use `references/repro-checklist.md` before calling work reproducible.
- Use `references/reproduction-report.md` for final reports.
