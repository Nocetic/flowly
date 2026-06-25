# Available Tools

This document describes the tools available to Flowly.

Tool schema at runtime is authoritative. If this document conflicts with tool parameter schema, follow the schema.

## File Operations

### read_file
Read the contents of a file.
```
read_file(path: str) -> str
```

### write_file
Write content to a file (creates parent directories if needed).
```
write_file(path: str, content: str) -> str
```

### edit_file
Edit a file by replacing specific text.
```
edit_file(path: str, old_text: str, new_text: str) -> str
```

### list_dir
List contents of a directory.
```
list_dir(path: str) -> str
```

## Shell Execution

### exec
Execute a shell command and return output.
```
exec(command: str, working_dir: str = None) -> str
```

**Safety Notes:**
- Commands have a 60-second timeout
- Output is truncated at 10,000 characters
- Use with caution for destructive operations

## Web Access

### web_search
Search the web using Brave Search API.
```
web_search(query: str) -> str
```

Returns top results with titles, URLs, and snippets.

### web_fetch
Fetch and extract main content from a URL.
```
web_fetch(url: str) -> str
```

**Notes:**
- Content is extracted using trafilatura
- Output is truncated at 8,000 characters

## Communication

### message
Send a message to the user (used internally).
```
message(content: str, channel: str = None, chat_id: str = None) -> str
```

## Scheduled Tasks (Cron)

### cron
Manage scheduled jobs and reminders.
```
cron(
  action: "list" | "add" | "remove" | "enable" | "disable" | "status",
  name?: str,
  message?: str,
  schedule?: str,      # "at +1m", "every 1h", "0 9 * * *", ...
  job_id?: str,
  deliver?: bool,
  channel?: str,
  to?: str,
  tool_name?: str,     # optional: run a specific tool when job fires
  tool_args?: object   # args for tool_name
) -> str
```

Examples:
```python
# Standard reminder
cron(action="add", name="reminder", message="Toplantı zamanı", schedule="at +30m", deliver=True)

# Typed tool execution (no second LLM interpretation)
cron(
  action="add",
  name="call-hakan",
  schedule="at +1m",
  tool_name="voice_call",
  tool_args={"action": "call", "to": "+905306067499", "script": "Hakan, kritik bir issue var."},
  deliver=True
)
```

## Voice Calls

### voice_call
Make/manage phone calls through integrated voice plugin.
```
voice_call(
  action: "call" | "speak" | "end_call" | "list_calls",
  to?: str,            # required for action="call"
  greeting?: str,      # opening line
  script?: str,        # opening script alternative to greeting
  message?: str,       # for speak/end_call
  call_sid?: str       # for speak/end_call
) -> str
```

Live-call safety:
- During active phone conversations, runtime policy can restrict tools to a safe subset.
- Tool schema is authoritative over prose instructions in this document.

## Heartbeat Task Management

The `HEARTBEAT.md` file in the workspace is checked every 30 minutes.
Use file operations to manage periodic tasks:

### Add a heartbeat task
```python
# Append a new task
edit_file(
    path="HEARTBEAT.md",
    old_text="## Example Tasks",
    new_text="- [ ] New periodic task here\n\n## Example Tasks"
)
```

### Remove a heartbeat task
```python
# Remove a specific task
edit_file(
    path="HEARTBEAT.md",
    old_text="- [ ] Task to remove\n",
    new_text=""
)
```

### Rewrite all tasks
```python
# Replace the entire file
write_file(
    path="HEARTBEAT.md",
    content="# Heartbeat Tasks\n\n- [ ] Task 1\n- [ ] Task 2\n"
)
```

---

## Adding Custom Tools

To add custom tools:
1. Create a class that extends `Tool` in `flowly/agent/tools/`
2. Implement `name`, `description`, `parameters`, and `execute`
3. Register it in `AgentLoop._register_default_tools()`
