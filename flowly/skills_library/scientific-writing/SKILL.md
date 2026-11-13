---
name: scientific-writing
description: "Write scientific manuscripts, abstracts, rebuttals, related work, figure captions, limitations."
metadata: {"flowly":{"tags":["science","writing","manuscript","abstract","rebuttal","papers"],"requires":{"bins":["python3"]},"category":"science","related_skills":["literature-review","paper-deep-dive","scientific-peer-review","humanizer"]}}
---

# Scientific Writing

Use this skill for scientific manuscripts, preprints, abstracts, related work, limitations sections, figure captions, reviewer responses, and grant-style technical narratives.

## Writing Principles

1. Claims must map to evidence.
2. The reader should know what is new, what is measured, and what remains uncertain.
3. Methods and results should not be mixed.
4. Limitations should be specific, not generic.
5. Avoid hype words unless the evidence warrants them.
6. Prefer precise verbs: measured, estimated, observed, compared, tested, simulated.

## Workflow

1. Identify the artifact: abstract, manuscript section, full outline, rebuttal, caption, or related work.
2. Extract the target claim and evidence.
3. Pick the structure.
4. Draft with calibrated language.
5. Audit for unsupported claims.
6. Add limitations and future work when appropriate.

## Helper

Create a manuscript or rebuttal outline:

```bash
python3 flowly/skills/scientific-writing/scripts/outline_manuscript.py "Title" --kind article --out outline.md
```

## References

- Use `references/imrad-outline.md` for article structure.
- Use `references/rebuttal-template.md` for reviewer responses.
- Use `references/caption-checklist.md` for figures and tables.
