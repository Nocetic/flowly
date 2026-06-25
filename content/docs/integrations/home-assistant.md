---
title: Home Assistant
eyebrow: Integrations
description: Talk to your Home Assistant instance directly over your local network using a Long-Lived Access Token. Four tools register together, with an in-tool blocklist guard around service calls.
---

Flowly talks to your Home Assistant instance directly over your local network
using a **Long-Lived Access Token**. Four tools register together as a unit:
entity discovery, state inspection, service listing, and service calls.

> [!WARNING]
> Because Home Assistant has no service-level authorization, Flowly adds an in-tool blocklist guard around service calls.

## Tools

| Tool | Purpose | Key params |
|---|---|---|
| `ha_list_entities` | List/filter entities | `domain`, `area` |
| `ha_get_state` | Detailed state of one entity | `entity_id` |
| `ha_list_services` | List services (actions) per domain | `domain` (optional) |
| `ha_call_service` | Call a service (e.g. `turn_on`, `set_temperature`) | `domain`, `service`, `entity_id`, `data` (JSON string) |

## Configuration

All four tools register together, gated on **both** `url` and `token` being
non-empty:

```json
{
  "integrations": {
    "homeAssistant": {
      "url": "http://homeassistant.local:8123",
      "token": "<long-lived-access-token>"
    }
  }
}
```

| Key | Notes |
|---|---|
| `url` | Base URL of your HA instance on the LAN, e.g. `http://homeassistant.local:8123` |
| `token` | Long-Lived Access Token |

The token is sent as `Authorization: Bearer <token>` on every REST call. It is
used directly from the running process and Home Assistant is reached directly on
the local network.

## Getting a Long-Lived Access Token

In Home Assistant, open your **Profile → Long-Lived Access Tokens** and create a
token. Copy it into `integrations.homeAssistant.token`.

## Security: service-call blocklist

> [!IMPORTANT]
> Since the HA token grants broad access, `ha_call_service` applies guards before issuing a call.

- Domain and service identifiers are validated against a strict pattern
  *before* any other check, preventing path traversal in the
  `/api/services/{domain}/{service}` path (e.g. `domain="../../api/config"` or
  `domain="shell_command/../light"`). Entity IDs are likewise validated when
  supplied.
- A **blocklist** of six service domains is rejected even if your token would
  otherwise authorize them, because they allow code or shell execution on the HA
  host (or SSRF from the HA server):

  | Blocked domain |
  |---|
  | `shell_command` |
  | `command_line` |
  | `python_script` |
  | `pyscript` |
  | `hassio` |
  | `rest_command` |

- State-changing services that report no affected entities are flagged as
  unsuccessful (`success: false`).

## Setup

Run the interactive integrations setup and pick Home Assistant:

```bash
flowly setup tools
```

The Home Assistant card prompts for the URL and token, validates them with a
probe, and writes `integrations.homeAssistant.url` /
`integrations.homeAssistant.token`.

> [!NOTE]
> The integration needs a gateway restart to take effect (handled by setup, or run `flowly service restart`).

## Related

- [MCP](../features/mcp.md)
- [Tools reference](../reference/tools.md)
- [Configuration](../using-flowly/configuration.md)
- [Google Workspace](./google-workspace.md), [Linear](./linear.md), [Trello](./trello.md), [X](./x.md)
- [Channels overview](../channels/overview.md)
