---
name: literature-review
description: "Systematic literature reviews: search strategy, screening, paper matrix, evidence map, synthesis."
metadata: {"flowly":{"tags":["science","research","literature-review","papers","evidence","synthesis"],"requires":{"bins":["python3"]},"category":"science","related_skills":["arxiv","paper-deep-dive","llm-wiki","summarize","scientific-writing"]}}
---

# Literature Review

Use this skill for systematic or semi-systematic reviews, related work sections, evidence maps, and "what does the literature say about X?" tasks.

## Workflow

1. Scope the review question before searching.
   - Population/system/domain
   - Intervention/method/exposure
   - Comparator/baseline, if any
   - Outcomes/metrics
   - Time range and accepted source types
2. Write the search strategy explicitly.
   - Databases searched
   - Query strings
   - Date of search
   - Inclusion and exclusion criteria
3. Build a paper matrix before synthesizing.
   - Do not jump from paper summaries to conclusions.
   - Track method, dataset/sample, metrics, claims, limitations, and relevance.
4. Screen in two passes.
   - Title/abstract screen for obvious misses.
   - Full-text screen for papers that may affect the answer.
5. Synthesize by evidence pattern, not by paper order.
   - Group converging findings.
   - Separate direct evidence from adjacent evidence.
   - Mark conflicts, weak evidence, and open questions.
6. Produce a final answer with provenance.
   - State search boundaries.
   - Cite the strongest sources.
   - Avoid stronger claims than the evidence supports.

## Helpers

Create a review workspace:

```bash
python3 flowly/skills/literature-review/scripts/make_review_workspace.py "topic name" --out ./review-topic
```

Then fill:

- `papers.csv` for the source matrix
- `screening.md` for inclusion/exclusion decisions
- `evidence-map.md` for grouped findings
- `synthesis.md` for the final narrative

## References

- Use `references/review-checklist.md` before finalizing the review.
- Use `references/matrix-schema.md` when building or auditing the paper matrix.
