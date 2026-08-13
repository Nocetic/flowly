"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class MemoryFlushConfig(BaseModel):
    """Pre-compaction memory flush configuration."""
    enabled: bool = True
    soft_threshold_tokens: int = 4000
    prompt: str = (
        "Pre-compaction memory flush. "
        "Store durable memories now (use memory/YYYY-MM-DD.md). "
        "If nothing to store, reply with NO_REPLY."
    )
    system_prompt: str = (
        "Pre-compaction memory flush turn. "
        "The session is near auto-compaction; capture durable memories to disk."
    )


class MicrocompactConfig(BaseModel):
    """Truncation of old tool results, applied before full compaction."""
    enabled: bool = True
    keep_recent_full: int = Field(default=5, ge=1)
    truncate_chars: int = Field(default=200, ge=50)


class KeepRecentConfig(BaseModel):
    """How much recent conversation survives compaction verbatim."""
    enabled: bool = True
    min_tokens: int = Field(default=5000, ge=0)
    min_messages: int = Field(default=3, ge=0)
    max_tokens: int = Field(default=20000, ge=1)


class CompactionConfig(BaseModel):
    """Context compaction configuration."""
    mode: Literal["default", "safeguard"] = "safeguard"
    # Headroom kept free for the model's reply and the next prompt. Must stay
    # well under the context window or compaction would trigger on every turn.
    reserve_tokens_floor: int = Field(default=20000, ge=1000)
    max_history_share: float = Field(default=0.5, ge=0.1, le=0.9)
    # Fallback only — the real window is detected from the model catalog
    # unless this is set explicitly in config.json.
    context_window: int = Field(default=128000, ge=4000)
    memory_flush: MemoryFlushConfig = Field(default_factory=MemoryFlushConfig)
    microcompact: MicrocompactConfig = Field(default_factory=MicrocompactConfig)
    keep_recent: KeepRecentConfig = Field(default_factory=KeepRecentConfig)

    @model_validator(mode="after")
    def _check_budget_fits(self) -> "CompactionConfig":
        """A reserve at or above the window leaves no room to converse."""
        if self.reserve_tokens_floor >= self.context_window:
            raise ValueError(
                f"compaction.reserveTokensFloor ({self.reserve_tokens_floor}) must be "
                f"smaller than compaction.contextWindow ({self.context_window})"
            )
        return self


class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""
    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers


class IMessageConfig(BaseModel):
    """iMessage channel configuration (macOS only).

    Inbound reads the local Messages database (needs Full Disk Access);
    outbound goes through Messages.app via the desktop bridge or
    AppleScript. v1 replies to existing conversations only — starting a
    chat with a never-messaged address is not supported.
    """
    enabled: bool = False
    poll_interval_seconds: float = 2.0  # chat.db tail frequency
    db_path: str = ""  # default ~/Library/Messages/chat.db (tests override)
    allow_from: list[str] = Field(default_factory=list)  # Phone numbers (E.164) or emails
    dm_policy: Literal["open", "pairing", "allowlist"] = "pairing"  # DM access policy
    group_policy: Literal["mention", "open", "allowlist"] = "mention"  # Group chat gating
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed group chat_identifiers
    mention_patterns: list[str] = Field(default_factory=list)  # Wake words (regex); default "@flowly"
    # Delivery via a BlueBubbles server (a separate signed macOS app that
    # holds the Automation + Full Disk Access grants). When set, the
    # channel runs in full BlueBubbles mode: INBOUND via a local webhook
    # BlueBubbles POSTs to, OUTBOUND via its REST API. Flowly then needs
    # NO macOS permissions of its own — no chat.db read, no AppleScript,
    # no FDA — sidestepping the TCC -10004 wall entirely. Leave blank to
    # use the direct chat.db + AppleScript path.
    bluebubbles_url: str = ""  # e.g. http://127.0.0.1:1234
    bluebubbles_password: str = ""  # BlueBubbles server password
    bluebubbles_webhook_host: str = "127.0.0.1"  # webhook listener bind
    bluebubbles_webhook_port: int = 8642  # webhook listener port


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    dm_policy: Literal["open", "pairing", "allowlist"] = "pairing"  # DM access policy


class DiscordConfig(BaseModel):
    """Discord channel configuration."""
    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    group_policy: str = "mention"  # Server channels: "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Channel IDs if allowlist
    group_context: str = "listen"  # Passive read of un-answered channel msgs: "listen" | "off"


class SlackDMConfig(BaseModel):
    """Slack DM policy configuration."""
    enabled: bool = True
    policy: str = "open"  # "open" or "allowlist"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs


class SlackConfig(BaseModel):
    """Slack channel configuration."""
    enabled: bool = False
    mode: str = "socket"  # "socket" supported
    bot_token: str = ""  # xoxb-...
    app_token: str = ""  # xapp-...
    group_policy: str = "mention"  # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed channel IDs if allowlist
    group_context: str = "listen"  # Passive read of un-answered channel msgs: "listen" | "off"
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class WebChannelConfig(BaseModel):
    """Web chat channel configuration (relay mode — no SSH needed)."""
    enabled: bool = False
    relay_url: str = ""  # set by Flowly Cloud pairing; self-host: leave empty
    server_id: str = ""    # Flowly server ID (from deployment)
    auth_token: str = ""   # gatewayAuthToken (from deployment)
    jwt_secret: str = ""   # JWT signing secret (for relay authentication)


class EmailConfig(BaseModel):
    """Email/Gmail channel configuration (OAuth-based)."""
    enabled: bool = False
    poll_interval_seconds: int = 30  # Check for new emails every N seconds
    allow_from: list[str] = Field(default_factory=list)  # Allowed sender emails


class TeamsConfig(BaseModel):
    """Microsoft Teams channel — incoming-webhook outbound only (Faz 1).

    Faz 1 is one-way (bot → Teams channel). The user creates an
    "Incoming Webhook" connector in their Teams channel and pastes the
    resulting URL into ``webhook_url``. Suitable for notifications,
    cron output, alerts, daily summaries.

    Faz 2 (bidirectional via Bot Framework + Azure AD) will add the
    Graph API path and reuse the same TeamsChannel — the webhook
    fields stay backward-compatible.
    """
    enabled: bool = False
    webhook_url: str = ""             # Teams Incoming Webhook URL (HTTPS)
    default_chat_label: str = ""      # Human-friendly label for the target channel
    allow_from: list[str] = Field(default_factory=list)  # Reserved for Faz 2 inbound


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    web: WebChannelConfig = Field(default_factory=WebChannelConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    imessage: IMessageConfig = Field(default_factory=IMessageConfig)


class HeartbeatActiveHours(BaseModel):
    """Active hours window for heartbeat — outside this window heartbeat is skipped."""
    start: str = "09:00"  # HH:MM (24h)
    end: str = "23:00"    # HH:MM (24h)
    timezone: str = ""    # IANA timezone, e.g. "Europe/Istanbul". Empty = system local.


class HeartbeatConfig(BaseModel):
    """Periodic heartbeat configuration — wakes the agent to check for tasks."""
    enabled: bool = True
    every_minutes: int = 30
    active_hours: HeartbeatActiveHours | None = None
    # Where to deliver non-OK heartbeat responses:
    # "none" = don't deliver, "message_tool" = agent uses message tool (needs channel config)
    deliver: str = "none"


class MemorySearchConfig(BaseModel):
    """Memory search / indexing configuration."""
    enabled: bool = True
    # Embedding provider: "auto", "openai", "gemini", "none"
    # "auto" tries openai then gemini from providers config; "none" = FTS5 keyword only
    provider: str = "auto"
    model: str = ""           # Override embedding model (default: provider's default)
    api_key: str = ""         # Override API key for embeddings (defaults to provider config)
    api_base: str = ""        # Override API base URL
    chunk_tokens: int = 400   # Target tokens per chunk
    overlap_tokens: int = 80  # Overlap between consecutive chunks
    max_results: int = 6      # Max search results returned
    min_score: float = 0.35   # Minimum relevance score (0-1)
    vector_weight: float = 0.7   # Weight for vector similarity in hybrid score
    text_weight: float = 0.3     # Weight for BM25 keyword score in hybrid score


class MemoryDreamingConfig(BaseModel):
    """Cross-session memory consolidation ("dreaming") configuration.

    The dreamer reads conversation deltas across sessions, extracts candidate
    memories, reconciles them against the governance store + KG, and commits the
    survivors through the lifecycle status machine. Disabled by default until the
    feature is rolled out; turning it on does not change how facts are stored,
    only adds the governance/lifecycle layer on top.
    """
    # ON by default: existing configs don't carry this key, so the Python loader
    # fills this default on update → the feature auto-enables for bot + Desktop
    # (Desktop never writes memory_dreaming, so the default always wins). Users
    # opt OUT by setting it false in config.json (or a Desktop toggle).
    enabled: bool = True
    # Commit policy: "selective" auto-activates high-confidence/unconflicted
    # items and queues the rest for review; "manual" queues everything;
    # "aggressive" lowers the bar (not recommended).
    commit_mode: str = "selective"
    # Triggers.
    idle_minutes: int = 30          # run after this much agent inactivity
    daily_enabled: bool = True
    daily_time: str = "03:30"       # HH:MM local
    turn_interval: int = 10         # also run every N user turns (coarse pass)
    # Commit thresholds (against calibrated confidence in P3; raw before that).
    auto_floor: float = 0.80        # >= → auto-active (if unconflicted, not sensitive)
    review_floor: float = 0.55      # < → rejected
    # Bound per run so a backlog can't blow up a single pass.
    max_messages_per_run: int = 500
    # Autonomous consolidation (cleanup) — runs in the background, gated on
    # "dirty" (new writes since last pass) so it doesn't burn tokens for nothing.
    auto_consolidate: bool = True
    consolidate_turn_interval: int = 50   # also run every N user turns (0=off)
    consolidate_every_minutes: int = 30   # background timer interval (0=off)
    # Freeze the injected memory block (MEMORY.md + KG summary) per session so the
    # Anthropic prefix cache stays stable across a session/compaction window. OFF
    # by default — flip ON only after a measured cache-hit before/after proves a
    # gain with no regression. Tradeoff: a mid-session write isn't re-injected into
    # the system prompt until the next snapshot boundary (it's on disk + reachable
    # via memory_search/recall; the agent already knows what it just wrote).
    freeze_injected_memory: bool = False


class SkillImprovementConfig(BaseModel):
    """Autonomous skill self-improvement (creation + consolidation).

    The agent mines its own trajectories for recurring procedures and proposes/
    applies new skills, and consolidates the skill library (merge narrow siblings
    into umbrellas, archive stale). Auto-apply with safety rails: pre-run snapshot
    + rollback, archive-only (never delete), dry-run, pinned protection, first-run
    deferral. Off by default until rolled out.
    """
    enabled: bool = False
    mine_enabled: bool = True            # trajectory-miner (creation) source
    curate_enabled: bool = True          # consolidation (curator) source
    mine_turn_interval: int = 0          # also run every N user turns (0=off, timer-preferred)
    mine_every_minutes: int = 360        # background timer interval (0=off) — low frequency
    curate_every_minutes: int = 720
    stale_after_days: int = 60           # active→stale when unused this long
    stale_min_uses: int = 1
    max_messages_per_run: int = 1000
    min_evidence_sessions: int = 2       # don't propose a skill from a single session
    min_repeat_count: int = 3            # repeated-procedure threshold
    snapshot_keep: int = 10              # rollback snapshots to retain


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    workspace: str = "~/.flowly/workspace"
    cwd: str = ""
    """Default runtime working directory for shell commands (``exec``) and
    delegated coding subprocesses (``codex_session``). Empty → use the
    workspace. ``~`` is expanded. Overridden per-call by an explicit
    ``working_dir``, per-session by the gateway, and by the ``FLOWLY_CWD``
    env var. See flowly/runtime_cwd.py for the full resolution order."""
    model: str = "anthropic/claude-haiku-4.5"
    max_tokens: int = 8192
    # Per-request LLM call timeout (seconds). A hung or slow provider call is
    # aborted after this instead of hanging and accumulating token charges.
    # The FLOWLY_LLM_TIMEOUT_SECONDS env var still wins when set (power users);
    # otherwise this config value is used. Surfaced in the desktop Settings UI.
    llm_timeout_seconds: int = 120
    temperature: float = 0.7
    action_temperature: float = 0.1
    action_tool_retries: int = 2
    # Hard runaway cap; not a target. Multi-step browser flows (search → upload
    # → verify) routinely need 20-40 tool calls. The model normally stops on
    # its own via stop_reason="end_turn"; this is just a guard against an
    # infinite tool spam scenario. Follows a "let the model decide"
    # philosophy with a safety net.
    max_tool_iterations: int = 100
    # Inject a one-shot nudge at this iteration count: "you've been working a
    # while — keep going if making progress, otherwise tell the user what's
    # blocking you". Helps the model self-evaluate without hard-stopping it.
    soft_warn_at_iteration: int = 30
    # Legacy compatibility knob. Agent requests deliberately do not use this
    # as a hard history ceiling: the complete working history must reach token
    # budgeting so older turns are either retained or represented by a
    # successful compaction summary, never silently sliced away.
    context_messages: int = 100
    persona: str = "default"  # Bot persona (default, jarvis, pirate, etc.)
    save_trajectories: bool = False  # Save conversation turns as ShareGPT JSONL
    memory_nudge_interval: int = 10   # Background memory review every N user turns (0=disabled)
    skill_nudge_interval: int = 15    # Deprecated/no-op: self-review no longer creates skills
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    memory_search: MemorySearchConfig = Field(default_factory=MemorySearchConfig)
    memory_dreaming: MemoryDreamingConfig = Field(default_factory=MemoryDreamingConfig)
    skill_improvement: SkillImprovementConfig = Field(default_factory=SkillImprovementConfig)


class MultiAgentConfig(BaseModel):
    """Single agent configuration for multi-agent orchestration."""
    name: str = ""
    provider: str = "anthropic"  # "anthropic", "openai", "flowly"
    model: str = ""  # Short name ("sonnet", "opus") or full model ID
    working_directory: str = ""  # Default: ~/.flowly/agents/{id}/
    persona: str = ""


class MultiAgentTeamConfig(BaseModel):
    """Team of agents for chain collaboration."""
    name: str = ""
    agents: list[str] = Field(default_factory=list)
    leader_agent: str = ""


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    agents: dict[str, MultiAgentConfig] = Field(default_factory=dict)
    teams: dict[str, MultiAgentTeamConfig] = Field(default_factory=dict)
    # Per-specialist model override: assistant name → model id. Empty/absent
    # means the specialist inherits the bot's selected model. Lets a user run,
    # say, the researcher on a cheap fast model and the writer on a strong one.
    assistant_models: dict[str, str] = Field(default_factory=dict)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    api_key: str = ""
    api_base: str | None = None
    # Additional API keys to rotate through when the primary key fails.
    # Keys are tried in order; failed keys enter a 60-second cooldown.
    fallback_keys: list[str] = Field(default_factory=list)


class XAIOAuthConfig(BaseModel):
    """xAI Grok subscription OAuth configuration.

    Tokens are stored in the OS keychain / ``~/.flowly/credentials`` rather
    than in config.json. ``client_id`` is public but must belong to Flowly's
    own xAI OAuth app; users normally should not need to edit it.
    """
    enabled: bool = True
    client_id: str = ""
    api_base: str = "https://api.x.ai/v1"


class OpenAICodexConfig(BaseModel):
    """ChatGPT subscription (OpenAI Codex OAuth) configuration.

    Tokens are stored in the OS keychain / ``~/.flowly/credentials`` rather
    than in config.json. The OAuth client id is the public Codex CLI client,
    baked in as a constant (not configurable). When enabled and signed in via
    ``flowly codex login``, stored tokens serve GPT-5.x / Codex requests billed
    against the user's ChatGPT plan.
    """
    enabled: bool = True
    # Let eligible Responses models emit encrypted server-side checkpoints.
    # Local summary compaction stays enabled as the independent fallback.
    native_compaction: bool = True
    native_compaction_threshold: int = Field(default=200_000, ge=1_024)


class ZaiCodingConfig(BaseModel):
    """Z.AI GLM Coding Plan configuration.

    The plan key is stored in the OS keychain / ``~/.flowly/credentials``
    rather than config.json. Flowly can also read an existing OpenCode
    ``auth.json`` entry as a fallback. ``api_base`` is pinned to the dedicated
    Coding Plan endpoint so requests use the subscription quota, not the
    regular Zhipu/Z.AI metered API surface.
    """
    enabled: bool = True
    api_base: str = "https://api.z.ai/api/coding/paas/v4"


class FlowlyHostedConfig(BaseModel):
    """Use the user's Flowly account to access hosted Anthropic/OpenAI/etc.

    Auth comes from the Flowly account (Firebase ID token) — there's no
    api_key field here. When ``enabled`` is True and the user is signed in
    via ``/login``, the runtime injects ``account.id_token`` as the bearer
    token and routes through ``api_base`` (``useflowlyapp.com/api/v1`` by
    default).

    Disable this to fall back to BYOK providers below (anthropic / openai /
    openrouter / …). Disabling it does NOT sign the user out — pairing
    for iOS still works.
    """
    enabled: bool = True   # default on so logging in immediately gives LLM access
    api_base: str = "https://useflowlyapp.com/api/v1"
    # Flowly account credential pushed by the Desktop app (the only minter) so
    # the bot can use the Flowly hosted provider — billed to the account —
    # WITHOUT a server record / relay (channels.web stays untouched).
    #
    # `account_key` (an `flw_…` account API key) is the canonical path: the
    # proxy resolves it straight to the account, no server registration. The
    # `server_id`/`auth_token` pair is the legacy/relay path (proxy bearer
    # `serverId:authToken`) and is kept for relay-registered bots. account_key
    # wins when both are present.
    account_key: str = ""
    server_id: str = ""
    auth_token: str = ""


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers.

    ``active`` is the **explicit default provider** — the one the next LLM
    request will use. When non-empty, it overrides the implicit cascade
    (was: "first non-empty api_key wins", which silently surprised users
    who added a second key). When empty (the back-compat default), we
    fall back to the cascade.

    One global choice, set via UI / ``/provider`` slash command, not
    inferred from credential presence.
    """
    active: str = ""   # "" = use cascade; otherwise the provider slug to use
    flowly: FlowlyHostedConfig = Field(default_factory=FlowlyHostedConfig)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    sakana: ProviderConfig = Field(default_factory=ProviderConfig)  # Fugu / Fugu Ultra (OpenAI-compat)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)  # For voice transcription
    xai: ProviderConfig = Field(default_factory=ProviderConfig)  # xAI Grok models
    xai_oauth: XAIOAuthConfig = Field(default_factory=XAIOAuthConfig)  # xAI Grok subscription OAuth
    openai_codex: OpenAICodexConfig = Field(default_factory=OpenAICodexConfig)  # ChatGPT subscription OAuth
    zai_coding: ZaiCodingConfig = Field(default_factory=ZaiCodingConfig)  # Z.AI GLM Coding Plan


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = "127.0.0.1"
    port: int = 18790
    # Static remote-access token for self-hosted desktop clients (no Flowly
    # account). Empty = local-only, no auth. Set/rotated by ``flowly gateway``
    # when binding to a non-loopback host. See flowly/gateway/auth.py.
    token: str = ""

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {v}")
        return v


class DdgsProviderConfig(BaseModel):
    """DuckDuckGo (ddgs) backend — no key, opt-in toggle."""
    enabled: bool = False
    default: bool = False  # mark as the active web search backend


class SearxngProviderConfig(BaseModel):
    """SearXNG backend — self-hosted instance URL + opt-in toggle."""
    enabled: bool = False
    default: bool = False  # mark as the active web search backend
    url: str = ""


class TavilyProviderConfig(BaseModel):
    """Tavily backend — search + extract via API key."""
    enabled: bool = False
    default: bool = False
    api_key: str = ""


class ExaProviderConfig(BaseModel):
    """Exa backend — semantic search + extract via API key."""
    enabled: bool = False
    default: bool = False
    api_key: str = ""


class FirecrawlProviderConfig(BaseModel):
    """Firecrawl backend — search + extract; cloud key or self-hosted URL."""
    enabled: bool = False
    default: bool = False
    api_key: str = ""
    api_url: str = ""  # self-hosted Firecrawl instance (optional)


class ParallelProviderConfig(BaseModel):
    """Parallel.ai backend — search + extract via API key."""
    enabled: bool = False
    default: bool = False
    api_key: str = ""


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    enabled: bool = True  # Brave/default backend on/off (connections card toggle)
    default: bool = False  # mark Brave as the active web search backend
    api_key: str = ""  # Brave Search API key (self-hosted, optional)
    max_results: int = 5
    proxy_url: str = ""  # Flowly Cloud search proxy; empty + logged-in falls back to canonical /api/v1/search; self-host: use BRAVE_API_KEY instead
    # Pluggable backend selection. Empty = auto (availability-ordered,
    # Brave first). ``backend`` is the shared selector; ``search_backend`` /
    # ``extract_backend`` override per capability. Values are provider names
    # ("brave", "ddgs", "searxng", "tavily", "exa", "firecrawl", ...).
    backend: str = ""
    search_backend: str = ""
    extract_backend: str = ""
    # Per-provider sub-configs (also written by the connections cards).
    ddgs: DdgsProviderConfig = Field(default_factory=DdgsProviderConfig)
    searxng: SearxngProviderConfig = Field(default_factory=SearxngProviderConfig)
    tavily: TavilyProviderConfig = Field(default_factory=TavilyProviderConfig)
    exa: ExaProviderConfig = Field(default_factory=ExaProviderConfig)
    firecrawl: FirecrawlProviderConfig = Field(default_factory=FirecrawlProviderConfig)
    parallel: ParallelProviderConfig = Field(default_factory=ParallelProviderConfig)


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(BaseModel):
    """Command execution tool configuration.

    NOTE: the exec **policy** (security mode + ask mode + allowlist) lives in
    the approvals store (``~/.flowly/credentials/exec-approvals.json``), which
    is the single source of truth the executor enforces. It is intentionally
    NOT mirrored here — older builds had ``security``/``ask`` fields on this
    model, but nothing read them at runtime, so they silently did nothing.
    Legacy keys still present in a user's config.json are ignored on load and
    migrated into the store once (see ``ExecApprovalStore.load``).
    Only ``enabled`` and ``cron_mode`` below are read from config.json.
    """
    enabled: bool = True
    timeout_seconds: int = 300  # 5 minutes default
    max_output_chars: int = 200000  # 200KB
    approval_timeout_seconds: int = 120  # 2 minutes to approve
    # Policy for commands that would need approval while running inside a
    # cron job. No user is present to click approve, so the default is
    # "deny" — scheduled runs can only execute already-allowlisted
    # commands. Set to "approve" to bypass approval prompts entirely
    # (dangerous, trust-the-schedule mode).
    cron_mode: Literal["deny", "approve"] = "deny"

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        if not 1 <= v <= 3600:
            raise ValueError(f"timeout_seconds must be between 1 and 3600, got {v}")
        return v


class TrelloConfig(BaseModel):
    """Trello integration configuration."""
    api_key: str = ""  # Get at https://trello.com/app-key
    token: str = ""  # Generate from the same page


class XConfig(BaseModel):
    """X (Twitter) API configuration."""
    bearer_token: str = ""  # App-only Bearer Token (read operations)
    api_key: str = ""  # OAuth 1.0a Consumer Key (write operations)
    api_secret: str = ""  # OAuth 1.0a Consumer Secret
    access_token: str = ""  # OAuth 1.0a Access Token
    access_token_secret: str = ""  # OAuth 1.0a Access Token Secret


class VoiceWebhookSecurityConfig(BaseModel):
    """Voice webhook security configuration."""
    allowed_hosts: list[str] = Field(default_factory=list)
    trust_forwarding_headers: bool = False
    trusted_proxy_ips: list[str] = Field(default_factory=list)


class VoiceLiveCallConfig(BaseModel):
    """Live-call tool sandbox policy."""
    strict_tool_sandbox: bool = True
    allow_tools: list[str] = Field(
        default_factory=lambda: ["voice_call", "message", "screenshot", "system"]
    )


class VoiceBridgeConfig(BaseModel):
    """Integrated voice plugin configuration for Twilio calls."""
    enabled: bool = False
    # Legacy bridge fallback API URL (optional, disabled by default)
    bridge_url: str = "http://localhost:8765"
    legacy_bridge_enabled: bool = False

    # Twilio settings
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Webhook URL (static public URL for Twilio callbacks)
    webhook_base_url: str = ""
    skip_signature_verification: bool = False
    webhook_security: VoiceWebhookSecurityConfig = Field(default_factory=VoiceWebhookSecurityConfig)
    live_call: VoiceLiveCallConfig = Field(default_factory=VoiceLiveCallConfig)

    # Link voice calls to Telegram session (for screenshots, messages etc.)
    telegram_chat_id: str = ""  # Your Telegram chat ID - voice calls will use this session
    default_to_number: str = ""  # Optional default target phone for "beni ara" requests

    # STT/TTS settings
    stt_provider: str = "groq"  # groq, deepgram, openai, or elevenlabs
    tts_provider: str = "elevenlabs"  # openai, deepgram, or elevenlabs
    groq_api_key: str = ""  # For Groq Whisper STT
    deepgram_api_key: str = ""  # For Deepgram STT/TTS
    elevenlabs_api_key: str = ""  # For ElevenLabs STT/TTS
    tts_voice: str = "21m00Tcm4TlvDq8ikWAM"  # TTS voice (provider-specific, default: rachel)
    language: str = "en-US"

    # Ngrok auto-tunnel (alternative to manual webhook_base_url)
    ngrok_authtoken: str = ""  # ngrok authtoken from https://dashboard.ngrok.com


class GoogleWorkspaceConfig(BaseModel):
    """Google Workspace integration configuration."""
    enabled: bool = False
    email: str = ""  # Connected Google account email


class LinearConfig(BaseModel):
    """Linear integration configuration."""
    api_key: str = ""  # Personal API key from Linear Settings → API


class GitHubConfig(BaseModel):
    """GitHub integration configuration.

    The ``github`` tool registers when ``token`` is non-empty. ``token`` is a
    personal access token (classic or fine-grained) with repo scope.
    ``default_repo`` ("owner/name") is an optional fallback when the tool
    can't infer the repo from the open project's origin remote.
    """
    token: str = ""
    default_repo: str = ""


class SentryConfig(BaseModel):
    """Sentry integration configuration.

    The ``sentry`` tool registers when both ``token`` and ``org`` are set.
    ``token`` is an auth token (Settings → Auth Tokens); ``org`` is the org
    slug. ``default_project`` is an optional project slug used by list_issues.
    """
    token: str = ""
    org: str = ""
    default_project: str = ""


class HomeAssistantConfig(BaseModel):
    """Home Assistant integration configuration.

    Tools (ha_list_entities, ha_get_state, ha_list_services, ha_call_service)
    register only when both ``url`` and ``token`` are non-empty. URL must point
    to the HA instance on the local network (default Bonjour: homeassistant.local:8123).
    Token is a Long-Lived Access Token from HA Profile → Long-Lived Access Tokens.
    """
    url: str = ""  # e.g. "http://homeassistant.local:8123"
    token: str = ""  # Long-Lived Access Token


class ObsidianConfig(BaseModel):
    """Obsidian vault integration configuration.

    Treats the vault as a local-first Markdown knowledge source. Tools
    (obsidian_search/read/list/write/append/ingest) register only when
    ``enabled`` is true and ``vault_path`` resolves to a directory.

    Vault content is untrusted: it is searched/cited and optionally turned
    into *review-gated* memory candidates, but is never written to memory
    automatically and never dumped wholesale into the prompt.
    """
    enabled: bool = False
    vault_path: str = ""  # absolute path; falls back to OBSIDIAN_VAULT_PATH / ~/Documents/Obsidian Vault
    index_enabled: bool = True  # build a local FTS index for fast search
    # "off": never auto-inject; "on_demand": inject top-k snippets only when the
    # user message looks like it needs the vault (keyword-gated).
    auto_inject: Literal["off", "on_demand"] = "on_demand"
    # How aggressively vault-derived facts may become memory:
    #   manual_only    – only the obsidian_ingest tool creates candidates
    #   review_gated   – candidates created but require explicit user accept (default)
    #   selective_auto – reserved for future use; treated as review_gated for now
    ingestion_policy: Literal["manual_only", "review_gated", "selective_auto"] = "review_gated"
    include_globs: list[str] = Field(default_factory=lambda: ["**/*.md"])
    exclude_globs: list[str] = Field(
        default_factory=lambda: [".obsidian/**", ".trash/**", ".git/**", "node_modules/**"]
    )
    max_note_bytes: int = 1_000_000  # skip notes larger than this in index/read


class ElevenLabsConfig(BaseModel):
    """ElevenLabs as a voice provider in its own right.

    The key used to live inside the Twilio voice card, which made it look like
    a telephony detail — you could only reach it by configuring phone calls you
    might not want, and nothing suggested the same key could also read a reply
    aloud or write a song.

    Everything here is chosen from the user's OWN account: the voices are the
    ones in their library, the models the ones their plan can run. That is the
    whole reason a voice provider is not an entry in a shared catalog.

    The old location (``integrations.voice.elevenlabs_api_key``) is still read,
    so nobody has to re-enter anything; see
    :func:`flowly.voice.settings.resolve_elevenlabs`.
    """
    enabled: bool = False
    api_key: str = ""
    #: Which voice speaks — a voice id from the account's own library.
    voice_id: str = ""
    #: Which model reads it. Empty means "not chosen yet"; Flowly will not pick
    #: one, because a model is a price the user never saw.
    model_id: str = ""
    #: Which model composes. Empty means Flowly will not write music.
    music_model_id: str = ""


class IntegrationsConfig(BaseModel):
    """External integrations configuration."""
    trello: TrelloConfig = Field(default_factory=TrelloConfig)
    voice: VoiceBridgeConfig = Field(default_factory=VoiceBridgeConfig)
    x: XConfig = Field(default_factory=XConfig)
    google_workspace: GoogleWorkspaceConfig = Field(default_factory=GoogleWorkspaceConfig)
    linear: LinearConfig = Field(default_factory=LinearConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    sentry: SentryConfig = Field(default_factory=SentryConfig)
    home_assistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    elevenlabs: ElevenLabsConfig = Field(default_factory=ElevenLabsConfig)


class ArtifactConfig(BaseModel):
    """Artifact persistence configuration."""
    enabled: bool = True
    max_content_length: int = 500000  # 500KB


class BrowserTabConfig(BaseModel):
    """Browser tab tool configuration.

    Requires a connected browser provider: the desktop app's embedded
    Flowly Browser or the Flowly Chrome extension.
    """
    enabled: bool = False


class ComputerConfig(BaseModel):
    """Computer use (desktop automation) configuration."""
    enabled: bool = False
    action_delay_ms: int = 100
    failsafe: bool = True


class CodexSessionToolConfig(BaseModel):
    """``codex_session`` tool configuration.

    Optional integration: enables the ``codex_session`` tool that
    delegates coding-heavy turns to OpenAI's Codex CLI ``app-server``.
    Disabled by default — only useful for users who:

      * Have the Codex CLI installed (``npm i -g @openai/codex``).
      * Are logged in (``codex login``).
      * Want to use Codex's sandboxed coding workflow (its own
        ``shell`` / ``apply_patch`` tools) instead of Flowly's
        exec / read_file / write_file dispatch.

    Codex is invoked as a subprocess; its tools run inside Codex's
    own sandbox + approval flow. Flowly is the orchestrator. With the
    Flowly tool-callback enabled (``expose_flowly_tools``), Codex can
    also reach back into a curated subset of Flowly's tools (web
    search, skills, …) via MCP.
    """

    enabled: bool = False
    """If False, the codex_session tool is not registered at all and
    Flowly has zero dependency on the Codex CLI. Set True to opt in.
    """

    codex_bin: str = "codex"
    """Absolute path or PATH-lookup name for the codex executable."""

    codex_home: str = ""
    """Override for ``$CODEX_HOME`` (where Codex stores ``auth.json``
    and thread state). Empty string → Codex's default (``~/.codex``).
    """

    cwd: str = ""
    """Working directory the Codex subprocess starts in. Codex uses
    this as the implicit root for ``exec`` / ``apply_patch`` calls.
    Empty string → Flowly process's own cwd.
    """

    turn_timeout_s: int = 600
    """Hard ceiling on wall-clock time for one Codex turn (seconds)."""

    post_tool_quiet_timeout_s: int = 90
    """Wedge-detection threshold (seconds). If Codex has run at least
    one tool but then goes silent this long without a new notification
    or ``turn/completed``, we interrupt the turn and retire the
    session. Mirrors Codex's upstream post-tool watchdog.
    """

    approval_policy: Literal["on-request", "never", "auto-review", "granular"] = "on-request"
    """Codex's per-thread approval policy. ``on-request`` is the
    safest default — Codex prompts before destructive actions.
    """

    sandbox: Literal["read-only", "workspace-write", "full-access"] = "workspace-write"
    """Codex sandbox level. ``workspace-write`` (default) lets Codex
    write inside the project but not to system paths. ``read-only``
    disables file writes entirely. ``full-access`` removes sandbox
    boundaries — use with extreme care.
    """

    expose_flowly_tools: bool = True
    """When True, register Flowly's own tool surface as an MCP server
    in ``~/.codex/config.toml`` so the Codex subprocess can call back
    into Flowly for tools Codex doesn't ship with (web search, skills,
    etc.). Migration runs when the runtime is enabled / a session
    starts. Set False to keep Codex fully isolated.
    """


class ImageGenerationConfig(BaseModel):
    """Legacy image-only settings, superseded by :class:`MediaGenerationConfig`.

    Kept so an existing ``tools.imageGeneration`` block on disk keeps working —
    it is read at startup and folded into the media settings, so nobody loses
    their key or their chosen model on upgrade. New writes go to
    ``tools.mediaGeneration``.
    """
    enabled: bool = False
    provider: str = "fal"
    model: str = "fal-ai/flux/dev"
    api_key: str = ""


class MediaGenerationDefaults(BaseModel):
    """The model used for each kind of generation when the agent doesn't pick.

    Every field defaults to empty, meaning "not chosen yet" — a real default
    here would silently outrank the model an upgrading user already picked in
    the older image-only config. The fallback chain lives in
    :func:`flowly.media.settings.resolve_media_settings`, which is the only
    place that can see both blocks at once.
    """
    text_to_image: str = ""
    image_to_image: str = ""
    text_to_video: str = ""
    image_to_video: str = ""


class MediaGenerationConfig(BaseModel):
    """Image AND video generation (provider-backed media).

    Off by default; the tools register only when ``enabled`` and an ``api_key``
    are present. One key and one provider now cover both kinds — they are the
    same account and the same catalog, and splitting them would mean asking for
    the same credential twice.
    """
    enabled: bool = False
    provider: str = "fal"
    api_key: str = ""
    defaults: MediaGenerationDefaults = Field(default_factory=MediaGenerationDefaults)
    # Ceiling for one video generation before it is cancelled provider-side.
    # Generous: a 1080p clip genuinely takes minutes.
    video_timeout_seconds: int = 900


class ToolDiscoveryConfig(BaseModel):
    """Progressively disclose large tool schemas to the model."""

    enabled: bool = True
    deferred_toolsets: list[str] = Field(default_factory=lambda: [
        "mcp",
        "extensions",
    ])
    always_visible_tools: list[str] = Field(default_factory=list)
    minimum_deferred_schema_tokens: int = Field(default=750, ge=0, le=100_000)
    catalog_max_chars: int = Field(default=6_000, ge=500, le=50_000)
    search_default_limit: int = Field(default=5, ge=1, le=50)
    search_max_limit: int = Field(default=20, ge=1, le=100)
    # Deterministically expose only the external schemas that match the current
    # user request. This avoids an LLM discovery round without making the full
    # MCP/plugin catalog eager.
    intent_routing_enabled: bool = True
    intent_max_promoted_tools: int = Field(default=4, ge=0, le=20)
    intent_min_score: float = Field(default=4.0, ge=0.0, le=100.0)
    # Local, API-key-free semantic selection for external tools. It indexes
    # static tool metadata only and is independent from conversation memory.
    # This is an explicit opt-in: a fresh install must neither initialize nor
    # download the local model until the owner accepts the recommendation.
    # Any local model/index failure falls back to the lexical catalog.
    semantic_routing_enabled: bool = False
    semantic_model_auto_download: bool = False
    semantic_routing_consent: Literal["undecided", "enabled", "dismissed"] = "undecided"
    semantic_recommendation_min_tools: int = Field(default=12, ge=1, le=10_000)
    semantic_recommendation_min_schema_tokens: int = Field(
        default=2_500,
        ge=0,
        le=1_000_000,
    )
    semantic_recommendation_growth_tools: int = Field(default=8, ge=1, le=10_000)
    semantic_recommendation_growth_schema_tokens: int = Field(
        default=2_000,
        ge=0,
        le=1_000_000,
    )
    semantic_recommendation_dismissed_tool_count: int = Field(default=0, ge=0)
    semantic_recommendation_dismissed_schema_tokens: int = Field(default=0, ge=0)
    semantic_source_min_score: float = Field(default=0.78, ge=-1.0, le=1.0)
    semantic_source_min_margin: float = Field(default=0.009, ge=0.0, le=2.0)
    semantic_tool_min_score: float = Field(default=0.76, ge=-1.0, le=1.0)
    semantic_tool_score_window: float = Field(default=0.035, ge=0.0, le=2.0)
    semantic_query_cache_size: int = Field(default=256, ge=1, le=10_000)


class ToolRoutingConfig(BaseModel):
    """Model-facing platform/toolset selection policy."""

    enabled: bool = True
    platform_toolsets: dict[str, list[str]] = Field(default_factory=dict)
    disabled_toolsets: list[str] = Field(default_factory=list)
    availability_cache_ttl_seconds: float = Field(default=30.0, ge=0.0, le=300.0)
    availability_failure_grace_seconds: float = Field(default=60.0, ge=0.0, le=600.0)
    schema_cache_ttl_seconds: float = Field(default=10.0, ge=0.0, le=300.0)
    # Keep direct core tools visible while moving only their large operating
    # playbooks behind a deterministic, host-side intent router.  Disabling
    # this is the instant rollback path to the legacy eager prompt.
    capability_guidance_enabled: bool = True
    # A routing error or ambiguous UI action loads the legacy guides instead
    # of attempting to save tokens.  Behaviour wins over prompt size.
    capability_guidance_fail_open: bool = True
    # Startup skill metadata is progressive: relevant descriptions plus a
    # compact complete name index.  Full SKILL.md bodies remain available via
    # skill_view and explicit capability activation.
    skill_catalog_max_chars: int = Field(default=8_000, ge=1_000, le=50_000)
    discovery: ToolDiscoveryConfig = Field(default_factory=ToolDiscoveryConfig)


class ToolsConfig(BaseModel):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    artifact: ArtifactConfig = Field(default_factory=ArtifactConfig)
    browser_tab: BrowserTabConfig = Field(default_factory=BrowserTabConfig)
    computer: ComputerConfig = Field(default_factory=ComputerConfig)
    codex_session: CodexSessionToolConfig = Field(default_factory=CodexSessionToolConfig)
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    media_generation: MediaGenerationConfig = Field(default_factory=MediaGenerationConfig)
    routing: ToolRoutingConfig = Field(default_factory=ToolRoutingConfig)


class MediaRetentionConfig(BaseModel):
    """How long generated media is kept in ``~/.flowly/media``.

    Age is about relevance, so one cap covers every kind. Size is about disk,
    and video is one to two orders of magnitude larger than an image — a shared
    budget would mean generating video silently evicts screenshots somebody
    still wanted, so each kind gets its own. Stray non-media files ride the
    image budget; they are rare and small.

    ``retention_days = -1`` disables the age cap; a size budget of ``0``
    disables that budget. A clip and its poster frame are always deleted
    together — they are one attachment, not two files.
    """
    enabled: bool = True
    retention_days: int = 30
    image_max_size_mb: int = 500
    video_max_size_mb: int = 2000
    # Added when the media library made audio browsable. An existing config
    # without the key picks up this default, so nobody has to migrate.
    audio_max_size_mb: int = 1000


class MediaConfig(BaseModel):
    """Settings for the media Flowly stores on this machine."""
    retention: MediaRetentionConfig = Field(default_factory=MediaRetentionConfig)


class AuditConfig(BaseModel):
    """Audit log retention configuration.

    The audit logger always writes records to ``~/.flowly/audit/YYYY-MM-DD.jsonl``;
    these settings only control how long files are kept. ``enabled`` controls
    whether retention runs at all (when False, files accumulate forever).
    Set ``retention_days=-1`` or ``max_size_mb=0`` to disable that individual cap.
    """
    enabled: bool = True
    retention_days: int = 90
    max_size_mb: int = 100


class PluginsConfig(BaseModel):
    """Plugin enablement configuration.

    Bundled plugins (shipped in ``flowly/plugins_bundled/``) load by
    default unless their key is listed in ``disabled``.  User-installed
    plugins (``$FLOWLY_HOME/plugins/<name>/``) only load when their key
    appears in ``enabled``.

    The ``disabled`` list overrides ``enabled`` and applies to bundled
    plugins as well.
    """
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class MCPSamplingConfig(BaseModel):
    """Per-server sampling (server-initiated LLM) policy. Off by default."""
    enabled: bool = False
    model: str = ""  # force a specific model (else honor server hint)
    max_rpm: int = 10  # sliding-window requests/minute cap
    max_tokens_cap: int = 4096  # clamp the server's requested maxTokens
    allowed_models: list[str] = Field(default_factory=list)  # whitelist (empty = any)


class MCPServerToolsFilter(BaseModel):
    """Per-server tool filter and utility-tool toggles."""
    # If non-empty, only these tool names register (whitelist).
    include: list[str] = Field(default_factory=list)
    # If non-empty and include is empty, exclude these names (blacklist).
    exclude: list[str] = Field(default_factory=list)
    # resources/* and prompts/* utility tools — Faz 2 actually wires them up.
    resources: bool = False
    prompts: bool = False


class MCPServerConfig(BaseModel):
    """One MCP server entry under top-level ``mcpServers``.

    Either ``command`` (stdio) or ``url`` (HTTP/SSE) must be set; the
    discovery layer rejects entries with neither at boot time.
    """
    enabled: bool = True
    # stdio
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # http / sse
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    # TLS / mTLS (HTTP/SSE only): ssl_verify is True | False | CA-path.
    # client_cert is a combined PEM path or [cert, key]/[cert, key, pw].
    ssl_verify: bool | str = True
    client_cert: str | list[str] = ""
    client_key: str = ""
    # common
    timeout: float = 120.0  # per-tool-call timeout in seconds
    connect_timeout: float = 60.0  # initial connection timeout
    tools: MCPServerToolsFilter = Field(default_factory=MCPServerToolsFilter)
    # Declared now so users can pre-populate; Faz 2 reads them.
    transport: Literal["auto", "stdio", "http", "sse"] = "auto"
    auth: Literal["", "oauth"] = ""
    scope: str = ""  # optional OAuth scope string (space-separated)
    supports_parallel_tool_calls: bool = False
    # Opt-in (default off): force-kill stdio child processes that appear
    # during this server's spawn and survive teardown. Only useful on
    # Linux where setsid() children can escape cleanup on cancellation.
    # Off by default because the spawn-window child diff can, in rare
    # races, attribute an unrelated subprocess to this server.
    reap_orphans: bool = False
    # OSV malware gate: before an npx/uvx/pipx stdio server spawns, query
    # the OSV API for MAL-* advisories on the package. Default on (cheap,
    # fail-open). Set false to skip for a trusted/local server.
    osv_check: bool = True
    # Server-initiated LLM (sampling/createMessage). Off by default.
    sampling: MCPSamplingConfig = Field(default_factory=MCPSamplingConfig)


class PetDisplayConfig(BaseModel):
    """Optional floating desktop pet (Petdex companion). Off by default."""
    enabled: bool = False
    slug: str = ""
    scale: float = 0.33  # default mirrors flowly/pet/constants.DEFAULT_SCALE

    @field_validator("scale")
    @classmethod
    def _clamp_scale(cls, v: float) -> float:
        # Mirror flowly.pet.constants SCALE_MIN/SCALE_MAX inline so the config
        # module stays dependency-free (config must not import the pet package).
        return max(0.1, min(3.0, float(v)))


class DisplayConfig(BaseModel):
    """Cosmetic display options surfaced by the desktop app."""
    pet: PetDisplayConfig = Field(default_factory=PetDisplayConfig)


class Config(BaseSettings):
    """Root configuration for flowly."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    mcp_servers: dict[str, MCPServerConfig] = Field(
        default_factory=dict, alias="mcpServers",
    )
    background_mode: bool = Field(default=False, alias="backgroundMode")

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def get_api_key(self) -> str | None:
        """Get API key in priority order: OpenRouter > Anthropic > OpenAI > xAI > Gemini > Zhipu > vLLM."""
        return (
            self.providers.openrouter.api_key or
            self.providers.anthropic.api_key or
            self.providers.openai.api_key or
            self.providers.xai.api_key or
            self.providers.gemini.api_key or
            self.providers.zhipu.api_key or
            self.providers.sakana.api_key or
            self.providers.vllm.api_key or
            None
        )

    def get_active_provider_name(self) -> str:
        """Return the name of the active provider (highest-priority configured key)."""
        if self.providers.openrouter.api_key:
            return "openrouter"
        if self.providers.anthropic.api_key:
            return "anthropic"
        if self.providers.openai.api_key:
            return "openai"
        if self.providers.xai.api_key:
            return "xai"
        if self.providers.gemini.api_key:
            return "gemini"
        if self.providers.zhipu.api_key:
            return "zhipu"
        if self.providers.sakana.api_key:
            return "sakana"
        if self.providers.vllm.api_key:
            return "vllm"
        return "unknown"

    def get_fallback_keys(self) -> list[str]:
        """Return fallback_keys for the active provider."""
        name = self.get_active_provider_name()
        provider_cfg = getattr(self.providers, name, None)
        if provider_cfg is None:
            return []
        return provider_cfg.fallback_keys

    def get_api_base(self) -> str | None:
        """Get API base URL if using OpenRouter, xAI, Zhipu or vLLM."""
        if self.providers.openrouter.api_key:
            return self.providers.openrouter.api_base or "https://openrouter.ai/api/v1"
        if self.providers.xai.api_key:
            return self.providers.xai.api_base or "https://api.x.ai/v1"
        if self.providers.zhipu.api_key:
            return self.providers.zhipu.api_base
        if self.providers.sakana.api_key:
            return self.providers.sakana.api_base or "https://api.sakana.ai/v1"
        if self.providers.vllm.api_base:
            return self.providers.vllm.api_base
        return None

    class Config:
        env_prefix = "FLOWLY_"
        env_nested_delimiter = "__"
        populate_by_name = True
        extra = "ignore"
