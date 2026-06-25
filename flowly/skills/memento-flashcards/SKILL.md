---
name: memento-flashcards
description: "Spaced-repetition flashcards: create from facts or text, chat-grade free-text answers, quiz from YouTube transcripts, export/import CSV."
metadata: {"flowly":{"emoji":"🧠","platforms":["macos","linux"],"tags":["education","flashcards","spaced-repetition","learning","quiz","youtube"],"requires":{"bins":["python3"]},"install":[{"id":"pip-yt","kind":"pip","package":"youtube-transcript-api","label":"Install youtube-transcript-api (pip, for YouTube quiz only)"}],"related_skills":["youtube-content","summarize","llm-wiki"]}}
---

# Memento Flashcards

A self-contained, file-backed flashcard system with adaptive review scheduling. The agent
authors the card content and grades free-text answers conversationally; a small Python helper
owns persistence and the scheduling math. No network services or API keys are involved.

## What this skill does

- Captures a fact as a question/answer card and files it into a named collection.
- Runs interactive review sessions: the agent shows the prompt, reads the user's typed answer,
  judges it, reveals the correct answer, and reschedules the card.
- Builds a five-item quiz from a YouTube video's transcript.
- Moves card data in and out of plain CSV, and reports deck statistics.

Tone for everything the user sees: plain text, no Markdown. Feedback during review and quizzing
is short and matter-of-fact — no cheerleading, no padding.

## When it applies

Reach for this skill when the request is about memorizing or reviewing knowledge:

- saving facts to study later
- working through cards that have come due
- turning a YouTube video into quiz questions
- moving, listing, or pruning stored cards

Leave it alone for coding, general questions, or ordinary conversation.

## Intent map

| User says | What you do |
|---|---|
| "remember that…", "save this card", "add a flashcard" | author a Q/A pair, run `memento_cards.py add` |
| a bare fact, no flashcard wording | offer: "Want me to save this as a Memento flashcard?" — act only on a yes |
| "make a flashcard" (no content yet) | collect question, answer, collection; run `add` |
| "review my cards", "quiz me on my deck" | run `due`, walk the cards one at a time |
| "quiz me on <YouTube link>" | `youtube_quiz.py fetch <id>`, write 5 questions, run `add-quiz` |
| "export my cards" | `memento_cards.py export --output <path>` |
| "import these cards" | `memento_cards.py import --file <path> --collection <name>` |
| "how many cards do I have" | `memento_cards.py stats` |
| "delete that card" | `memento_cards.py delete --id <id>` |
| "drop that whole collection" | `memento_cards.py delete-collection --collection <name>` |

## Where data lives

Every card is kept in one JSON document:

```
~/.flowly/skills/memento-flashcards/data/cards.json
```

It is created on first write. Do not hand-edit it — go through the `memento_cards.py`
subcommands, which write to a temporary file and atomically rename it into place so a crash
can't leave a half-written deck behind.

## Authoring cards

### Deciding whether to create one

A factual sentence is not automatically a card. Sort the request into one of three buckets:

1. **Asked for outright** — the message contains "flashcard", "memento", "remember this",
   "add a card", or equivalent. Create the card straight away; no need to ask.
2. **Maybe** — the user states a fact but never asks for a card (e.g. "light travels at about
   299,792 km/s"). Ask once: "Want me to save this as a Memento flashcard?" and only proceed if
   they agree.
3. **Not at all** — questions, instructions, code, or chit-chat. Don't engage this skill; let
   normal handling take over.

### From a fact

Once you've decided to create the card, distill the statement into a single recall pair:

- the question should probe the one fact worth remembering
- the answer should be tight and unambiguous

Then store it:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py add \
  --question "In what year did World War II end?" \
  --answer "1945" \
  --collection "History"
```

With no collection given, the script files the card under `General`. The command prints JSON
describing the new card.

### By request

If the user asks to build a card but hasn't supplied the parts, gather: the front (question),
the back (answer), and an optional collection name (defaulting to `General`). Then run the same
`add` command.

## Review sessions

Pull the cards that have come due:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py due
```

The result is a JSON list of every non-retired card whose `next_review_at` has passed. Scope it
to one collection if needed:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py due --collection "History"
```

If nothing comes back, tell the user there's nothing to review right now and to check back later.

### The loop

Take one card at a time and follow this exact rhythm:

1. Print only the question. Stop and wait.
2. When the answer arrives, weigh it against the stored answer:
   - **right** — the core fact is there, even if phrased differently
   - **partial** — on the right track but the key detail is missing
   - **wrong** — incorrect or off-topic
3. **Always** reply with the verdict, the correct answer, and the next interval, in one short
   plain-text line. Suggested phrasings:
   - right: `Correct. Answer: {answer}. Next review in 7 days.`
   - partial: `Close. Answer: {answer}. {missing piece}. Next review in 3 days.`
   - wrong: `Not quite. Answer: {answer}. Next review tomorrow.`
4. Record the result, mapping right → `easy`, partial → `good`, wrong → `hard`:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py rate \
  --id CARD_ID --rating easy --user-answer "the user's exact words"
```

5. Move to the next card.

Step 3 is non-negotiable — the user must see the right answer and the verdict before the next
prompt appears.

Worked example:

> Agent: When did the Berlin Wall fall?
>
> User: 1991
>
> Agent: Not quite. Answer: 1989. Next review tomorrow.
> *(runs: memento_cards.py rate --id ABC --rating hard --user-answer "1991")*
>
> Next: Who was the first person to walk on the Moon?

If at any point the user wants a card gone for good, rate it `retire`.

## How scheduling works

The rating you submit sets the next interval and adjusts the card's state:

| Rating | Next review | Easy streak | Effect |
|---|---|---|---|
| `hard` | +1 day | reset to 0 | stays in rotation |
| `good` | +3 days | reset to 0 | stays in rotation |
| `easy` | +7 days | +1 | retired once the streak hits 3 |
| `retire` | never | reset to 0 | retired immediately |

A card is either **learning** (in active rotation) or **retired** (no longer surfaced, whether
mastered or dismissed). Three `easy` ratings in a row retire a card automatically.

## YouTube quizzes

When the user hands over a video link and wants to be quizzed:

**1. Get the video ID.** Pull it from either URL shape — `youtube.com/watch?v=ID` or
`youtu.be/ID` (so `dQw4w9WgXcQ` out of `https://www.youtube.com/watch?v=dQw4w9WgXcQ`).

**2. Fetch the transcript:**

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/youtube_quiz.py fetch VIDEO_ID
```

Success returns `{"ok": true, "video_id": "...", "transcript": "..."}`. If the JSON carries
`"error": "missing_dependency"`, point the user at the install step:

```bash
pip install youtube-transcript-api
```

**3. Write five questions** from the transcript yourself. Take the leading 15,000 characters as
your source material and follow these constraints:

- Pick the facts that matter most — striking, central, or load-bearing points. Skip throwaway
  lines, the obvious, and anything that needs a pile of context to make sense.
- One discrete fact per question. No true/false items. Don't ask purely for a date.
- Favor What / Who / How many / Which over open-ended "describe" or "explain".
- Each answer stays under 240 characters, leads with the answer itself, and adds only the
  smallest clarification needed.

Emit exactly five objects, each with non-empty `question` and `answer` string fields, as a JSON
array.

**4. Sanity-check** that you produced valid JSON, exactly five entries, each with a real question
and answer. If it's off, redo it once.

**5. Save the batch:**

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py add-quiz \
  --video-id "VIDEO_ID" \
  --questions '[{"question":"...","answer":"..."}]' \
  --collection "Quiz - Episode Title"
```

The script keys on `video_id`: if cards from that video already exist, it adds nothing and hands
back the cards already on file.

**6. Quiz the user** with the same free-text grading loop as review:

1. Show `Question 1/5: …` and wait. Never leak the answer or hint that one is coming.
2. Read the user's own-words answer.
3. Grade it by the same right/partial/wrong scale.
4. Reply with the verdict, the answer, and the next due time **before anything else** — never
   slide silently into the next question. Keep it to one short plain line, e.g.
   `Not quite. Answer: {answer}. Next review tomorrow.`
5. Then record it and show the next question:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py rate \
  --id CARD_ID --rating easy --user-answer "the user's exact words"
```

6. Repeat for all five. Every answer earns visible feedback before the next question.

## CSV in and out

**Export** writes a headerless three-column file — `question,answer,collection`:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py export \
  --output ~/flashcards.csv
```

**Import** reads the same shape. Column three (collection) is optional; rows without it land in
the `--collection` value:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py import \
  --file ~/flashcards.csv \
  --collection "Imported"
```

## Statistics

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py stats
```

Returns JSON with `total`, `learning`, `retired`, `due_now`, and a `collections` map of
per-collection counts.

## Things to watch

- **Don't touch `cards.json` by hand** — only the subcommands keep it consistent.
- **Some videos won't yield a transcript** — they may have captions disabled or no English
  track. Say so and suggest a different video.
- **The YouTube path needs `youtube-transcript-api`** — if it's missing, the script returns
  `missing_dependency`; relay the `pip install youtube-transcript-api` step.
- **Big imports** print verbose JSON — summarize the count for the user instead of dumping it.
- **Accept both URL forms** when pulling a video ID: `youtube.com/watch?v=ID` and `youtu.be/ID`.

## Verification

Exercise the helper directly:

```bash
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py stats
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py add --question "Capital of France?" --answer "Paris" --collection "General"
python3 ~/.flowly/skills/memento-flashcards/scripts/memento_cards.py due
```

At the agent level, confirm:

- review feedback is plain text, brief, and always reveals the correct answer before the next card
- a YouTube quiz gives visible feedback on every answer before moving on
