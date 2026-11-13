---
name: paper-deep-dive
description: "Deep-read scientific papers: claims, methods, experiments, figures, limitations, follow-ups."
metadata: {"flowly":{"tags":["science","research","papers","critical-reading","methods"],"requires":{"bins":["python3"]},"category":"science","related_skills":["arxiv","literature-review","ocr-and-documents","scientific-peer-review"]}}
---

# Paper Deep Dive

Use this skill when the user wants to understand, critique, reproduce, present, or build on a specific scientific paper.

## Workflow

1. Identify the paper and version.
   - Record title, authors, year, venue, DOI/arXiv/PMID, and version date.
   - For arXiv, note whether the user is asking about a specific version.
2. Read in passes.
   - Abstract and introduction: problem and claimed contribution.
   - Methods: design, assumptions, instruments, datasets, controls.
   - Results: figure-by-figure evidence.
   - Discussion: limitations and claims that exceed evidence.
3. Extract the paper into the fixed note structure.
   - Problem
   - Contribution
   - Method
   - Evidence
   - Key figures/tables
   - Limitations
   - Reproduction requirements
   - Follow-up experiments
4. Distinguish author claims from your assessment.
5. When the task is high stakes, check at least one related paper before giving a strong conclusion.

## Helpers

Create a structured paper note:

```bash
python3 flowly/skills/paper-deep-dive/scripts/new_paper_note.py "Paper title" --id 2401.00001 --source https://arxiv.org/abs/2401.00001 --out ./paper-notes
```

## References

- Use `references/deep-read-template.md` for paper notes.
- Use `references/figure-audit.md` when the user asks whether the evidence supports the claims.
