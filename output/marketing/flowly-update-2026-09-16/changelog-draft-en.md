# Flowly — upcoming update

Draft release notes. Version and release date to be assigned when the release is prepared.

- New — Advanced MCP connections: easier setup and authorization, permissions you can review in chat, and more reliable connection recovery. Manage connections for individual bots from desktop, iOS, and Android.
- New — Flowly Bots: keep separate memory, models, providers, skills, and integrations for each agent. Bring bots into group conversations and let them hand work to one another.
- New — Connect Gmail separately for each bot, with browser-based sign-in and connection management across desktop and mobile.
- Improvements — Redirect an active task with a follow-up message without stopping the tools already running.
- Improvements — Follow delegated work and return to its saved results across desktop, iOS, and Android.
- Fixes — More reliable chat streaming, tool progress, connection setup, and bot selection, alongside smaller interface fixes.

## Editorial notes

The Bots/group/delegation capabilities are already documented in the September 4, 2026 CLI 3.2.0 changelog. They remain here because the requested announcement covers the broader app update. Do not present them as a second new CLI release.

The MCP, Gmail, active-task steering, and mobile reliability items are supported by subsequent local commits. Their presence in source code does not confirm App Store, Play Store, or desktop distribution availability. The social post therefore says “coming” and does not assign a release number.

Excluded to keep this announcement focused: icon choices, animations, gestures, detailed panel changes, backend billing changes, and voice rollout work.

## Source references

- Canonical changelog: `/Users/hakanoren/flowly-app/content/changelog/releases.json`, latest entry 2026-09-04 / CLI 3.2.0.
- Web visual design: `/Users/hakanoren/flowly-app/app/flowly-editorial.css`, `/Users/hakanoren/flowly-app/components/site/editorial-hero.tsx`, and `/Users/hakanoren/flowly-app/BRAND.md`.
- Agent core: `54422257` (isolated MCP/Gmail), `3963ca8e` (connection recovery), `573b208d` (consent/input routing), `c20d8a2f` (active-run steering), `90cb3937` (delegated activity/results).
- Desktop: `1c252fb8` (profile Gmail), `7b62466f` (in-chat MCP requests), `524e8d38` (saved/live tool outcomes).
- iOS: `e6acbb0b` (profile Gmail), `c3bf5f6a` (remote profile MCP), `e63a86e8` (delegated activity/results), `dd2ef8ad` (stream reliability).
- Android: `1bcaa89` (profile Gmail), `12dd37a` (remote profile MCP), `66d9ec8` (delegated activity), `a004ccf` (selected agent retention).
