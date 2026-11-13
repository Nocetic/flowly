---
name: brand-voice
description: "Define and enforce a brand voice — codify tone attributes, do/don't rules, vocabulary (words to use and avoid), and sentence style into a voice guide; then audit or rewrite copy to match it and check consistency across content. Use when the user wants to define a brand/writing voice, create a style guide, rewrite copy to fit a voice, check that text is on-brand, or make content sound consistent."
metadata: {"flowly":{"emoji":"🗣️","tags":["creator","brand-voice","tone","style-guide","copywriting","consistency","editing"],"requires":{"bins":[]},"category":"creator","related_skills":["newsletter-editor","video-scriptwriter","humanizer","internal-comms"]}}
---

# Brand Voice — Sound Like One Consistent Person

Brand voice is the personality that makes content recognizably *yours* across every channel and writer. The job has two modes: **codify** the voice into rules concrete enough that anyone can apply them, and **enforce** it by auditing/rewriting copy against those rules. Vague adjectives ("friendly, professional") aren't a voice — a voice is specific, exemplified, and has explicit do/don'ts.

## What this skill produces

**Chat-first.** Default: either a **voice guide** (tone attributes with do/don't examples, lexicon, style rules) or a **rewrite/audit** of supplied copy against the voice (the on-brand version + what changed and why). Keep it concrete — every rule paired with a before/after.

## When to use

- "Define our brand voice / writing style." / "Create a voice/style guide."
- "Rewrite this to match our voice." / "Make this on-brand."
- "Is this copy on-brand?" / "Audit this for voice consistency."
- "Make our content sound consistent across writers/channels."

## Codifying a voice (make it concrete)

A useful voice guide has:
1. **3–4 tone attributes, each defined and bounded.** Not just "friendly" but "**Friendly** — we write like a helpful colleague, not a corporation. *We do:* use 'you' and 'we', contractions, warmth. *We don't:* use slang that excludes, or fake hype." Pair each attribute with a **"this not that"**: "*Confident, not arrogant*", "*Playful, not unprofessional*". The boundaries prevent caricature.
2. **Voice vs tone:** voice is constant (the personality); tone flexes by context (an error message is more reassuring; a launch is more energetic). Note how the voice adapts to situations.
3. **Lexicon:** words/phrases to **use** (signature terms, how you refer to the product/users) and to **avoid** (jargon, banned clichés, competitor framing, words that feel off-brand). A do/don't word list is one of the most enforceable parts.
4. **Style mechanics:** sentence length/rhythm, formality, person (1st/2nd), oxford comma, emoji policy, capitalization, how you handle humor. Decide the small things so they're consistent.
5. **Examples:** the same message written on-brand vs off-brand — examples teach faster than rules.

## Auditing / rewriting to a voice

Given a voice (guide, or inferred from samples) and copy:
- **Read against the attributes** — where does it drift (too stiff, too hype, wrong person, banned words)?
- **Rewrite preserving meaning** but shifting tone, lexicon, and rhythm to match. Don't change the facts/structure unless asked — change the *voice*.
- **Show the diff/rationale** — "swapped 'utilize'→'use' (plain-language rule), cut the exclamation (we're confident, not shouty), added 'you' (we address the reader directly)." The rationale teaches the voice.
- **Flag genuine conflicts** — if a requirement (legal disclaimer, technical accuracy) fights the voice, keep accuracy and note it.
- **Inferring a voice from samples:** if no guide exists, extract the pattern from 2–3 on-brand examples (tone, lexicon, rhythm) and state your read before applying it.

## Consistency across content

- **Same voice, channel-appropriate tone** — Twitter, docs, email, and error messages share the personality but flex formality/length.
- **The "one person" test:** could all this content plausibly come from the same person? If pieces clash, the voice isn't being applied.
- For longer programs, the voice guide becomes the reference writers/AI check against. (For making AI-generated text read more human/natural, pair with `humanizer`; for internal-comms formats, `internal-comms`.)

## Chat output format

```
**Voice guide — <brand>** (sketch)

Attributes:
  • Plain-spoken — like a smart friend, not a textbook. Do: short sentences,
    "you/we", everyday words. Don't: jargon, "leverage/utilize", passive voice.
  • Confident, not arrogant — make claims plainly; no hype/!!!, no overpromising.
  • Warm, not cutesy — friendly and human; no forced memes or excessive emoji.
Lexicon — use: "build", "ship", "your workspace". Avoid: "synergy", "revolutionary", "users" (say "you").
Style: 2nd person, contractions, oxford comma, ≤1 emoji per post, sentence-case headers.

Rewrite example:
  ✗ "Our revolutionary platform empowers users to leverage synergies!"
  ✓ "Flowly helps you get more done — without the busywork."
  (cut hype + jargon, addressed 'you' directly, plain verbs)
```

## Workflow

1. **Codify or infer the voice** — 3–4 bounded attributes ("this not that"), lexicon (use/avoid), style mechanics, examples. If only samples exist, extract and state the pattern.
2. **For a rewrite/audit:** read the copy against the attributes; identify drift.
3. **Rewrite** preserving meaning, shifting tone/lexicon/rhythm to match; **show the rationale**.
4. **Flag conflicts** (accuracy/legal vs voice) and resolve toward correctness.
5. **Check consistency** with the one-person test across pieces/channels.
6. **Deliver** the guide or rewritten copy + rationale; apply across `newsletter-editor`, `video-scriptwriter`, `internal-comms`; humanize AI text with `humanizer`.

## Key pitfalls

- **Vague attributes.** "Friendly, professional" describes everything and guides nothing — bound each with do/don't and "this not that".
- **No examples.** Rules without before/after are hard to apply — show on-brand vs off-brand.
- **Confusing voice and tone.** Voice is constant; tone flexes by context — say how it adapts, don't make it rigid.
- **Caricature.** Over-applying an attribute ("playful" → cringe) — boundaries keep it from tipping over.
- **Changing meaning in a rewrite.** Shift the *voice*, not the facts/structure (unless asked).
- **Ignoring accuracy/legal constraints** in pursuit of voice — correctness wins; note the tension.
- **No enforceable lexicon.** A use/avoid word list is one of the most practical tools — include it.
- **Inconsistency across channels.** Same personality everywhere; failing the one-person test means it's not really a voice.

## Quick reference

- Codify: 3–4 bounded tone attributes ("this not that") + lexicon (use/avoid) + style mechanics + examples.
- Voice = constant personality; tone = context flex. Define both.
- Rewrite/audit: spot drift vs attributes → rewrite preserving meaning → show the rationale → flag accuracy conflicts.
- "One person" test for cross-channel consistency; channel-appropriate tone, same voice.
- Apply across newsletter-editor / video-scriptwriter / internal-comms; humanize AI text via humanizer.
