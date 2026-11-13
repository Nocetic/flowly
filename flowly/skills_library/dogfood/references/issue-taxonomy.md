# Grading findings: severity × category

Every finding gets two independent labels. **Severity** answers "how much
does this hurt the user?" **Category** answers "what kind of problem is
it?" A single bug always has exactly one of each — a double-charge at
checkout is Critical (severity) and Functional (category); a typo is Low
and Content.

When a finding could sit in two severity buckets, pick the higher one and
explain the reasoning in the report. When in doubt between two
categories, choose the one a developer would file it under.

## Severity — how much it hurts

**Critical** — a core promise of the app is broken, or the user is
actively harmed. There is no workaround.
- the page renders blank or the app hard-crashes
- submitted data is silently dropped
- nobody can authenticate
- a payment completes the charge but not the order (or charges twice)
- a security hole: reflected/stored XSS, secrets printed to the console,
  auth tokens in the URL

**High** — a primary feature is broken or wrong, though a determined user
might find a way around it.
- a key control does nothing (until a reload "fixes" it)
- valid searches return nothing
- validation rejects legitimate input
- a nav link dead-ends in a 404 or the wrong destination
- uncaught exceptions fire on a core page

**Medium** — the user notices and is slowed or annoyed, but can still get
the job done.
- overlapping or misaligned layout in part of the page
- broken image references
- loads that visibly stall (multi-second waits with no indicator)
- bad input is rejected but with no message explaining why
- styling that drifts between otherwise-matching pages

**Low** — cosmetic or hygiene issues with no functional impact.
- typos and grammar slips
- a pixel or two of misalignment
- leftover placeholder text ("Lorem ipsum", "TODO")
- a missing favicon
- debug/info logging shipped to production
- contrast that's slightly off but still passes WCAG AA

## Category — what kind of problem it is

**Functional** — the behavior is wrong. Dead controls, forms that don't
submit or submit garbage, journeys that can't be completed, wrong data on
screen, half-working features.

**Visual** — it renders wrong. Overlapping elements, collapsed grids,
broken media, responsive breakpoints that fall apart, content hidden
behind other content, text that overflows or gets clipped.

**Accessibility** — it excludes users with disabilities. Meaningful images
without alt text, contrast below WCAG AA, controls unreachable by
keyboard, missing labels or ARIA, invisible focus rings, content a screen
reader can't parse.

**Console** — the browser is complaining. Uncaught exceptions, unhandled
promise rejections, failed network calls (4xx/5xx), CORS failures, mixed
HTTP-on-HTTPS warnings, deprecation notices, noisy leftover logging.

**UX** — it works but it's a bad experience. Confusing navigation, no
feedback after an action, missing loading states, inconsistent
interaction patterns, no confirmation before destructive actions, error
messages that don't help the user recover.

**Content** — the words or information are wrong. Typos, placeholder copy
in production, stale facts, empty sections that should have content, dead
external links, misleading labels.
