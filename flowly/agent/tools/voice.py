"""Voice call tool for making and managing phone calls.

This tool integrates with the voice plugin for direct agent control.
"""

from typing import Any, TYPE_CHECKING

from loguru import logger

from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.voice.plugin import VoicePlugin


class VoiceCallTool(Tool):
    """
    Tool to make and manage voice calls via the integrated voice plugin.

    The voice plugin handles Twilio, STT, and TTS directly, giving
    the agent full tool access during voice conversations.
    """

    def __init__(self, voice_plugin: "VoicePlugin | None" = None):
        """
        Initialize the voice call tool.

        Args:
            voice_plugin: Optional voice plugin instance. If not provided,
                          the tool will be disabled.
        """
        self._voice_plugin = voice_plugin
        self._telegram_chat_id: str | None = None

    def set_voice_plugin(self, voice_plugin: "VoicePlugin"):
        """Set the voice plugin instance."""
        self._voice_plugin = voice_plugin

    def set_context(self, channel: str, chat_id: str):
        """Set the current context for linking calls to Telegram."""
        if channel == "telegram":
            self._telegram_chat_id = chat_id
            logger.info(f"Voice tool context set: telegram:{chat_id}")

    @property
    def name(self) -> str:
        return "voice_call"

    @property
    def description(self) -> str:
        return """Make and manage voice phone calls.

Actions:
- call: Make a call to a phone number
- speak: Speak a message on an active call
- end_call: End a call (optionally with goodbye message)
- list_calls: List all active calls

Phone numbers should be in E.164 format (e.g., +1234567890).

When you make a call:
1. The system calls the phone number
2. Listens for the user's speech
3. Transcribes it and sends to you
4. Your response is automatically spoken back

Examples:
- Make a call: voice_call(action="call", to="+1234567890", greeting="Hello, how can I help you?")
- End call: voice_call(action="end_call", call_sid="CA...", message="Goodbye!")
- List calls: voice_call(action="list_calls")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": ["call", "speak", "end_call", "list_calls"],
                },
                "to": {
                    "type": "string",
                    "description": "Phone number to call (for action=call)",
                },
                "greeting": {
                    "type": "string",
                    "description": "Initial greeting message (for action=call)",
                },
                "script": {
                    "type": "string",
                    "description": "Full first message/script to speak immediately after call is answered (for action=call)",
                },
                "message": {
                    "type": "string",
                    "description": "Message to speak (for action=speak, end_call)",
                },
                "call_sid": {
                    "type": "string",
                    "description": "Call SID (for speak, end_call)",
                },
            },
            "required": ["action"],
            # Keep schema provider-compatible: some providers reject top-level
            # allOf/anyOf/oneOf. Action-specific validation is enforced in execute().
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        """Execute a voice call action."""
        if not self._voice_plugin:
            return "Error: Voice plugin is not enabled. Configure it in ~/.flowly/config.json under integrations.voice"

        try:
            if action == "call":
                return await self._make_call(
                    to=kwargs.get("to", ""),
                    greeting=kwargs.get("greeting"),
                    script=kwargs.get("script"),
                )
            elif action == "speak":
                return await self._speak(
                    call_sid=kwargs.get("call_sid", ""),
                    message=kwargs.get("message", ""),
                )
            elif action == "end_call":
                return await self._end_call(
                    call_sid=kwargs.get("call_sid", ""),
                    message=kwargs.get("message"),
                )
            elif action == "list_calls":
                return await self._list_calls()
            else:
                return f"Unknown action: {action}"

        except Exception as e:
            logger.error(f"Voice call error: {e}")
            return f"Error: {str(e)}"

    def _resolve_initial_greeting(self, greeting: str | None, script: str | None) -> str:
        """Resolve the first spoken message for a new call."""
        if greeting and greeting.strip():
            return greeting.strip()
        if script and script.strip():
            return script.strip()
        return "Hello!"

    def _resolve_default_to_number(self) -> str | None:
        """Resolve default target phone number from voice plugin config, if available."""
        plugin_config = getattr(self._voice_plugin, "config", None)
        if not plugin_config:
            return None
        voice_cfg = getattr(getattr(plugin_config, "integrations", None), "voice", None)
        if not voice_cfg:
            return None
        number = getattr(voice_cfg, "default_to_number", "") or ""
        number = str(number).strip()
        return number or None

    async def _make_call(self, to: str, greeting: str | None = None, script: str | None = None) -> str:
        """Make a call."""
        to_number = str(to or "").strip() or self._resolve_default_to_number()
        if not to_number:
            return (
                "Error: 'to' phone number is required. "
                "You can also set integrations.voice.default_to_number in config."
            )
        # Ensure E.164 format
        if to_number[0].isdigit():
            to_number = f"+{to_number}"

        initial_greeting = self._resolve_initial_greeting(greeting, script)

        # Pass telegram_chat_id and greeting to make_call
        # Greeting will be queued when call is answered (not before!)
        call_sid = await self._voice_plugin.make_call(
            to_number=to_number,
            telegram_chat_id=self._telegram_chat_id,
            greeting=initial_greeting,
        )

        logger.info(
            f"Call initiated: {call_sid} with telegram_chat_id={self._telegram_chat_id}, "
            f"greeting_len={len(initial_greeting)}"
        )

        return f"""📞 Call initiated!

Call SID: {call_sid}
To: {to_number}
Opening message: {initial_greeting}

The call is being placed. When the user answers and speaks, their words will appear in the conversation.
Your responses will be automatically spoken to them."""

    async def _speak(self, call_sid: str, message: str) -> str:
        """Speak on an active call."""
        if not call_sid:
            return "Error: 'call_sid' is required"
        if not message:
            return "Error: 'message' is required"

        call = self._voice_plugin.call_manager.get_call(call_sid)
        if not call:
            return f"Error: Call not found: {call_sid}"

        await self._voice_plugin.call_manager.speak(call_sid, message)

        return f"🗣️ Speaking on call {call_sid}: \"{message}\""

    async def _end_call(self, call_sid: str, message: str | None = None) -> str:
        """End a call."""
        if not call_sid:
            return "Error: 'call_sid' is required"

        await self._voice_plugin.end_call(call_sid, message)

        if message:
            return f"📞 Call ended with message: \"{message}\""
        return f"📞 Call {call_sid} ended."

    async def _list_calls(self) -> str:
        """List active calls."""
        calls = self._voice_plugin.call_manager.list_active_calls()

        if not calls:
            return "📞 No active calls."

        lines = ["📞 Active Calls:\n"]
        for call in calls:
            lines.append(f"• {call.call_sid}")
            lines.append(f"  From: {call.from_number} → To: {call.to_number}")
            lines.append(f"  Status: {call.status} | Duration: {call.duration_seconds:.0f}s")
            lines.append("")

        return "\n".join(lines)
