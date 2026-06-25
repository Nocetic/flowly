---
title: Image generation
eyebrow: Features
description: Generate images from a text prompt right in the chat — the picture rides the assistant's own reply on every channel and client, no separate bubble. Powered by FAL (FLUX, Stable Diffusion, Recraft, Ideogram); bring your own FAL key, pick a model in setup, and the agent calls the image_generate tool whenever you ask for a picture.
group: Media
---

Ask Flowly for an image and it appears **inline in the reply** — no separate message, no link to click. Generation runs through [FAL](https://fal.ai) with a model you choose, the file is downloaded to your Flowly home, and Flowly's media-delivery layer surfaces it on whatever surface you're talking from: a Telegram photo, an inline image in the desktop/iOS chat, or an attachment fetched over the gateway.

It's an opt-in tool: it registers **only** when `tools.image_generation` is enabled and has an API key. Bring your own FAL key — there's no hosted image quota in the open-source build.

## Quick start

```text
> draw a watercolor fox sleeping under a maple tree, autumn light
… Flowly: Here's the watercolor fox — soft autumn palette, curled up under
  the maple. (image attached)
```

The agent decides to call `image_generate` on its own when your message asks for a picture. You never invoke the tool directly.

## Setup

The tool is gated behind the **FAL Image Generation** integration card. Configure it any of three ways:

**1. Setup wizard (recommended)**

```bash
flowly setup            # full wizard — pick "FAL Image Generation", paste a key, choose a model
```

Get a key at [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys).

**2. Desktop / remote** — open **Connections**, find **FAL Image Generation**, enter your key and pick a model. This works for local, relay, and self-hosted gateway bots through the same connection RPC.

**3. By hand** — edit `~/.flowly/config.json` (keys are camelCase):

```json
{
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "apiKey": "fal-...",
      "model": "fal-ai/flux/dev"
    }
  }
}
```

Restart the gateway / start a new session after enabling — the tool is wired at agent boot.

## Models

FAL has no clean "list all image models" API, so Flowly ships a curated short list of popular, known-good models. You pick the default in setup; the agent can override it per call via the `model` parameter.

| Model id | Label | Best for |
|---|---|---|
| `fal-ai/flux/schnell` | FLUX.1 [schnell] | Fastest & cheapest |
| `fal-ai/flux/dev` | FLUX.1 [dev] | Balanced quality/speed **(default)** |
| `fal-ai/flux-pro/v1.1` | FLUX1.1 [pro] | Highest quality |
| `fal-ai/stable-diffusion-v35-large` | Stable Diffusion 3.5 Large | Open, versatile |
| `fal-ai/recraft-v3` | Recraft V3 | Strong typography & logos |
| `fal-ai/ideogram/v2` | Ideogram v2 | Best in-image text |

You can also pass **any** FAL text-to-image model id as the `model` argument — the curated list is just what the picker shows.

## The `image_generate` tool

| Parameter | Type | Notes |
|---|---|---|
| `prompt` | string (required) | The image description. **Always written in English** — the agent translates your request if you asked in another language, because most image models understand English best. |
| `image_size` | string | `square_hd` · `square` · `portrait_4_3` · `portrait_16_9` · `landscape_4_3` (default) · `landscape_16_9` |
| `num_images` | integer | How many to generate, **1–4** (clamped). |
| `model` | string | Optional — override the configured default with any FAL model id. |

> [!NOTE]
> Prompts are always sent in English. If you write "kar altında bir kedi çiz", the agent translates the description to English before calling the model — non-English prompts produce noticeably worse results on most image models.

## How delivery works

The generated file is downloaded to **`~/.flowly/media/img-<id>.png`** and then rides the assistant's own reply — there is no separate "here's your image" message. The same picture reaches every surface through Flowly's existing media path:

- **Messaging channels** (Telegram, WhatsApp, …) — sent as a native photo/attachment.
- **Relay-connected apps** (iOS / desktop / web) — uploaded and surfaced from the conversation.
- **Direct gateway clients** (self-hosted iOS / desktop) — delivered as an inline thumbnail with the reply, with the full-resolution original served on demand from `GET /api/media?id=…` (tap to zoom). No relay/S3 needed.

Because the file lives on disk, a generated image still shows correctly when you re-open a past conversation.

## Configuration reference

Everything lives under `tools.imageGeneration` in `~/.flowly/config.json`:

| Key | Type | Meaning |
|---|---|---|
| `enabled` | bool | Master switch. The tool is registered only when this is true **and** `apiKey` is set. |
| `apiKey` | string | Your FAL API key (`fal-…`). Stored on the bot; remote clients only ever see "configured", never the secret. |
| `model` | string | Default model id (one of the curated list, or any FAL model). Defaults to `fal-ai/flux/dev`. |

## Notes & limits

- **Bring your own key** — image generation bills to your FAL account; there is no hosted quota in the open-source build.
- **Synchronous** — Flowly calls FAL's sync endpoint (`POST https://fal.run/{model}`), which suits images (a few seconds). Errors (auth, network, malformed response) come back as a short, user-facing message.
- **`num_images` is capped at 4** per call.
- **Image-editing (image-to-image) is not wired yet** — the model catalog records an `edit_endpoint` for FLUX [dev] for a future `image_edit` tool, but there's no edit tool in this version.

## Related

- [Tools reference](../reference/tools.md)
- [Voice](voice.md) · [Artifacts](artifacts.md)
- [Feature overview](overview.md)
