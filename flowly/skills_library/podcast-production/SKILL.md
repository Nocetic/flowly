---
name: podcast-production
description: "Support podcast production end to end — episode briefs and angles, guest research and tailored question prep, recording/structure guidance, and post-production assets: show notes, timestamped chapters, episode titles and descriptions, quotable clips, and social promo. Use when the user is planning or producing a podcast episode, needs guest research, an interview question list, show notes, chapters, an episode title/description, or clips from a transcript."
metadata: {"flowly":{"emoji":"🎙️","tags":["creator","podcast","show-notes","interview","guest-research","clips","audio"],"requires":{"bins":[]},"category":"creator","related_skills":["seo-podcast-optimizer","video-scriptwriter","newsletter-editor","summarize"]}}
---

# Podcast Production — From Guest Prep to Show Notes

Great episodes come from **preparation** (a clear angle + questions that get the guest past their canned answers) and great reach comes from **post-production assets** (discoverable title, useful show notes, chapters, and clips that travel). This skill covers both ends — the planning before the mic and the packaging after. For SEO-tuned titles/descriptions specifically, pair with `seo-podcast-optimizer`.

## What this skill produces

**Chat-first.** Default: the requested asset — an episode brief, a tailored question list, show notes with chapters, title/description options, or pulled clips from a transcript. The bot can ingest transcripts directly, which makes post-production a natural fit.

## When to use

- "Plan an episode about \<topic\>." / "Episode brief / angle?"
- "Research this guest." / "Interview questions for \<guest\>."
- "Write show notes / chapters / timestamps for this episode."
- "Episode title and description." / "Pull clips / quotes from this transcript."
- "What should I ask to make this interview good?"

## Pre-production

**Episode brief:** the angle (the *one* thing this episode is about — not "a chat with X" but "how X rebuilt their company after failing twice"), the target listener and what they'll take away, and the arc. A focused angle beats a meandering conversation.

**Guest research:** their background, recent work/news, prior interviews (to *avoid* the same questions they always get), strong opinions/contrarian takes, and personal stories. The goal is questions only *this* guest can answer interestingly.

**Question prep:**
- Open with an easy, warm question to settle in; save the hardest/most personal for after rapport.
- **Ask open, specific questions** ("Tell me about the moment you decided to quit") over yes/no or generic ("Tell me about your journey").
- Prepare ~10–15 but hold them loosely — **follow the interesting tangent**; the best moments are unscripted. Have follow-ups ready ("why?", "what did that feel like?").
- Avoid multi-part questions (the guest answers one and forgets the rest).

## Recording structure (briefly)

Cold open / hook → intro (who, why this episode) → main conversation (the arc) → recurring segments if any → wrap + CTA (subscribe, links). Record a few seconds of room tone and do a level check; capture the hook to use as a cold open.

## Post-production assets

- **Show notes:** a 1–2 paragraph episode summary (the value, not a transcript), key topics/takeaways as bullets, **timestamps**, guest bio + links, resources mentioned, and CTAs. Make it skimmable and useful on its own. (Use `summarize` to digest the transcript.)
- **Chapters / timestamps:** segment the episode at topic shifts (`00:00 Intro`, `04:30 The first failure`, …) — improves listener navigation and is a YouTube/Spotify ranking signal.
- **Title:** specific + curiosity, names the guest if they're a draw, front-loads the hook. Offer several; tune for search with `seo-podcast-optimizer`.
- **Description:** hook line → what listeners learn → guest + links → timestamps → CTA. First ~125 chars matter most (preview).
- **Clips:** pull 3–6 self-contained, punchy moments (30–90s) — a complete thought with a hook, ideal for shorts/social. Note the timestamp and why it works. (Script social posts with `video-scriptwriter`.)
- **Quotes:** 3–5 tweetable/quote-card lines.

## Chat output format

```
**Episode 17 — assets**

Title options:
  1. "How Maria rebuilt after two failed startups" (story + guest)
  2. "The failure that taught me everything about hiring"
Description: Maria spent 6 years and two collapses learning what most founders
  never do... [what you'll learn] · Guest: Maria L., founder of X · [links]

Chapters: 00:00 Cold open · 01:10 Intro · 03:40 The first failure ·
  18:20 What she'd do differently · 35:00 Hiring lessons · 41:00 Wrap
Show notes: <1-para summary> + 5 takeaway bullets + resources.
Clips: 18:45–19:50 "the hiring mistake" (complete thought, strong hook);
  35:10–36:00 "fire fast" (contrarian, punchy).
```

## Workflow

1. **Pre:** define the angle + listener takeaway (brief); research the guest for *unique* questions; prep open, specific questions held loosely.
2. **Record:** hook → intro → arc → wrap+CTA; follow tangents; grab a cold-open moment.
3. **Post (from transcript):** show notes + chapters (`summarize` to digest), title/description options, clips, quotes.
4. **Optimize discovery** with `seo-podcast-optimizer`; **repurpose** clips via `video-scriptwriter`, recap in `newsletter-editor`.
5. **Deliver** the requested asset(s), skimmable and ready to publish.

## Key pitfalls

- **No angle.** "A conversation with X" meanders; commit to the one thing the episode is about.
- **Generic/recycled questions.** "Tell me about your journey" wastes a guest — research what only they can answer and what they're tired of being asked.
- **Yes/no or multi-part questions.** Kill momentum; ask open, single, specific questions with follow-ups.
- **Over-scripting.** Reading questions robotically misses the gold — hold the list loosely and chase tangents.
- **Show notes = transcript.** A wall of text isn't useful; summarize with value + timestamps + links.
- **No chapters.** Hurts navigation and discoverability — segment at topic shifts.
- **Weak title/description.** Generic titles bury good episodes; front-load hook + specificity (→ seo-podcast-optimizer).
- **Clips that need context.** Pull self-contained moments with their own hook, or they flop as shorts.

## Quick reference

- Pre: angle (the one thing) + listener takeaway; guest research for unique questions; open/specific Qs, held loosely, with follow-ups.
- Structure: cold-open hook → intro → arc → wrap+CTA.
- Post: show notes (value summary + takeaways + timestamps + links), chapters at topic shifts, title (specific+curious), description (hook in first ~125 chars), 3–6 self-contained clips, quote lines.
- Digest transcript → summarize; SEO title/desc → seo-podcast-optimizer; clips/shorts → video-scriptwriter; recap → newsletter-editor.
