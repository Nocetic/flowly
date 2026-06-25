<!-- Keep PRs focused: one logical change. See CONTRIBUTING.md. -->

## What & why

<!-- What does this change, and why? Link any related issue (#123). -->

## How to test

<!-- Repro steps for a bug, or usage for a feature. Note OS/Python if relevant. -->

## Checklist

- [ ] `ruff check flowly/` passes
- [ ] `pytest` passes (new behavior has a test where it makes sense)
- [ ] One logical change — no unrelated refactors mixed in
- [ ] Conventional commit title (`type(scope): description`)
- [ ] If this touches exec, file paths, credentials, or the sandbox, I flagged it and tested the affected platforms
