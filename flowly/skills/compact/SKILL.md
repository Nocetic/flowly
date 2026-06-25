# Compact Skill

Manually trigger context compaction to summarize conversation history.

## Usage

```
/compact [instructions]
```

## Examples

- `/compact` - Compact with default settings
- `/compact Focus on decisions and open questions` - Compact with custom focus
- `/compact Keep technical details about the API` - Preserve specific information

## What It Does

1. Summarizes the current conversation history
2. Replaces old messages with a concise summary
3. Preserves important context while freeing up token space

## When to Use

- When you notice the conversation is getting long
- Before starting a new topic to clear old context
- When you want to preserve specific information in the summary
