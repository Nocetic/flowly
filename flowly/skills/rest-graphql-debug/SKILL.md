---
name: rest-graphql-debug
description: "Debug REST/GraphQL APIs: status codes, auth, schemas, repro."
metadata: {"flowly":{"emoji":"🌐","tags":["api","rest","graphql","http","debugging","testing","curl","integration"],"requires":{"bins":["curl","python3"]},"related_skills":["systematic-debugging","test-driven-development"]}}
---

# API Testing & Debugging

Use this skill for REST and GraphQL failures: unexpected status/body, auth
failures, webhook debugging, pagination issues, contract drift, or code that
works in Postman but fails in the app.

Drive diagnosis with Flowly tools:

- Use `exec` for `curl`, `openssl`, `dig/nslookup`, pytest, and temporary Python scripts.
- Use `web_fetch` or browser tools for vendor API docs.
- Keep credentials in environment variables; never paste or print full tokens.

## Debug Order

Walk the chain in order. Do not jump to code changes before isolating the
failing layer.

1. Connectivity: can the host be reached?
2. Timeouts: connect-slow or read-slow?
3. TLS: certificate valid, trusted, and matching hostname?
4. Auth: token present, unexpired, correct scheme and scopes?
5. Request format: method, headers, content type, payload shape?
6. Response parsing: content type and body match what code expects?
7. Semantics: response data means what the app assumes?

## Quick Checks

```bash
# Connectivity
nslookup api.example.com
curl -v --connect-timeout 5 https://api.example.com/health

# Timing profile
curl -w "dns:%{time_namelookup}s connect:%{time_connect}s tls:%{time_appconnect}s ttfb:%{time_starttransfer}s total:%{time_total}s\n" \
  -o /dev/null -s https://api.example.com/endpoint

# TLS
curl -vI https://api.example.com 2>&1 | grep -E "SSL|subject|expire|issuer"

# Auth smoke
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/me
```

## REST Repro

Always capture the smallest reproducible `curl` command. Redact secrets in
anything shown to the user.

```bash
curl -v -X POST https://api.example.com/users \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"test","email":"test@example.com"}'
```

Pretty-print JSON without assuming `jq` exists:

```bash
curl -s https://api.example.com/users | python3 -m json.tool
```

## GraphQL

GraphQL servers often return HTTP 200 when the query failed. Always inspect
the `errors` field.

```bash
curl -s -X POST https://api.example.com/graphql \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"query":"{ user(id: 1) { name email } }"}' \
  | python3 -m json.tool
```

For richer checks, run Python through `exec`:

```bash
python3 - <<'PY'
import os, requests

resp = requests.post(
    "https://api.example.com/graphql",
    json={"query": "{ user(id: 1) { name email } }"},
    headers={"Authorization": f"Bearer {os.environ['TOKEN']}"},
    timeout=(3.05, 30),
)
print("status", resp.status_code)
data = resp.json()
for err in data.get("errors", []):
    print("GraphQL error:", err.get("message"), "path:", err.get("path"))
print(data.get("data"))
PY
```

## Python Requests Probe

Use this when response parsing, headers, retries, or contract validation
matter more than a one-line `curl`.

```bash
python3 - <<'PY'
import os, requests

resp = requests.get(
    "https://api.example.com/users/1",
    headers={"Authorization": f"Bearer {os.environ['TOKEN']}"},
    timeout=(3.05, 30),
)
print("status", resp.status_code)
print("headers", dict(resp.headers))
print(resp.text[:500])
PY
```

Use tuple timeouts. `requests` has no default timeout and can hang forever.

## Status Playbook

- `401`: missing/expired credentials, wrong auth scheme, wrong environment.
- `403`: authenticated but missing scope/permission, wrong account, IP allowlist, CORS.
- `404`: wrong URL/version/base URL, deleted resource, ID mismatch.
- `409`: duplicate create, stale ETag, concurrent modification.
- `422`: payload shape valid JSON but invalid fields/types/enums.
- `429`: check `Retry-After` and rate-limit headers; use exponential backoff.
- `5xx`: capture request ID/correlation ID; retry with jitter only when safe.

## Contract Validation

Run after API upgrades, when integrating a new provider, or before shipping
a fix.

```bash
python3 - <<'PY'
import requests

BASE = "https://api.example.com"
HEADERS = {"Authorization": "Bearer <REDACTED>"}

def validate_user(data):
    errors = []
    required = {"id": int, "email": str, "created_at": str}
    for field, expected in required.items():
        if field not in data:
            errors.append(f"missing field: {field}")
        elif not isinstance(data[field], expected):
            errors.append(f"{field}: want {expected.__name__}, got {type(data[field]).__name__}")
    return errors

resp = requests.get(f"{BASE}/users/1", headers=HEADERS, timeout=10)
print("status", resp.status_code)
if resp.headers.get("content-type", "").startswith("application/json"):
    print(validate_user(resp.json()))
else:
    print("unexpected content type", resp.headers.get("content-type"))
    print(resp.text[:300])
PY
```

## Regression Test Template

Create a focused smoke test and run it with `exec`.

```python
import os
import requests

BASE_URL = os.environ.get("API_BASE_URL", "https://api.example.com")
TOKEN = os.environ.get("API_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def test_health():
    resp = requests.get(f"{BASE_URL}/health", timeout=5)
    assert resp.status_code == 200

def test_list_users_returns_array():
    resp = requests.get(f"{BASE_URL}/users", headers=HEADERS, timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data.get("data", data), list)
```

```bash
pytest tests/test_api_smoke.py -v
```

## Security

- Never log full tokens. Redact as `Bearer <REDACTED>`.
- Never hardcode credentials in scripts; read from env or `~/.flowly/.env`.
- Do not use `curl -v` output in user-facing messages until auth headers are redacted.
- Use headers over query parameters for API keys where the API allows it.
- Check error bodies for PII, internal hostnames, stack traces, and echoed tokens.

## Output Format

```markdown
## Finding
Endpoint: POST /api/v1/users
Status:   422 Unprocessable Entity
Req ID:   req_abc123xyz

## Repro
curl -X POST https://api.example.com/api/v1/users \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <REDACTED>' \
  -d '{"name":"test"}'

## Root Cause
Missing required field `email`.

## Fix
Send `{"name":"test","email":"test@example.com"}`.
```
