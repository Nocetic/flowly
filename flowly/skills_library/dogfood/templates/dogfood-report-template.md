# QA Sweep — {target_url}

- **Scope covered:** {scope_description}
- **Run on:** {date}
- **Method:** automated exploratory browser QA (Flowly)

## At a glance

{one_or_two_sentence_verdict}

| Severity | Count |
|----------|------:|
| 🔴 Critical | {critical_count} |
| 🟠 High | {high_count} |
| 🟡 Medium | {medium_count} |
| 🔵 Low | {low_count} |
| **Total** | **{total_count}** |

## Findings

<!-- One block per finding, Critical first. Drop the block entirely if none. -->

### {issue_number}. {issue_title}  ·  {severity} / {category}

**Where:** {url_where_found}

**What happens:** {description}

**Reproduce:**
1. {step_1}
2. {step_2}

**Expected:** {expected_behavior}
**Got:** {actual_behavior}

![evidence](MEDIA:{screenshot_path})

<!-- Include only if the console showed something: -->
**Console:**
```
{console_error_output}
```

---

## All findings

| # | Finding | Severity | Category | URL |
|--:|---------|----------|----------|-----|
| {n} | {title} | {severity} | {category} | {url} |

## Coverage

- **Exercised:** {pages_and_flows_actually_tested}
- **Not reached:** {areas_skipped_and_why}
- **Blockers:** {anything_that_stopped_testing_a_path}

## Other observations

{notes_recommendations_or_patterns_worth_flagging}
