# Paper Matrix Schema

Use these columns for `papers.csv`.

| Column | Purpose |
| --- | --- |
| `id` | Stable local id, DOI, PMID, arXiv id, or short citation key. |
| `title` | Paper title. |
| `year` | Publication or preprint year. |
| `authors` | First author or author list. |
| `venue` | Journal, conference, preprint server, report source. |
| `source_url` | URL, DOI URL, arXiv abs URL, or local file path. |
| `study_type` | Experiment, observational, simulation, benchmark, review, theory, case study. |
| `sample_or_dataset` | Participants, specimens, datasets, tasks, simulations, or corpora. |
| `method` | Main method, intervention, model, assay, instrument, or design. |
| `outcomes` | Primary outcomes or metrics. |
| `main_claim` | The authors' central claim in one sentence. |
| `evidence_strength` | High, medium, low, or unclear. |
| `limitations` | Internal threats, missing controls, small sample, bias, confounds. |
| `relevance` | Core, supporting, adjacent, or background. |
| `notes` | Brief extraction notes. |

Keep extraction factual. Save interpretation for `evidence-map.md` and `synthesis.md`.
