# Flowly Voice Bridge

Real-time voice call bridge for Flowly using Twilio Media Streams.

## Features

- **Real-time Speech-to-Text**: Deepgram (streaming) or OpenAI Whisper
- **Text-to-Speech**: OpenAI TTS with multiple voices
- **Bidirectional Audio**: Twilio Media Streams for low-latency audio
- **Barge-in Support**: Interrupt AI speech with user voice
- **Call Recording**: Optional call recording support
- **Webhook Security**: HMAC signature verification

## Prerequisites

- Node.js >= 20.0.0
- Twilio account with:
  - Account SID and Auth Token
  - Phone number capable of voice calls
- Deepgram API key (for streaming STT) or OpenAI API key (for batch STT)
- OpenAI API key (for TTS)

## Installation

```bash
cd voice-bridge
npm install
npm run build
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required environment variables:

```
# Twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxx
TWILIO_PHONE_NUMBER=+15551234567

# Webhook (use ngrok for local development)
WEBHOOK_BASE_URL=https://your-domain.ngrok.io
WEBHOOK_PORT=8765

# STT (choose one)
STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=xxxxxxxx

# TTS
OPENAI_API_KEY=sk-xxxxxxxx
TTS_VOICE=nova
```

## Usage

### Start the server

```bash
npm start
```

### Make an outbound call

```bash
curl -X POST http://localhost:8765/api/call \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+1234567890",
    "greeting": "Hello, this is Flowly calling. How can I help you?",
    "conversation": true
  }'
```

### Speak on an active call

```bash
curl -X POST http://localhost:8765/api/speak \
  -H "Content-Type: application/json" \
  -d '{
    "callSid": "CAxxxxxxxx",
    "message": "I understand. Let me help you with that."
  }'
```

### End a call

```bash
curl -X POST http://localhost:8765/api/end \
  -H "Content-Type: application/json" \
  -d '{
    "callSid": "CAxxxxxxxx",
    "message": "Thank you for calling. Goodbye!"
  }'
```

### Get call status

```bash
curl http://localhost:8765/api/call/CAxxxxxxxx
```

### List active calls

```bash
curl http://localhost:8765/api/calls
```

## Twilio Configuration

### For Incoming Calls

Configure your Twilio phone number webhook:
- Voice URL: `https://your-domain.ngrok.io/voice/inbound`
- Method: POST

### For Local Development

Use ngrok to expose your local server:

```bash
ngrok http 8765
```

Update `WEBHOOK_BASE_URL` in `.env` with the ngrok URL.

## API Reference

### POST /api/call
Make an outbound call.

**Body:**
- `to` (required): Phone number to call
- `message`: Simple message to speak (one-way call)
- `greeting`: Greeting for conversation call
- `conversation`: Enable two-way conversation (default: false)
- `metadata`: Custom metadata object

### POST /api/speak
Speak on an active call.

**Body:**
- `callSid` (required): Call SID
- `message` (required): Message to speak

### POST /api/end
End a call.

**Body:**
- `callSid` (required): Call SID
- `message`: Optional goodbye message

### GET /api/call/:callSid
Get call details including transcript.

### GET /api/calls
List all active calls.

### GET /health
Health check endpoint.

## Events

The voice bridge emits events that can be used for integration:

- `state_changed`: Call state changed
- `transcription`: User speech transcribed
- `speech_started`: User started speaking
- `speech_ended`: User stopped speaking
- `stream_connected`: Media stream connected
- `stream_disconnected`: Media stream disconnected
- `dtmf`: DTMF digit pressed

## Architecture

```
                    ┌─────────────────┐
                    │   Twilio API    │
                    └────────┬────────┘
                             │
          HTTP Webhooks      │      Media Stream (WSS)
         ┌───────────────────┼───────────────────┐
         │                   │                   │
         ▼                   ▼                   ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   /voice/   │     │   /voice/   │     │   /voice/   │
│   inbound   │     │   status    │     │   stream    │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────────────┴───────────────────┘
                           │
                    ┌──────▼──────┐
                    │Call Manager │
                    └──────┬──────┘
                           │
            ┌──────────────┼──────────────┐
            │              │              │
     ┌──────▼──────┐ ┌─────▼─────┐ ┌─────▼─────┐
     │   Twilio    │ │    STT    │ │    TTS    │
     │  Provider   │ │ Deepgram/ │ │  OpenAI   │
     │             │ │  OpenAI   │ │           │
     └─────────────┘ └───────────┘ └───────────┘
```

## STT Providers

### Deepgram (Recommended for real-time)
- Streaming transcription
- Low latency
- Voice activity detection
- Multiple languages

### OpenAI Whisper
- Batch transcription
- Higher accuracy
- Audio buffered before processing

## TTS Providers

### OpenAI
- High-quality voices: alloy, echo, fable, onyx, nova, shimmer
- Models: tts-1 (fast), tts-1-hd (high quality)
- Converts 24kHz PCM to 8kHz mu-law for Twilio

## License

MIT
