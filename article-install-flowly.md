# I Built a Self-Hosted AI Butler. It Runs on My Laptop and I Talk to It From Telegram.

What if you had your own Jarvis — not a cloud service, not a subscription, but an AI that actually runs on your machine?

That's what I built. It's called **Flowly**, and in the next 5 minutes you'll have it running too.

---

## What Flowly actually does

Flowly is an AI agent that lives on your computer. You connect it to Telegram (or Discord, Slack, WhatsApp) and talk to it from your phone. It can:

- Run shell commands on your machine
- Read and write files
- Browse the web
- Take screenshots of your screen
- Schedule recurring tasks
- Make phone calls (yes, real phone calls)
- Manage Docker containers
- And more — it's extensible

The key part: **your data never leaves your machine.** The AI runs through your own API key. No middleman.

```
You (Telegram on your phone)
  → Flowly (running on your laptop)
    → tools, files, APIs
      → response back to Telegram
```

---

## Install in 60 seconds

You need Python 3.11+ and `uv` (the fast Python package manager).

**Don't have uv?**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Clone and set up:**

```bash
git clone https://github.com/Nocetic/flowly.git
cd flowly
uv sync
```

**Run the setup wizard:**

```bash
uv run flowly onboard
```

This creates your config at `~/.flowly/config.json`. You'll need an [OpenRouter API key](https://openrouter.ai/keys) — it gives you access to Claude, GPT-4, Gemini, and 200+ models through a single key.

That's it. Flowly is installed.

---

## Your first conversation

Let's make sure it works:

```bash
uv run flowly agent -m "Hey, what can you do?"
```

You should see Flowly respond with a list of its capabilities. If you want an interactive chat session:

```bash
uv run flowly agent
```

Type. Get answers. It has access to your filesystem, your shell, the web. It's not a chatbot — it's an agent.

---

## Connect Telegram (the fun part)

This is where it gets interesting. Set up a Telegram bot and you can talk to Flowly from anywhere — your phone, your tablet, another country.

1. Open Telegram, search for `@BotFather`, create a new bot, grab the token
2. Get your user ID from `@userinfobot`
3. Add to `~/.flowly/config.json`:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

4. Start the gateway:

```bash
uv run flowly gateway
```

Now open your bot on Telegram and send a message. Flowly responds. From your phone. While it's running on your laptop.

Ask it to check your disk space. Ask it to find a file. Ask it to summarize a webpage. It does it all — because it has access to your machine.

---

## Run it as a background service

You don't want to keep a terminal open. Install Flowly as a system service:

```bash
flowly service install --start
```

It now runs in the background. Survives terminal close. Auto-starts on reboot. Works on macOS (launchd), Linux (systemd), and Windows (Task Scheduler).

Check on it anytime:

```bash
flowly service status
flowly service logs -f
```

---

## Give it personality

Flowly ships with built-in personas. Want your AI to talk like Jarvis?

```bash
flowly persona set jarvis
flowly service restart
```

Available personas: `default`, `jarvis`, `friday`, `pirate`, `samurai`, `casual`, `professor`, `butler`. Or create your own — just drop a markdown file in `~/.flowly/workspace/personas/`.

---

## The stack

- **Python 3.11+** — core runtime
- **LiteLLM** — unified API for any LLM (Claude, GPT-4, Gemini, open-source models)
- **OpenRouter** — single API key for all models
- **Channels** — Telegram, WhatsApp, Discord, Slack
- **Voice** — Twilio + ElevenLabs/Deepgram/Groq for real-time phone calls

Everything is open source: [github.com/Nocetic/flowly](https://github.com/Nocetic/flowly)

---

## TL;DR

```bash
# Install
git clone https://github.com/Nocetic/flowly.git && cd flowly
uv sync

# Setup
uv run flowly onboard

# Chat from terminal
uv run flowly agent -m "What's my IP address?"

# Or connect Telegram and chat from your phone
uv run flowly gateway
```

Your own AI. On your machine. Reachable from anywhere.

No subscription. No cloud dependency. Just you and your butler.

---

*Flowly is Apache 2.0 licensed and open source. Star it on [GitHub](https://github.com/Nocetic/flowly) if you find it useful.*
