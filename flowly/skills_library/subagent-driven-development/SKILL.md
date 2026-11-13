---
name: subagent-driven-development
description: "Execute plans via delegate_to subagents with two-stage review (spec compliance, then code quality)."
metadata: {"flowly":{"emoji":"🤝","tags":["delegation","subagent","implementation","workflow"],"related_skills":["writing-plans","requesting-code-review","test-driven-development"]}}
---

# Subagent-Driven Development

## Overview

Execute implementation plans by dispatching fresh subagents per task with systematic two-stage review.

**Core principle:** Fresh subagent per task + two-stage review (spec then quality) = high quality, fast iteration.

## When to Use

Use this skill when:
- You have an implementation plan (from writing-plans skill or user requirements)
- Tasks are mostly independent
- Quality and spec compliance are important
- You want automated review between tasks

**vs. manual execution:**
- Fresh context per task (no confusion from accumulated state)
- Automated review process catches issues early
- Consistent quality checks across all tasks
- Subagents can ask questions before starting work

## The Process

### 1. Read and Parse Plan

Read the plan file. Extract ALL tasks with their full text and context upfront. Track tasks in your working memory or todo state:

- Task 1: Create User model with email field — pending
- Task 2: Add password hashing utility — pending
- Task 3: Create login endpoint — pending

**Key:** Read the plan ONCE. Extract everything. Don't make subagents read the plan file — provide the full task text directly in context.

### 2. Per-Task Workflow

For EACH task in the plan:

#### Step 1: Dispatch Implementer Subagent

Use `delegate_to` with complete context. Example goal/instructions:

```
Implement Task 1: Create User model with email and password_hash fields.

TASK FROM PLAN:
- Create: src/models/user.py
- Add User class with email (str) and password_hash (str) fields
- Use bcrypt for password hashing
- Include __repr__ for debugging

FOLLOW TDD:
1. Write failing test in tests/models/test_user.py
2. Run: pytest tests/models/test_user.py -v (verify FAIL)
3. Write minimal implementation
4. Run: pytest tests/models/test_user.py -v (verify PASS)
5. Run: pytest tests/ -q (verify no regressions)
6. Commit: git add -A && git commit -m "feat: add User model with password hashing"

PROJECT CONTEXT:
- Python 3.11, Flask app in src/app.py
- Existing models in src/models/
- Tests use pytest, run from project root
- bcrypt already in requirements.txt
```

#### Step 2: Dispatch Spec Compliance Reviewer

After the implementer completes, verify against the original spec via a fresh `delegate_to` call:

```
Review whether the implementation matches the spec from the plan.

ORIGINAL TASK SPEC:
- Create src/models/user.py with User class
- Fields: email (str), password_hash (str)
- Use bcrypt for password hashing
- Include __repr__

CHECK:
- [ ] All requirements from spec implemented?
- [ ] File paths match spec?
- [ ] Function signatures match spec?
- [ ] Behavior matches expected?
- [ ] Nothing extra added (no scope creep)?

OUTPUT: PASS or list of specific spec gaps to fix.
```

**If spec issues found:** Fix gaps, then re-run spec review. Continue only when spec-compliant.

#### Step 3: Dispatch Code Quality Reviewer

After spec compliance passes, dispatch another fresh `delegate_to`:

```
Review code quality for Task 1 implementation.

FILES TO REVIEW:
- src/models/user.py
- tests/models/test_user.py

CHECK:
- [ ] Follows project conventions and style?
- [ ] Proper error handling?
- [ ] Clear variable/function names?
- [ ] Adequate test coverage?
- [ ] No obvious bugs or missed edge cases?
- [ ] No security issues?

OUTPUT FORMAT:
- Critical Issues: [must fix before proceeding]
- Important Issues: [should fix]
- Minor Issues: [optional]
- Verdict: APPROVED or REQUEST_CHANGES
```

**If quality issues found:** Fix issues, re-review. Continue only when approved.

#### Step 4: Mark Complete

Update task tracking — Task 1: completed.

### 3. Final Review

After ALL tasks are complete, dispatch a final integration reviewer via `delegate_to`:

```
Review the entire implementation for consistency and integration issues.

All tasks from the plan are complete. Review the full implementation:
- Do all components work together?
- Any inconsistencies between tasks?
- All tests passing?
- Ready for merge?
```

### 4. Verify and Commit

```bash
# Run full test suite
pytest tests/ -q

# Review all changes
git diff --stat

# Final commit if needed
git add -A && git commit -m "feat: complete [feature name] implementation"
```

## Task Granularity

**Each task = 2-5 minutes of focused work.**

**Too big:**
- "Implement user authentication system"

**Right size:**
- "Create User model with email and password fields"
- "Add password hashing function"
- "Create login endpoint"
- "Add JWT token generation"
- "Create registration endpoint"

## Red Flags — Never Do These

- Start implementation without a plan
- Skip reviews (spec compliance OR code quality)
- Proceed with unfixed critical/important issues
- Dispatch multiple implementation subagents for tasks that touch the same files
- Make subagent read the plan file (provide full text in context instead)
- Skip scene-setting context (subagent needs to understand where the task fits)
- Ignore subagent questions (answer before letting them proceed)
- Accept "close enough" on spec compliance
- Skip review loops (reviewer found issues → implementer fixes → review again)
- Let implementer self-review replace actual review (both are needed)
- **Start code quality review before spec compliance is PASS** (wrong order)
- Move to next task while either review has open issues

## Handling Issues

### If Subagent Asks Questions

- Answer clearly and completely
- Provide additional context if needed
- Don't rush them into implementation

### If Reviewer Finds Issues

- Implementer subagent (or a new one) fixes them
- Reviewer reviews again
- Repeat until approved
- Don't skip the re-review

### If Subagent Fails a Task

- Dispatch a new fix subagent with specific instructions about what went wrong
- Don't try to fix manually in the controller session (context pollution)

## Efficiency Notes

**Why fresh subagent per task:**
- Prevents context pollution from accumulated state
- Each subagent gets clean, focused context
- No confusion from prior tasks' code or reasoning

**Why two-stage review:**
- Spec review catches under/over-building early
- Quality review ensures the implementation is well-built
- Catches issues before they compound across tasks

**Cost trade-off:**
- More subagent invocations (implementer + 2 reviewers per task)
- But catches issues early (cheaper than debugging compounded problems later)

## Integration with Other Skills

### With writing-plans

This skill EXECUTES plans created by the writing-plans skill:
1. User requirements → writing-plans → implementation plan
2. Implementation plan → subagent-driven-development → working code

### With test-driven-development

Implementer subagents should follow TDD:
1. Write failing test first
2. Implement minimal code
3. Verify test passes
4. Commit

Include TDD instructions in every implementer context.

### With requesting-code-review

The two-stage review process IS the code review. For final integration review, use the requesting-code-review skill's review dimensions.

### With systematic-debugging

If a subagent encounters bugs during implementation:
1. Follow systematic-debugging process
2. Find root cause before fixing
3. Write regression test
4. Resume implementation

## Example Workflow

```
[Read plan: .flowly/plans/auth-feature.md]
[Track 5 tasks]

--- Task 1: Create User model ---
[delegate_to → implementer subagent]
  Implementer: "Should email be unique?"
  You: "Yes, email must be unique"
  Implementer: Implemented, 3/3 tests passing, committed.

[delegate_to → spec reviewer]
  Spec reviewer: PASS — all requirements met

[delegate_to → quality reviewer]
  Quality reviewer: APPROVED — clean code, good tests

[Mark Task 1 complete]

--- Task 2: Password hashing ---
[delegate_to → implementer subagent]
  Implementer: No questions, implemented, 5/5 tests passing.

[delegate_to → spec reviewer]
  Spec reviewer: FAIL — missing password strength validation (spec says "min 8 chars")

[Implementer fixes]
  Implementer: Added validation, 7/7 tests passing.

[delegate_to → spec reviewer again]
  Spec reviewer: PASS

[delegate_to → quality reviewer]
  Quality reviewer: Important — magic number 8, extract to constant
  Implementer: Extracted MIN_PASSWORD_LENGTH constant
  Quality reviewer: APPROVED

[Mark Task 2 complete]

... (continue for all tasks)

[After all tasks: delegate_to → final integration reviewer]
[Run full test suite: all passing]
[Done!]
```

## Remember

```
Fresh subagent per task
Two-stage review every time
Spec compliance FIRST
Code quality SECOND
Never skip reviews
Catch issues early
```

**Quality is not an accident. It's the result of systematic process.**
