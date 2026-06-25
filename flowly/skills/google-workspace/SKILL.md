---
name: google-workspace
description: "Google Workspace (Drive, Gmail, Calendar, Sheets, Docs, Chat) via the `gws` CLI. Read files, send emails, manage events, update spreadsheets and more."
metadata: {"flowly":{"emoji":"🏢","requires":{"bins":["gws"]},"install":[{"id":"npm","kind":"npm","package":"@googleworkspace/cli","bins":["gws"],"label":"Install Google Workspace CLI (npm)"},{"id":"brew","kind":"brew","formula":"node","bins":["node","npm"],"label":"Install Node.js via Homebrew (required for npm)"}]}}
---

# Google Workspace Skill

Use the `gws` CLI to interact with Google Workspace APIs. All output is structured JSON.

## Setup (first time)

```bash
# 1. Install gws
npm install -g @googleworkspace/cli

# 2. Create Google Cloud project + OAuth credentials
gws auth setup

# 3. Authenticate with your Google account
gws auth login

# 4. (Optional) Limit scopes if you get "too many scopes" error
gws auth login --scopes drive,gmail,calendar
```

## Syntax

```
gws <service> <resource> [sub-resource] <method> [flags]
```

| Flag | Description |
|------|-------------|
| `--params '{"key":"val"}'` | URL / query parameters |
| `--json '{"key":"val"}'` | Request body |
| `--dry-run` | Validate request without calling the API |
| `--page-all` | Auto-paginate (NDJSON output) |
| `--format table` | Human-readable table output |

Inspect any method before using it:
```bash
gws schema drive.files.list
gws drive --help
```

## Security Rules

- Always confirm with the user before write or delete operations.
- Use `--dry-run` first for destructive operations.
- Never output OAuth tokens or credentials.

---

## Drive

```bash
# List recent files
gws drive files list --params '{"pageSize": 10, "orderBy": "modifiedTime desc"}'

# Search files
gws drive files list --params '{"q": "name contains '\''budget'\'' and trashed=false"}'

# Get file metadata
gws drive files get --params '{"fileId": "FILE_ID", "fields": "id,name,mimeType,size,modifiedTime"}'

# Download a file
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}' -o ./output.pdf

# Create a folder
gws drive files create --json '{"name": "My Folder", "mimeType": "application/vnd.google-apps.folder"}'

# Upload a file
gws drive files create --json '{"name": "report.pdf"}' --upload ./report.pdf

# Move file to trash
gws drive files update --params '{"fileId": "FILE_ID"}' --json '{"trashed": true}' --dry-run

# Share a file
gws drive permissions create \
  --params '{"fileId": "FILE_ID", "sendNotificationEmail": "true"}' \
  --json '{"role": "reader", "type": "user", "emailAddress": "user@example.com"}' \
  --dry-run
```

## Gmail

```bash
# List unread messages (inbox)
gws gmail users messages list \
  --params '{"userId": "me", "q": "is:unread in:inbox", "maxResults": 10}'

# Get a message (full content)
gws gmail users messages get \
  --params '{"userId": "me", "id": "MESSAGE_ID", "format": "full"}'

# Send an email (base64url-encoded RFC 2822)
# Build the raw message first, then:
gws gmail users messages send \
  --params '{"userId": "me"}' \
  --json '{"raw": "BASE64URL_ENCODED_MESSAGE"}' \
  --dry-run

# List labels
gws gmail users labels list --params '{"userId": "me"}'

# Create a draft
gws gmail users drafts create \
  --params '{"userId": "me"}' \
  --json '{"message": {"raw": "BASE64URL_ENCODED_MESSAGE"}}' \
  --dry-run
```

### Sending Email Helper

To send email, encode the message as base64url:

```bash
# Build RFC 2822 message and encode
RAW=$(printf 'To: recipient@example.com\r\nSubject: Hello\r\nContent-Type: text/plain\r\n\r\nBody text here.' | base64 | tr '+/' '-_' | tr -d '=\n')
gws gmail users messages send --params '{"userId":"me"}' --json "{\"raw\":\"$RAW\"}" --dry-run
```

## Calendar

```bash
# List upcoming events (next 7 days)
gws calendar events list --params '{
  "calendarId": "primary",
  "timeMin": "2026-03-05T00:00:00Z",
  "timeMax": "2026-03-12T00:00:00Z",
  "singleEvents": "true",
  "orderBy": "startTime",
  "maxResults": 20
}'

# Create an event
gws calendar events insert \
  --params '{"calendarId": "primary"}' \
  --json '{
    "summary": "Team Standup",
    "start": {"dateTime": "2026-03-06T10:00:00+03:00"},
    "end":   {"dateTime": "2026-03-06T10:30:00+03:00"}
  }' \
  --dry-run

# Delete an event
gws calendar events delete \
  --params '{"calendarId": "primary", "eventId": "EVENT_ID"}' \
  --dry-run

# List calendars
gws calendar calendarList list
```

## Sheets

```bash
# Read a range (use single quotes around ranges with !)
gws sheets spreadsheets values get \
  --params '{"spreadsheetId": "SHEET_ID", "range": "Sheet1!A1:E10"}'

# Write values
gws sheets spreadsheets values update \
  --params '{"spreadsheetId": "SHEET_ID", "range": "Sheet1!A1", "valueInputOption": "USER_ENTERED"}' \
  --json '{"values": [["Name", "Score"], ["Alice", 95], ["Bob", 87]]}' \
  --dry-run

# Append rows
gws sheets spreadsheets values append \
  --params '{"spreadsheetId": "SHEET_ID", "range": "Sheet1!A1", "valueInputOption": "USER_ENTERED"}' \
  --json '{"values": [["Charlie", 91]]}' \
  --dry-run

# Create a spreadsheet
gws sheets spreadsheets create --json '{"properties": {"title": "Q1 Budget"}}'
```

## Docs

```bash
# Get document content
gws docs documents get --params '{"documentId": "DOC_ID"}'

# Create a new document
gws docs documents create --json '{"title": "Meeting Notes"}'

# Batch update (insert text, format, etc.)
gws docs documents batchUpdate \
  --params '{"documentId": "DOC_ID"}' \
  --json '{"requests": [{"insertText": {"location": {"index": 1}, "text": "Hello World\n"}}]}' \
  --dry-run
```

## Chat

```bash
# List spaces (rooms)
gws chat spaces list

# Send a message to a space
gws chat spaces messages create \
  --params '{"parent": "spaces/SPACE_ID"}' \
  --json '{"text": "Deploy complete!"}' \
  --dry-run

# List messages in a space
gws chat spaces messages list --params '{"parent": "spaces/SPACE_ID"}'
```

## Discovering APIs

```bash
# List all available services
gws --help

# Introspect any method
gws schema gmail.users.messages.list
gws schema drive.files.create
gws schema calendar.events.insert

# Browse a service
gws drive --help
gws gmail --help
```
