---
name: scientific-peer-review
description: "Review papers, grants, datasets, and analyses for scientific rigor and reproducibility."
metadata: {"flowly":{"tags":["science","peer-review","review","methods","reproducibility","papers"],"requires":{"bins":["python3"]},"category":"science","related_skills":["paper-deep-dive","research-methods","statistical-analysis","reproducible-research"]}}
---

# Scientific Peer Review

Use this skill when the user asks for a scientific review, referee report, grant critique, dataset review, methods critique, or claim-vs-evidence assessment.

## Review Stance

Lead with substantive issues. Do not spend the report on style while methods, evidence, or reproducibility problems remain.

## Workflow

1. Identify the artifact type.
   - Article, preprint, grant, dataset, benchmark, code, protocol, analysis, poster, or slide deck.
2. Extract the central claims.
3. Evaluate methods against the claims.
4. Check evidence quality.
5. Check statistics and uncertainty.
6. Check reproducibility and data availability.
7. Separate fatal issues, major issues, minor issues, and questions.
8. End with a clear recommendation only when the user asks for one.

## Helper

Create a review report scaffold:

```bash
python3 flowly/skills/scientific-peer-review/scripts/review_template.py "Manuscript title" --type article --out review.md
```

## References

- Use `references/review-rubric.md` while reviewing.
- Use `references/recommendation-scale.md` for accept/revise/reject decisions.
