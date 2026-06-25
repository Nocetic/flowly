---
title: Voice — phone calls over Twilio
eyebrow: Features
description: Connect Flowly to the telephone network through Twilio Media Streams for full-duplex voice calls. Flowly transcribes the caller, runs an agent turn, and speaks the reply back.
---

> [!NOTE]
> Voice is disabled by default and must be enabled with Twilio credentials and a public URL.

## Pipeline

Audio formats across the path: Twilio carries **mu-law 8 kHz mono**; STT runs on **PCM 16 kHz**; TTS output is **PCM 24 kHz**, re-encoded to mu-law for Twilio.

**Inbound call.** Twilio `POST /incoming` returns TwiML `<Connect><Stream url="wss://…/media-stream">`, and Twilio opens a Media Stream WebSocket. The stream handler processes `start` (register the stream, mark the call answered), `media` (audio frames), and `stop`.

**Outbound call.** `make_call` POSTs to `api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json` with a `/outgoing` callback URL and a status callback. `/outgoing` returns the same `<Connect><Stream>` TwiML.

### Turn loop

1. Each inbound frame is decoded mu-law → PCM and run through an **energy-based VAD** (threshold 500). Speech is buffered (bounded to ~30s), and silence onset is tracked.
2. A silence detector polls every 100 ms; after **800 ms** of silence the buffered speech is processed.
3. The speech buffer is combined, sub-300 ms blips are skipped, a turn lock prevents overlap, STT transcribes, repeats are deduped, and the text is handed to the transcription handler.
4. The handler builds an `[ACTIVE PHONE CALL]` prompt and runs an agent turn (30s timeout).
5. The reply is queued to TTS, synthesized, re-encoded to mu-law, and sent over the WebSocket in 8000-byte chunks. Flowly sleeps the playback duration, then resumes listening behind a 400 ms suppression guard so it does not hear its own audio.
6. When the call ends, an LLM summary is produced, injected into the session, and (if a Telegram chat id is linked) sent as a Telegram notification.

## STT providers

Only **two** STT providers are implemented. Selecting any other value raises at startup.

| Provider | Endpoint | Model | Auth |
| --- | --- | --- | --- |
| `groq` (default) | `api.groq.com/openai/v1/audio/transcriptions` | `whisper-large-v3-turbo` | Bearer |
| `elevenlabs` | `api.elevenlabs.io/v1/speech-to-text` | `scribe_v1` | `xi-api-key` |

> [!WARNING]
> Although the config schema text mentions `deepgram` and `openai` as STT options, the STT factory does **not** implement them — choosing either raises `Unknown STT provider` at startup. There is no OpenAI Whisper STT. Use `groq` or `elevenlabs` only.

## TTS providers

| Provider | Endpoint | Model / format |
| --- | --- | --- |
| `elevenlabs` (default) | `api.elevenlabs.io/v1/text-to-speech/{voice}` | `eleven_multilingual_v2`, `pcm_24000` |
| `openai` | `api.openai.com/v1/audio/speech` | `tts-1`, voice `nova`, `response_format=pcm` |
| `deepgram` | `api.deepgram.com/v1/speak` | `linear16` |

## Required credentials and config

Voice config lives under `integrations.voice.*` in `~/.flowly/config.json` (camelCase keys).

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | Must be `true`. |
| `twilioAccountSid` | — | Required; gateway will not init voice without it. |
| `twilioAuthToken` | — | Required. |
| `twilioPhoneNumber` | — | Your Twilio number. |
| `webhookBaseUrl` | — | Public HTTPS base for webhooks. **One of** this or `ngrokAuthtoken` is required. |
| `ngrokAuthtoken` | — | If set, Flowly auto-tunnels via ngrok and auto-updates the Twilio number's VoiceUrl. |
| `sttProvider` | `groq` | `groq` or `elevenlabs` only. |
| `ttsProvider` | `elevenlabs` | `elevenlabs`, `openai`, or `deepgram`. |
| `groqApiKey` | — | For Groq STT. |
| `elevenlabsApiKey` | — | For ElevenLabs STT/TTS. |
| `deepgramApiKey` | — | For Deepgram TTS. |
| `ttsVoice` | `21m00Tcm4TlvDq8ikWAM` | Voice id passed to the TTS provider. |
| `language` | `en-US` | — |
| `telegramChatId` | — | Links call summaries to a Telegram chat. |
| `defaultToNumber` | — | Default outbound callee. |
| `skipSignatureVerification` | `false` | Disables Twilio HMAC verification (not recommended). |
| `webhookSecurity` | — | `allowedHosts`, `trustForwardingHeaders`, `trustedProxyIps`. |
| `liveCall` | — | Live-call sandbox settings. |

> [!IMPORTANT]
> You must supply Twilio creds (sid + token + number), **either** `webhookBaseUrl` **or** `ngrokAuthtoken`, and the API key(s) for your chosen STT/TTS providers.

## Webhook signature verification

Inbound Twilio webhooks are authenticated with **Twilio HMAC-SHA1 signature validation**. Requests are also subject to a 1 MB body cap and a host allowlist.

> [!WARNING]
> Verification can be bypassed with `skipSignatureVerification`, but leave it on in any internet-reachable deployment.

## Agent `voice_call` tool

The agent places and controls calls with the `voice_call` tool. Actions: `call`, `speak`, `end_call`, `list_calls`. A `call` resolves greeting/script/default-to-number and forces the destination to E.164. The tool is disabled unless the voice subsystem is wired up. It can be invoked from a scheduled cron tool-call job (the job must set `action: call` and a valid `to`).

## Related

- [Cron](cron.md) — schedule outbound calls.
- [Coaching](coaching.md)
- [Channels overview](../channels/overview.md)
- [Feature overview](overview.md)
- [CLI commands reference](../reference/cli-commands.md)
