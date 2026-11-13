---
name: lab-notebook
description: "Maintain scientific lab notes: protocols, observations, deviations, raw data links, next runs."
metadata: {"flowly":{"tags":["science","lab-notebook","experiments","protocols","research","notes"],"requires":{"bins":["python3"]},"category":"science","related_skills":["research-methods","reproducible-research","llm-wiki","obsidian"]}}
---

# Lab Notebook

Use this skill when the user wants to plan, record, audit, or summarize experiments or research sessions.

## Rules

1. Separate plan, observation, result, and interpretation.
2. Record deviations from protocol immediately.
3. Link raw data instead of copying snippets without provenance.
4. Record instrument/software versions when they affect the result.
5. Preserve failed runs; they are part of the scientific record.
6. End each entry with concrete next actions.

## Helper

Create a new dated lab notebook entry:

```bash
python3 flowly/skills/lab-notebook/scripts/new_entry.py --project ./my-study --title "pilot run"
```

## References

- Use `references/entry-template.md` for notes.
- Use `references/protocol-template.md` before running an experiment.
