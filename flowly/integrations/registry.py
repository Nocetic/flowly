"""The integration catalog — declarative entries for every service.

Adding a new integration is two edits:

1. Append an :class:`IntegrationCard` here.
2. (Optional) Add a probe function in :mod:`flowly.integrations.probes`.

The TUI reads this list at render time and filters by category for surfaces
like ``/integrations``, ``/channels``, and ``/provider``. Order within a
category determines display order. Keep popular / first-time setups near the
top of each group.
"""

from __future__ import annotations

from flowly.integrations.cards import Field, FieldType, IntegrationCard
from flowly.integrations.probes import (
    probe_anthropic,
    probe_brave_search,
    probe_ddgs,
    probe_discord,
    probe_email,
    probe_exa,
    probe_fal_image,
    probe_firecrawl,
    probe_flowly_account,
    probe_gemini,
    probe_github,
    probe_groq,
    probe_home_assistant,
    probe_imessage,
    probe_linear,
    probe_obsidian,
    probe_openai,
    probe_openai_codex,
    probe_openrouter,
    probe_parallel,
    probe_sakana,
    probe_searxng,
    probe_sentry,
    probe_slack,
    probe_tavily,
    probe_teams,
    probe_telegram,
    probe_trello,
    probe_twilio,
    probe_web_channel,
    probe_whatsapp,
    probe_x,
    probe_xai,
    probe_xai_oauth,
    probe_zai_coding,
    probe_zhipu,
)
from flowly.media.image_models import DEFAULT_IMAGE_MODEL as _DEFAULT_IMAGE_MODEL
from flowly.media.image_models import model_choices as _image_model_choices


def _enabled_field(default: bool = False) -> Field:
    return Field("enabled", "Enabled", FieldType.BOOL, default=default,
                 help="Channel starts with the gateway when this is on.")


def _default_backend_field() -> Field:
    return Field("default", "Use as default backend", FieldType.BOOL, default=False,
                 help="Make web_search use this backend. Overrides the auto pick.")


def _allow_from() -> Field:
    return Field(
        "allow_from", "Allowed senders", FieldType.MULTI,
        placeholder="123456789, @yourhandle",
        help="Comma-separated user IDs or usernames. Empty = anyone.",
    )


# ── CHANNELS ───────────────────────────────────────────────────────


_CHANNELS: list[IntegrationCard] = [
    IntegrationCard(
        key="telegram", label="Telegram", category="channel",
        description="Talk to Flowly through a Telegram bot in DMs or groups.",
        docs_url="https://core.telegram.org/bots#how-do-i-create-a-bot",
        config_path="channels.telegram",
        fields=[
            _enabled_field(),
            Field("token", "Bot token", FieldType.PASSWORD,
                  placeholder="123456:ABC-DEF…", required=True,
                  help="Created by @BotFather → /newbot."),
            _allow_from(),
            Field("dm_policy", "DM policy", FieldType.SELECT,
                  default="pairing",
                  choices=[("open", "open · anyone can DM"),
                           ("pairing", "pairing · must claim a pair code"),
                           ("allowlist", "allowlist · only allowed senders")]),
        ],
        probe=probe_telegram,
    ),
    IntegrationCard(
        key="discord", label="Discord", category="channel",
        description="Discord bot adapter — DMs and guild channels.",
        docs_url="https://discord.com/developers/applications",
        config_path="channels.discord",
        fields=[
            _enabled_field(),
            Field("token", "Bot token", FieldType.PASSWORD,
                  placeholder="MTIxN…", required=True,
                  help="Discord Developer Portal → your app → Bot → Reset Token."),
            _allow_from(),
        ],
        probe=probe_discord,
    ),
    IntegrationCard(
        key="slack", label="Slack", category="channel",
        description="Slack bot via Socket Mode — DMs, mentions, allowed channels.",
        docs_url="https://api.slack.com/apps",
        config_path="channels.slack",
        fields=[
            _enabled_field(),
            Field("bot_token", "Bot OAuth token", FieldType.PASSWORD,
                  placeholder="xoxb-…", required=True,
                  help="Slack app → OAuth & Permissions → Bot User OAuth Token."),
            Field("app_token", "App-level token", FieldType.PASSWORD,
                  placeholder="xapp-…", required=True,
                  help="Slack app → Basic Information → App-Level Tokens (scope: connections:write)."),
            Field("group_policy", "Group policy", FieldType.SELECT,
                  default="mention",
                  choices=[("mention", "mention · only when @bot is tagged"),
                           ("open", "open · respond to every message"),
                           ("allowlist", "allowlist · only in listed channels")]),
            Field("group_allow_from", "Allowed channels", FieldType.MULTI,
                  placeholder="C01ABCDEF, C02XXXX",
                  help="Channel IDs (used when group policy = allowlist)."),
        ],
        probe=probe_slack,
    ),
    IntegrationCard(
        key="teams", label="Microsoft Teams", category="channel",
        description="One-way outbound to a Teams channel via Incoming Webhook.",
        docs_url="https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook",
        config_path="channels.teams",
        fields=[
            _enabled_field(),
            Field("webhook_url", "Webhook URL", FieldType.PASSWORD,
                  placeholder="https://outlook.office.com/webhook/…", required=True,
                  help="Teams channel → ⋯ → Connectors → Incoming Webhook → Create."),
            Field("default_chat_label", "Channel label", FieldType.TEXT,
                  placeholder="#team-ops",
                  help="Human-friendly name for the target channel."),
        ],
        probe=probe_teams,
    ),
    IntegrationCard(
        key="whatsapp", label="WhatsApp", category="channel",
        description="WhatsApp via a self-hosted bridge daemon.",
        docs_url="https://github.com/tulir/whatsmeow",
        config_path="channels.whatsapp",
        fields=[
            _enabled_field(),
            Field("bridge_url", "Bridge URL", FieldType.TEXT,
                  placeholder="ws://localhost:3001", required=True,
                  help="WebSocket URL of your WhatsApp bridge."),
            _allow_from(),
        ],
        probe=probe_whatsapp,
    ),
    IntegrationCard(
        key="imessage", label="iMessage", category="channel",
        description="Reply over iMessage using this Mac's Messages app.",
        docs_url="https://useflowlyapp.com/docs/imessage",
        config_path="channels.imessage",
        fields=[
            _enabled_field(),
            Field("allow_from", "Allowed senders", FieldType.MULTI,
                  placeholder="+15551234567, friend@icloud.com",
                  help="Phone numbers (E.164) or iMessage emails. Empty = pairing flow."),
            Field("dm_policy", "DM policy", FieldType.SELECT,
                  default="pairing",
                  choices=[("open", "open · anyone can message"),
                           ("pairing", "pairing · must claim a pair code"),
                           ("allowlist", "allowlist · only allowed senders")]),
            Field("group_policy", "Group policy", FieldType.SELECT,
                  default="mention",
                  choices=[("mention", "mention · only when @flowly is named"),
                           ("open", "open · respond to every group message"),
                           ("allowlist", "allowlist · only listed group chats")]),
            Field("group_allow_from", "Allowed group chats", FieldType.MULTI,
                  placeholder="chat831290…",
                  help="Group chat identifiers (used when group policy = allowlist)."),
            Field("bluebubbles_url", "BlueBubbles URL", FieldType.TEXT,
                  placeholder="http://127.0.0.1:1234",
                  help="Optional. Send replies through a BlueBubbles server "
                       "(holds the Automation permission). Leave blank to drive "
                       "Messages.app directly."),
            Field("bluebubbles_password", "BlueBubbles password", FieldType.PASSWORD,
                  help="Server password set in the BlueBubbles app."),
        ],
        probe=probe_imessage,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="email", label="Email (Gmail)", category="channel",
        description="Poll a Gmail inbox via OAuth; reply by sending.",
        docs_url="https://developers.google.com/gmail/api",
        config_path="channels.email",
        fields=[
            _enabled_field(),
            Field("poll_interval_seconds", "Poll interval (s)", FieldType.INT,
                  default=30, help="How often to check for new messages."),
            _allow_from(),
        ],
        probe=probe_email,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="web", label="iOS / Web pairing", category="channel",
        description="Lets the Flowly iOS app reach this machine via the cloud relay.",
        docs_url="https://useflowlyapp.com/docs/ios-pairing",
        config_path="channels.web",
        fields=[],
        probe=probe_web_channel,
        custom_action="login",   # opens login modal instead of form
    ),
]


# ── TOOLS (LLM-callable) ───────────────────────────────────────────


_TOOLS: list[IntegrationCard] = [
    IntegrationCard(
        key="home_assistant", label="Home Assistant", category="tool",
        description="Control smart-home entities via the local HA REST API.",
        docs_url="https://www.home-assistant.io/integrations/api/",
        config_path="integrations.home_assistant",
        fields=[
            Field("url", "Base URL", FieldType.TEXT,
                  placeholder="http://homeassistant.local:8123", required=True,
                  help="Your HA URL on the local network."),
            Field("token", "Long-Lived Token", FieldType.PASSWORD,
                  required=True,
                  help="HA Profile → Long-Lived Access Tokens → Create."),
        ],
        probe=probe_home_assistant,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="linear", label="Linear", category="tool",
        description="Read issues, projects and create tickets in Linear.",
        docs_url="https://linear.app/settings/api",
        config_path="integrations.linear",
        fields=[
            Field("api_key", "Personal API key", FieldType.PASSWORD,
                  placeholder="lin_api_…", required=True,
                  help="Linear → Settings → API → Personal API keys."),
        ],
        probe=probe_linear,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="github", label="GitHub", category="tool",
        description="Read issues and pull requests, comment, and open issues on GitHub.",
        docs_url="https://github.com/settings/tokens",
        config_path="integrations.github",
        fields=[
            Field("token", "Personal access token", FieldType.PASSWORD,
                  placeholder="ghp_… / github_pat_…", required=True,
                  help="GitHub → Settings → Developer settings → Personal access tokens (repo scope)."),
            Field("default_repo", "Default repo", FieldType.TEXT,
                  placeholder="owner/name", required=False,
                  help="Optional fallback when not working inside a git repo."),
        ],
        probe=probe_github,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="sentry", label="Sentry", category="tool",
        description="Read error issues and event stacktraces from Sentry.",
        docs_url="https://docs.sentry.io/api/auth/",
        config_path="integrations.sentry",
        fields=[
            Field("token", "Auth token", FieldType.PASSWORD,
                  placeholder="sntrys_…", required=True,
                  help="Sentry → Settings → Auth Tokens (project:read, event:read)."),
            Field("org", "Organization slug", FieldType.TEXT,
                  placeholder="my-org", required=True,
                  help="The org slug from your Sentry URL."),
            Field("default_project", "Default project", FieldType.TEXT,
                  placeholder="my-project", required=False,
                  help="Optional project slug used by list_issues."),
        ],
        probe=probe_sentry,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="obsidian", label="Obsidian", category="tool",
        description="Search, read and cite your Obsidian vault; turn selected notes into review-gated memory.",
        docs_url="https://help.obsidian.md/Files+and+folders/How+Obsidian+stores+data",
        config_path="integrations.obsidian",
        fields=[
            Field("enabled", "Enabled", FieldType.BOOL, default=False,
                  help="Register Obsidian tools and (optionally) on-demand note injection."),
            Field("vault_path", "Vault path", FieldType.TEXT,
                  placeholder="~/Documents/Obsidian Vault",
                  help="Absolute path to your vault folder. Defaults to OBSIDIAN_VAULT_PATH or ~/Documents/Obsidian Vault."),
            Field("auto_inject", "Auto-inject notes", FieldType.SELECT,
                  default="on_demand",
                  choices=[("on_demand", "on demand · only when the message needs notes"),
                           ("off", "off · only via tools")],
                  help="Whether relevant vault snippets are added to context automatically."),
            Field("ingestion_policy", "Memory ingestion", FieldType.SELECT,
                  default="review_gated",
                  choices=[("review_gated", "review-gated · you approve each fact"),
                           ("manual_only", "manual · only via obsidian_ingest")],
                  help="Vault-derived facts never enter memory automatically."),
        ],
        probe=probe_obsidian,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="trello", label="Trello", category="tool",
        description="Read boards, cards, lists and create/move cards.",
        docs_url="https://trello.com/app-key",
        config_path="integrations.trello",
        fields=[
            Field("api_key", "API key", FieldType.PASSWORD, required=True,
                  help="From https://trello.com/app-key (top of the page)."),
            Field("token", "Token", FieldType.PASSWORD, required=True,
                  help="Click 'manually generate a Token' on the same page."),
        ],
        probe=probe_trello,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="x", label="X (Twitter)", category="tool",
        description="Search, post and reply on X. Bearer = read, OAuth 1.0a = write.",
        docs_url="https://developer.twitter.com/en/portal/dashboard",
        config_path="integrations.x",
        fields=[
            Field("bearer_token", "App Bearer (read)", FieldType.PASSWORD,
                  required=True,
                  help="App-only Bearer Token from your X dev app."),
            Field("api_key", "OAuth Consumer Key", FieldType.PASSWORD,
                  help="Only needed for posting/writing."),
            Field("api_secret", "OAuth Consumer Secret", FieldType.PASSWORD),
            Field("access_token", "Access Token", FieldType.PASSWORD),
            Field("access_token_secret", "Access Token Secret", FieldType.PASSWORD),
        ],
        probe=probe_x,
        needs_gateway_restart=True,
    ),
    IntegrationCard(
        key="google_workspace", label="Google Workspace", category="tool",
        description="Calendar, Drive, Contacts, Tasks via OAuth on your account.",
        docs_url="https://console.cloud.google.com/apis/credentials",
        config_path="integrations.google_workspace",
        fields=[
            _enabled_field(),
            Field("email", "Account email", FieldType.TEXT,
                  placeholder="you@gmail.com",
                  help="Display only — actual OAuth tokens come from `flowly google login`."),
        ],
        # Probe is intentionally not registered: OAuth setup is its own
        # flow and the form here is just an opt-in toggle.
        needs_gateway_restart=True,
    ),
]


# ── VOICE ──────────────────────────────────────────────────────────


_VOICE: list[IntegrationCard] = [
    IntegrationCard(
        key="voice", label="Voice (Twilio)", category="voice",
        description="Inbound/outbound phone calls via Twilio + STT/TTS.",
        docs_url="https://console.twilio.com/",
        config_path="integrations.voice",
        fields=[
            _enabled_field(),
            Field("twilio_account_sid", "Twilio Account SID", FieldType.PASSWORD,
                  placeholder="AC…", required=True),
            Field("twilio_auth_token", "Twilio Auth Token", FieldType.PASSWORD,
                  required=True),
            Field("twilio_phone_number", "Twilio number", FieldType.TEXT,
                  placeholder="+15551234567",
                  help="E.164 format. The number Twilio rents to you."),
            Field("webhook_base_url", "Public webhook base URL", FieldType.TEXT,
                  placeholder="https://abc123.ngrok.app",
                  help="Or leave blank and set Ngrok authtoken below."),
            Field("ngrok_authtoken", "Ngrok authtoken (optional)", FieldType.PASSWORD,
                  help="Set this to auto-tunnel; you can leave webhook_base_url blank."),
            Field("stt_provider", "STT provider", FieldType.SELECT,
                  default="groq",
                  choices=[("groq", "groq · Whisper"),
                           ("deepgram", "deepgram"),
                           ("openai", "openai · Whisper"),
                           ("elevenlabs", "elevenlabs")]),
            Field("tts_provider", "TTS provider", FieldType.SELECT,
                  default="elevenlabs",
                  choices=[("elevenlabs", "elevenlabs"),
                           ("openai", "openai · tts-1"),
                           ("deepgram", "deepgram")]),
            Field("groq_api_key", "Groq API key", FieldType.PASSWORD,
                  help="Used for Groq Whisper STT."),
            Field("deepgram_api_key", "Deepgram API key", FieldType.PASSWORD),
            Field("elevenlabs_api_key", "ElevenLabs API key", FieldType.PASSWORD),
        ],
        probe=probe_twilio,
        needs_gateway_restart=True,
    ),
]


# ── LLM PROVIDERS ──────────────────────────────────────────────────


def _provider_card(
    key: str, label: str, description: str, docs: str,
    config_path: str, probe, *, default_base: str = "",
    fallback_help: str = "",
    key_placeholder: str = "",
    key_help_extra: str = "",
) -> IntegrationCard:
    api_key_help = (
        f"Used to authenticate against {label}. "
        f"{key_help_extra}" if key_help_extra
        else f"Used to authenticate against {label}."
    )
    return IntegrationCard(
        key=key, label=label, category="provider",
        description=description, docs_url=docs,
        config_path=config_path,
        fields=[
            Field("api_key", "API key", FieldType.PASSWORD, required=True,
                  placeholder=key_placeholder or "",
                  help=api_key_help),
            # ``api_base`` is intentionally NOT exposed as a form field.
            # The provider slug picks the URL (see ``_default_base_for``)
            # so picking ``/provider openrouter`` always means
            # openrouter.ai — no chance of accidentally routing through
            # the wrong vendor via a stale override.
            Field("fallback_keys", "Fallback keys (optional)", FieldType.MULTI,
                  placeholder="key1, key2",
                  help=fallback_help or "Used on rate-limits with 60s cooldown."),
        ],
        probe=probe,
        # Provider client is built once at gateway boot. The TUI's setup
        # modal now POSTs to ``/api/provider/reload`` after Save, so the
        # running gateway swaps the client without a manual restart.
        needs_gateway_restart=False,
    )


_PROVIDERS: list[IntegrationCard] = [
    # Flowly hosted goes FIRST — it's the default for signed-in users and
    # the only one that doesn't need a pasted API key. ``custom_action``
    # makes the setup modal render the account-aware view (Sign in / Sign
    # out + "Use as active provider" toggle) instead of a form.
    IntegrationCard(
        key="flowly", label="Flowly (hosted)", category="provider",
        description=(
            "Use your Flowly account's hosted models — no API key needed. "
            "Auth comes from your /login session; the account subscription "
            "covers the LLM bill."
        ),
        docs_url="https://useflowlyapp.com/account",
        config_path="providers.flowly",
        fields=[
            Field("enabled", "Active", FieldType.BOOL, default=True,
                  help="When on AND you're signed in, the agent routes through Flowly."),
            # No api_base field — the Flowly proxy URL is hardcoded in
            # ``_default_base_for("flowly")`` for the same reason BYOK
            # providers don't expose one: picking Flowly always means
            # the canonical proxy.
        ],
        probe=probe_flowly_account,
        custom_action="flowly_account",   # signed-in-aware setup view
        needs_gateway_restart=False,
    ),
    _provider_card("anthropic", "Anthropic",
                   "Claude (Opus, Sonnet, Haiku) via the Anthropic API.",
                   "https://console.anthropic.com/settings/keys",
                   "providers.anthropic", probe_anthropic,
                   default_base="https://api.anthropic.com",
                   key_placeholder="sk-ant-api03-…",
                   key_help_extra="Format: sk-ant-api03-… (from Anthropic Console)."),
    _provider_card("openai", "OpenAI",
                   "GPT-4o, o1, o3 via the OpenAI API.",
                   "https://platform.openai.com/api-keys",
                   "providers.openai", probe_openai,
                   default_base="https://api.openai.com",
                   key_placeholder="sk-proj-…",
                   key_help_extra="Format: sk-proj-… or sk-… (from OpenAI Platform)."),
    _provider_card("openrouter", "OpenRouter",
                   "Unified gateway across 100+ models.",
                   "https://openrouter.ai/keys",
                   "providers.openrouter", probe_openrouter,
                   default_base="https://openrouter.ai/api",
                   key_placeholder="sk-or-v1-…",
                   key_help_extra=(
                       "Format: sk-or-v1-… (from openrouter.ai/keys). "
                       "NOT your Flowly account token."
                   )),
    _provider_card("gemini", "Google Gemini",
                   "Gemini 1.5/2.0 via Google AI Studio.",
                   "https://aistudio.google.com/app/apikey",
                   "providers.gemini", probe_gemini,
                   default_base="https://generativelanguage.googleapis.com",
                   key_placeholder="AIza…",
                   key_help_extra="Format: AIza… (from Google AI Studio)."),
    _provider_card("groq", "Groq",
                   "Ultra-fast LPU inference for Llama, Mixtral, Whisper.",
                   "https://console.groq.com/keys",
                   "providers.groq", probe_groq,
                   default_base="https://api.groq.com/openai",
                   key_placeholder="gsk_…",
                   key_help_extra="Format: gsk_… (from Groq Console)."),
    _provider_card("xai", "xAI",
                   "Grok 2 / Grok 3 via the xAI API.",
                   "https://console.x.ai/",
                   "providers.xai", probe_xai,
                   default_base="https://api.x.ai",
                   key_placeholder="xai-…",
                   key_help_extra="Format: xai-… (from console.x.ai)."),
    IntegrationCard(
        key="xai_oauth", label="xAI Grok OAuth", category="provider",
        description=(
            "Use a connected SuperGrok / X Premium+ subscription through "
            "xAI OAuth. Sign in with your browser — no API key needed."
        ),
        docs_url="https://x.ai/news/grok-opencode",
        config_path="providers.xai_oauth",
        fields=[
            Field("enabled", "Enabled", FieldType.BOOL, default=True,
                  help="When on, stored xAI OAuth tokens can serve Grok requests."),
        ],
        probe=probe_xai_oauth,
        needs_gateway_restart=False,
        custom_action="xai_login",   # browser OAuth flow, not a pasted-key form
    ),
    IntegrationCard(
        key="openai_codex", label="ChatGPT subscription", category="provider",
        description=(
            "Use your ChatGPT Plus / Pro / Team plan through OpenAI's Codex "
            "sign-in. GPT-5.x on your plan's Codex limits — no API key needed."
        ),
        docs_url="https://developers.openai.com/codex/auth",
        config_path="providers.openai_codex",
        fields=[
            Field("enabled", "Enabled", FieldType.BOOL, default=True,
                  help="When on, a stored ChatGPT login can serve GPT-5.x requests."),
        ],
        probe=probe_openai_codex,
        needs_gateway_restart=False,
        custom_action="codex_login",   # browser / device OAuth, not a pasted-key form
    ),
    IntegrationCard(
        key="zai_coding", label="Z.AI GLM Coding Plan", category="provider",
        description=(
            "Use a GLM Coding Plan key through Z.AI's dedicated coding endpoint. "
            "Flowly can reuse an existing OpenCode Z.AI key or store one itself."
        ),
        docs_url="https://docs.z.ai/devpack/quick-start",
        config_path="providers.zai_coding",
        fields=[
            Field("enabled", "Enabled", FieldType.BOOL, default=True,
                  help="When on, a stored or OpenCode-detected GLM Coding Plan key can serve GLM requests."),
        ],
        probe=probe_zai_coding,
        needs_gateway_restart=False,
        custom_action="zai_coding_login",
    ),
    _provider_card("zhipu", "Zhipu GLM",
                   "ChatGLM / GLM-4 from Zhipu AI.",
                   "https://open.bigmodel.cn/usercenter/apikeys",
                   "providers.zhipu", probe_zhipu,
                   default_base="https://open.bigmodel.cn/api/paas"),
    _provider_card("sakana", "Sakana Fugu",
                   "Fugu / Fugu Ultra — multi-agent orchestration models from Sakana AI.",
                   "https://console.sakana.ai/get-started#create-an-api-key",
                   "providers.sakana", probe_sakana,
                   default_base="https://api.sakana.ai/v1"),
]


# ── Media generation ───────────────────────────────────────────────

_MEDIA: list[IntegrationCard] = [
    IntegrationCard(
        key="fal_image",
        label="FAL Image Generation",
        category="media",
        description="Generate images from text (FLUX & more) via fal.ai.",
        docs_url="https://fal.ai/dashboard/keys",
        config_path="tools.image_generation",
        fields=[
            _enabled_field(),
            Field("api_key", "FAL API key", FieldType.PASSWORD, required=True,
                  placeholder="fal-…", help="From fal.ai/dashboard/keys."),
            Field("model", "Image model", FieldType.SELECT,
                  default=_DEFAULT_IMAGE_MODEL, choices=_image_model_choices(),
                  help="Model to generate with — changeable later."),
        ],
        probe=probe_fal_image,
        needs_gateway_restart=True,
    ),
]


# ── WEB SEARCH ─────────────────────────────────────────────────────
# One card per pluggable web-search backend. Each backend is also a
# `kind: backend` plugin (flowly/plugins_bundled/web-<name>/) — the card is
# just the credential surface so it appears in the Desktop / iOS / Android
# connections tab via the shared feature_rpc surface. Cards are added here
# alongside each provider as it lands.


_WEB_SEARCH: list[IntegrationCard] = [
    IntegrationCard(
        key="web_brave", label="Brave Search", category="web_search",
        description="Default web search. Your own Brave API key, or the Flowly "
                    "Cloud search proxy automatically when you're logged in.",
        docs_url="https://brave.com/search/api/",
        config_path="tools.web.search",
        fields=[
            _enabled_field(default=True),
            _default_backend_field(),
            Field("api_key", "Brave API key", FieldType.PASSWORD,
                  placeholder="BSA…",
                  help="Optional — leave empty to use the Flowly proxy when logged in."),
        ],
        probe=probe_brave_search,
        # Search providers are resolved per call (not started at boot), so a
        # key change applies immediately — no gateway restart needed.
        needs_gateway_restart=False,
    ),
    IntegrationCard(
        key="web_ddgs", label="DuckDuckGo (ddgs)", category="web_search",
        description="Free web search, no API key. Needs the ddgs package "
                    "(`pip install ddgs` or `flowly[search]`).",
        docs_url="https://pypi.org/project/ddgs/",
        config_path="tools.web.search.ddgs",
        fields=[_enabled_field(default=False), _default_backend_field()],
        probe=probe_ddgs,
        needs_gateway_restart=False,
    ),
    IntegrationCard(
        key="web_searxng", label="SearXNG", category="web_search",
        description="Privacy-respecting metasearch on your own SearXNG instance.",
        docs_url="https://searx.space/",
        config_path="tools.web.search.searxng",
        fields=[
            _enabled_field(default=False),
            _default_backend_field(),
            Field("url", "Instance URL", FieldType.TEXT,
                  placeholder="http://localhost:8080",
                  help="Base URL of your SearXNG instance."),
        ],
        probe=probe_searxng,
        needs_gateway_restart=False,
    ),
    IntegrationCard(
        key="web_tavily", label="Tavily", category="web_search",
        description="Search + page extraction in one API.",
        docs_url="https://app.tavily.com/home",
        config_path="tools.web.search.tavily",
        fields=[
            _enabled_field(default=False),
            _default_backend_field(),
            Field("api_key", "Tavily API key", FieldType.PASSWORD, placeholder="tvly-…"),
        ],
        probe=probe_tavily,
        needs_gateway_restart=False,
    ),
    IntegrationCard(
        key="web_exa", label="Exa", category="web_search",
        description="Neural/semantic web search with content extraction.",
        docs_url="https://exa.ai",
        config_path="tools.web.search.exa",
        fields=[
            _enabled_field(default=False),
            _default_backend_field(),
            Field("api_key", "Exa API key", FieldType.PASSWORD),
        ],
        probe=probe_exa,
        needs_gateway_restart=False,
    ),
    IntegrationCard(
        key="web_firecrawl", label="Firecrawl", category="web_search",
        description="Strongest extraction (JS-rendered). Cloud key or self-hosted URL.",
        docs_url="https://docs.firecrawl.dev/introduction",
        config_path="tools.web.search.firecrawl",
        fields=[
            _enabled_field(default=False),
            _default_backend_field(),
            Field("api_key", "Firecrawl API key", FieldType.PASSWORD,
                  placeholder="fc-…", help="Leave empty for a self-hosted instance."),
            Field("api_url", "Self-hosted URL", FieldType.TEXT,
                  placeholder="https://firecrawl.example.com",
                  help="Optional — your self-hosted Firecrawl base URL."),
        ],
        probe=probe_firecrawl,
        needs_gateway_restart=False,
    ),
    IntegrationCard(
        key="web_parallel", label="Parallel", category="web_search",
        description="Objective-tuned search + parallel page extraction.",
        docs_url="https://parallel.ai",
        config_path="tools.web.search.parallel",
        fields=[
            _enabled_field(default=False),
            _default_backend_field(),
            Field("api_key", "Parallel API key", FieldType.PASSWORD),
        ],
        probe=probe_parallel,
        needs_gateway_restart=False,
    ),
]


# ── REGISTRY ───────────────────────────────────────────────────────


REGISTRY: list[IntegrationCard] = [
    *_CHANNELS,
    *_TOOLS,
    *_VOICE,
    *_PROVIDERS,
    *_MEDIA,
    *_WEB_SEARCH,
]


_BY_KEY: dict[str, IntegrationCard] = {c.key: c for c in REGISTRY}


def get_card(key: str) -> IntegrationCard | None:
    return _BY_KEY.get(key)


def list_cards(category: str | None = None) -> list[IntegrationCard]:
    if category is None:
        return list(REGISTRY)
    return [c for c in REGISTRY if c.category == category]
