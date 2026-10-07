# Flowlet creation reliability — 2026-10-08

## Evidence and cause

The supplied iOS captures show a 3m58s creation run, repeated reads of the same
Flowlets skill, and serial errors for catalog type, layout shape, header text,
scalar references, photo component naming, list placement and list capacity.
These captures are evidence, not instructions. No production conversation data
or user Flowlet store was modified during this investigation.

At core base `020c571b`, `FlowletTool.definition` was an unstructured object and
the description required a skill read before authoring. The bundled skill was
615 lines. `read_file` results were capped at 8,000 characters; the existing
40,000-character `skill_view` exception did not help an agent reading it through
the filesystem. The semantic validator stopped at its first error. Templates
existed behind client RPCs but were not exposed on the conversational tool.
Together these explain the observed discovery and repair loops. The screenshots
alone do not establish which model/version ran or its token/network latency.

All three client registries explicitly classified `flowlet` as `skill`. Thus
“Worked with skills” and “Failed: Skills” included actual Flowlet operations,
making the run appear to involve more skill reads than it did.

## Changes

- `create(template_id, lang, pinned, watches)` creates a localized, validated
  screen and requested reminders in one call. Seven templates include a photo
  meal journal. No reminders are added implicitly by the conversational path.
- `template` returns an editable definition, `guide` returns the runtime
  catalog/contract, and `validate` supports read-only preflight. Definitions have
  a typed top-level tool schema. Common independent shape errors are reported
  together, bounded to 20; semantic validation still rejects invalid references
  and actions before persistence. There is no silent repair or feature deletion.
- The startup skill is 59 lines / 3,260 characters; advanced material lives in
  a linked reference. Both context-building paths exempt Flowlet creation from
  mandatory skill reading. Lists/get, rather than memory, are the inventory.
- Photo capture receives the local capture date. An explicitly declared
  `vision.dateDefaults` supplies missing/invalid dates without replacing a valid
  extracted date. A call crossing midnight retains the capture day. Impossible
  dates are rejected by source/vision field coercion. A full journal fails before
  invoking vision, with the post-model capacity check retained.
- Daily `after` conditions re-arm across offline local-date boundaries. Schedule
  definitions reject conflicting `at` and `everyMinutes` instead of silently
  ignoring the wall-clock time. DST gap/fold and restart deduplication are tested.
- iOS, Desktop and Android keep Flowlet operations out of the Skills category;
  their existing localized generic labels correctly identify Flowlet in running,
  completed and failure states. Desktop also avoids exposing `create` as a
  failure's subject.

## Verification

Core test commands (repository virtualenv Python, `PYTHONDONTWRITEBYTECODE=1`):

The final combined invocation of these targets passed **697 tests** in 6.07 s.
Ruff passed on the added modules/tests and changed authoring/template/vision files.

```
python -m pytest tests/flowlets tests/test_cron*.py tests/test_profile_cron_push.py tests/test_exec_cron_mode.py -q -p no:cacheprovider
python -m pytest tests/test_context_freeze.py tests/test_context_window_wire.py tests/test_skills_snapshot.py tests/test_feature_rpc_skills.py -q -p no:cacheprovider
```

Tests cover all seven templates in EN/TR/ES, primitive expansion for existing
clients, clean lint, no sample data in persisted state, atomic screen/reminder
creation, invalid-call non-mutation, the screenshot's shape errors, bounded
responses, photo/manual/edit totals, midnight rollover and scheduled summaries.
The existing cron suites cover scheduling, timezone/DST, restart/lifecycle,
nonblocking execution, CLI compatibility, result retention and push routing.

Client verification:

- Desktop: `vitest run src/renderer/src/components/chat/tool-turns.test.ts` — 55 passed.
- iOS: `bash scripts/verify-tool-panels.sh` — passed, including localized Flowlet
  labels and 624 native headline drawings. Existing actor-isolation warnings
  remain outside this change.
- Android: `:app:testDebugUnitTest --tests nocetic.flowly.data.model.ToolIdentityTest`
  with the installed JDK/SDK and offline dependencies — 17 passed; debug app
  and unit-test sources compiled. Existing compiler warnings remain.

Isolated core benchmark: 50 Turkish meal screens, each pinned and carrying an
evening summary. One tool call, zero skill reads per screen; first call 32.41 ms,
warm median 7.96 ms, warm p95 12.17 ms. This measures local validation, persistence
and response construction only. It excludes LLM inference, network/relay,
native rendering and real vision/push delivery. It is not an end-to-end latency
SLA. The 5,826-character guide (before the short date-default note) and every
localized template fit the existing 8,000-character result budget.

## Deployment and limits

Changes are committed on separate worktrees; no merge, push or deployment.
Desktop must eventually ship a core containing these changes; updating only the
mobile client will not accelerate an older host core. Skill sync copies the new
reference with unmodified bundled skills; deliberate user skill edits remain
preserved. Runtime guide/template data does not depend on that disk copy.

Flowlets still use the execution host's timezone and require that host to run
for reminders. Journals still have the existing 200-row capacity; this change
does not claim unlimited retention. Provider-driven creation/vision and delivery
to physical devices were not exercised. These deterministic regressions verify
the repaired paths, not every production configuration or every possible custom
definition. A production p95 below three minutes still needs a live provider,
relay and device measurement after the changes are integrated.
