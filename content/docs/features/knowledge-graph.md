---
title: Knowledge Graph
eyebrow: Features
description: Flowly's knowledge graph is a temporal triple store of structured facts about people, companies, projects, events, and their relationships. Unlike free-form memory, facts here are queryable, time-aware, and can be invalidated when they stop being true.
---

The store is a SQLite database at `~/.flowly/knowledge_graph.sqlite3` (created lazily on first use). It is the structured counterpart to `MEMORY.md`: the agent is directed to record structured facts here rather than appending prose to memory.

## Data model

The graph holds three kinds of records:

### Entities

Nodes in the graph — people, companies, projects, events, or `unknown`.

- Each entity has a normalized `id` derived from its name (lowercased, trimmed, spaces become underscores, apostrophes stripped). For example `"O'Brien Corp"` → `obrien_corp`.
- Entity `type` is one of `person | company | project | event | unknown`. An entity's type is upgraded from `unknown` to a concrete type when better information arrives, but is never downgraded.
- Entities carry an optional JSON `properties` bag.

### Triples (facts)

Directed `subject — predicate — object` statements, each with a validity window:

- `valid_from` / `valid_to` are ISO date strings.
- A fact is **current iff `valid_to IS NULL`**. Setting `valid_to` marks the fact as ended (historical).
- Triples also carry `confidence` (default `1.0`) and `source` (default `agent`).

**Entity vs value predicates.** For most predicates, the object is another entity id. But a fixed set of **value predicates** store the object as plain normalized text instead of creating an entity node:

```
email, phone, url, website, address, location,
birthday, age, role, title, salary, note
```

So `alice — email — alice@example.com` stores the email as text, not as an entity.

### Aliases

Alternate names mapping to a canonical entity id, used during name resolution.

## Name resolution

> [!WARNING]
> Resolution is **exact id match → alias match only**. There is **no fuzzy or partial matching** in resolution — if a name doesn't resolve exactly or via an alias, the lookup misses.

When a lookup misses, the tool may surface "Did you mean…?" suggestions (partial `LIKE` matches), but it never auto-resolves to them; you must use the exact name or register an alias.

## The `knowledge_graph` tool

A single action-dispatched tool. It requires no approval — same trust level as `memory_append`.

### Actions

| Action | Purpose |
|--------|---------|
| `add` | Add an entity and/or a triple |
| `query` | Get triples for an entity |
| `invalidate` | End a currently-valid fact |
| `search` | Find facts by relationship type (predicate) |
| `timeline` | Chronological view of facts |
| `merge` | Merge two entities into one |
| `stats` | Counts of entities, triples, aliases, etc. |

### Parameters

| Param | Used by | Notes |
|-------|---------|-------|
| `action` | all | Required |
| `subject` | add / invalidate / merge | For `merge`, this is the **source** entity |
| `predicate` | add / invalidate / search | Required for `search` (the relationship type to match) |
| `object` | add / invalidate / merge | For `merge`, this is the **target** entity |
| `name` | query / timeline | Entity to look up |
| `valid_from` | add | ISO date the fact starts |
| `ended` | invalidate | ISO date the fact ended (defaults to today) |
| `as_of` | query / search | Temporal filter — facts valid at that date |
| `direction` | query | `outgoing` \| `incoming` \| `both` |
| `subject_type` | add | `person` \| `company` \| `project` \| `event` \| `unknown` |
| `object_type` | add | same enum as `subject_type` |

### Examples

Add a fact:

```json
{
  "action": "add",
  "subject": "Alice",
  "subject_type": "person",
  "predicate": "works_at",
  "object": "Nocetic",
  "object_type": "company"
}
```

Store a value predicate (object kept as plain text):

```json
{
  "action": "add",
  "subject": "Alice",
  "predicate": "email",
  "object": "alice@example.com"
}
```

Query an entity's current relationships:

```json
{ "action": "query", "name": "Alice", "direction": "both" }
```

Query as of a past date:

```json
{ "action": "query", "name": "Alice", "as_of": "2025-01-01" }
```

Invalidate a fact that is no longer true:

```json
{
  "action": "invalidate",
  "subject": "Alice",
  "predicate": "works_at",
  "object": "Nocetic",
  "ended": "2026-03-01"
}
```

Merge a duplicate entity into a canonical one (source → target):

```json
{ "action": "merge", "subject": "Alice B.", "object": "Alice" }
```

`merge` moves all of the source's triples to the target, registers the source name as an alias of the target, and de-duplicates the result.

### Tool-side guards

- Comma-separated **subjects** are rejected.
- Comma-separated **objects** are rejected unless the predicate is one of `email`, `phone`, `address`, `note`, `url`.
- On a name miss, the tool returns "Did you mean…?" suggestions rather than guessing.

## How the KG enters the system prompt

A compact KG summary (top entities by current-fact count) is injected into the main agent's system prompt as a `# Knowledge Graph` section. Subagents receive a `## Known Facts` summary the same way — and that subagent path additionally scans the summary for prompt-injection payloads before injecting it.

## Notes / limitations

> [!WARNING]
> **Custom workspace can desync KG injection.** The tool reads and writes `~/.flowly/knowledge_graph.sqlite3`, but the system-prompt injection resolves the KG database from the workspace location. These line up only when the workspace is the default `~/.flowly/workspace`. With a **custom (non-default) workspace**, the prompt summary may show an empty or stale graph while the tool continues to read/write the correct database. Subagent injection is more consistent (it prefers the tool's state directory). If you use a custom workspace and the injected `# Knowledge Graph` looks empty despite stored facts, this is the cause — the tool itself still works.

> [!NOTE]
> **No fuzzy resolution.** Despite older docstrings mentioning fuzzy matching, resolution is exact-id + alias only. Use exact names or register aliases.

- **Value predicates are plain text.** Objects for the value predicates listed above are stored as normalized text, not entity nodes, so they won't appear as graph entities.

## Related

- [Memory](./memory.md)
- [Delegation](../features/delegation.md)
- [Features overview](../features/overview.md)
- [Sessions](../using-flowly/sessions.md)
- [Tools reference](../reference/tools.md)
