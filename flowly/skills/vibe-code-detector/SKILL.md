---
name: vibe-code-detector
description: "Estimate whether a PUBLIC website or web app was built with an AI 'vibe coding' tool (Lovable, v0, Bolt.new, Replit, Windsurf, Base44, Framer AI) — and detect the AI-builder stack (shadcn/Tailwind/Supabase) — from the deployed site alone, passively. Ships a stdlib Python scanner that fetches the page + its JS bundles (ordinary GET, no auth testing, no exploitation), matches an updatable signature library, and returns a tiered verdict with the raw evidence and a calibrated confidence. Use when the user asks 'was this site vibe-coded / built with AI', 'is this Lovable/v0/Bolt', 'what AI builder made this site', or wants the tech/provenance fingerprint of a public URL. Heuristic, not proof."
metadata: {"flowly":{"emoji":"🕵️","tags":["vibe-coding","ai-builder","lovable","v0","bolt","fingerprint","detection","web","tech-stack","osint"],"requires":{"bins":["python3"]},"category":"web","related_skills":["privacy-review","api-security-audit","summarize"]}}
---

# Vibe-Code Detector — Was This Site Built With an AI Builder?

Estimates, from a **public deployed site alone**, whether it was produced by an AI "vibe coding" tool (Lovable, v0, Bolt.new, Replit, Windsurf, Base44, Framer AI) or at least built on the AI-builder stack (shadcn/ui + Tailwind + Supabase/Vercel). It fetches the page and a few of its JavaScript bundles with ordinary GET requests, matches them against an updatable signature library, and returns a **tiered verdict + the raw evidence + a calibrated confidence**.

---

## ⚠️ Scope & responsible use — Flowly / Nocetic

Read this before running, and pass its spirit through to the user in your output.

- **Purpose:** heuristic fingerprinting of the *tools/tech a public site was built with*, from publicly served assets only. **Passive** — ordinary GET requests, like a browser loading the page. It performs **no** authentication testing, exploitation, or unauthorized access.
- **Not proof.** Results are **probabilistic**, not a statement of authorship or quality. **False positives are expected** (shared stacks like shadcn / Tailwind / Vercel are used by plenty of hand-coded sites). **Absence of signals does not prove human authorship.**
- **Don't weaponize it.** Do **not** use the output for consequential or discriminatory decisions (hiring, grading, procurement, public accusation), and do not present it as fact. Report "how it was likely built," never a quality verdict; avoid pejorative "slop" framing.
- **Security observations are for disclosure, not access.** If the scan notices a credential in a shipped bundle it is reported **masked**, as awareness for **responsible disclosure to the site owner** — never for access or exploitation.
- Provided as-is, no warranty. **This is not legal advice**; if Flowly/Nocetic ships this publicly, have counsel review. Respect the target site's Terms of Service and local law.

### TR — Kapsam ve sorumlu kullanım

- **Amaç:** yalnızca kamuya açık kaynaklardan, bir sitenin *hangi araç/teknolojiyle kurulduğunu* tahmin etmek. **Pasif** — sadece sıradan GET istekleri; kimlik doğrulama testi, istismar veya yetkisiz erişim **yok**.
- **Kanıt değildir.** Sonuçlar **olasılıksaldır**; yazarlık ya da kalite beyanı değildir. **False-positive beklenir** (shadcn/Tailwind/Vercel'i bir sürü elle yazılmış site de kullanır). **Sinyal yokluğu, "insan yazdı" kanıtı değildir.**
- **Silah olarak kullanma.** Çıktıyı işe alım, notlandırma, ihale veya kamuya suçlama gibi sonuç doğuran/ayrımcı kararlarda **kullanma**; gerçek gibi sunma. "Muhtemelen nasıl yapıldı" de, kalite yargısı verme; aşağılayıcı dil kullanma.
- **Güvenlik gözlemleri ifşa içindir, erişim için değil.** Bir sır fark edilirse **maskelenerek** raporlanır; yalnızca site sahibine **sorumlu ifşa** amaçlıdır.
- Olduğu gibi sunulur, garanti yoktur. **Hukuki tavsiye değildir.** Hedef sitenin kullanım şartlarına ve yerel yasalara uy.

---

## When to use

- "Was this site vibe-coded / made with AI?" · "Is this built with Lovable / v0 / Bolt?"
- "What AI builder made `https://…`?" · "Fingerprint the stack of this URL."
- Curiosity, competitive/market research, or a founder security-awareness check of **their own** app.

Not for: judging a person, gatekeeping, or anything the "responsible use" block rules out.

## How to run

```bash
# live scan (fetches the page + up to 4 JS bundles, passively)
python3 scripts/detect.py https://example.com

# machine-readable
python3 scripts/detect.py https://example.com --json

# if the site blocks bots (Cloudflare, etc.): grab the HTML with the browser
# (summarize/dogfood skill or the user), save it, then:
python3 scripts/detect.py --html-file page.html --url https://example.com
```

Options: `--no-js` (skip bundle fetches, HTML only), `--max-js N`, `--timeout S`, `--headers-file h.json` (pair response headers with `--html-file`).

The scanner is deterministic and self-contained; the signature library lives in `signatures.json` and is meant to be edited/extended without touching the code.

## How the verdict works — tiers, not a naive sum

Weak signals must **never** add up to a strong verdict, so the scanner is tiered and a smoking gun always overrides:

- **Tier A — smoking guns (per platform).** One match ⇒ *detected*. These are load-bearing and hard to strip without breaking the app:
  - **Lovable:** `cdn.gpteng.co` / `gptengineer.js`, `*.lovable.app`/`.dev`, `data-lov-id`/`data-lovable`, `lovable-uploads`.
  - **Bolt.new:** `bolt.new`, StackBlitz WebContainer (`webcontainer.io`, `stackblitz`).
  - **v0:** `v0.dev`, `data-v0-t=`. **Replit:** `*.replit.dev`, `replit.com`. **Windsurf / Base44 / Framer AI:** their own domains / generator meta.
- **Tier B — stack signature (tool unknown).** shadcn/ui (`data-radix-*`, `--radius`/`--primary` CSS vars), dense Tailwind classes, Lucide, Inter/Geist fonts, Supabase (`*.supabase.co/rest/v1`), Firebase, Vercel/Netlify headers, `vc-domain-verify`. **≥2 distinct B categories ⇒ "Likely AI-builder stack."** This is the common vibe-code stack — but a hand-coded site can use it too.
- **Tier C — aesthetic tells (supporting only).** "AI purple" indigo→purple gradient (strongest, weight 2), default `indigo-500`/`purple-600`, rounded cards with ~0.1-opacity shadow, uniform `p-4`/`gap-6`, emoji feature bullets, boilerplate hero copy. **C alone never yields more than "weak/inconclusive."** (Deliberately excluded as unreliable per adversarial ranking: bento grids, glassmorphism, aurora/blob backgrounds — keyword artifacts, high false-positive.)

Verdict bands: `AI builder detected` (Tier A) · `Likely AI-builder stack` (≥2 Tier B) · `Weak / inconclusive` (1 Tier B or ≥3 aesthetic weight) · `No AI-builder signals found` (nothing — and that is **not** proof of human authorship).

## Passive security tells (separate from the authorship verdict)

Vibe-coded apps characteristically leak secrets and skip RLS. The scanner *passively notes* — masked, never exploited — a `service_role` reference, a JWT (anon key is normal for Supabase; note it), and `sk-…` / `AKIA…` / `AIza…` / `sk_live_…` key shapes in shipped JS. Treat these as **corroborating** the vibe-code hypothesis **and** as a responsible-disclosure item for the owner. Do not test, access, or exploit anything — the passive scope is deliberate. (Row-Level-Security probing would require sending queries to their backend; out of scope here.)

## Reliability — be honest in the answer

- **"Which builder" (Tier A) is strong** (~95%+ when a smoking gun is present) and rests on real prior art: Wappalyzer/BuiltWith-style fingerprinting, the VibeCheck detector's weighting scheme, `coderbuds/ai-detector` YAML rules.
- **"Is it AI-generated in general" is weak.** Academic code-origin detectors generalize poorly: text detectors score <60% on code; GPTSniffer ~57%; GPTZero misses most AI code (recall ~0.35). There is **no universal LLM signature**. So on that axis, say *probability, not proof*.
- **Base rate matters.** In one 1,590-page sample, 46% of pages were "clean" (0–1 aesthetic tell) and only 22% heavily slop-patterned — so one or two Tier-C tells mean little on their own.
- Always **show the matched evidence** and let the user judge. Never invent a percentage the scanner didn't produce. State that a negative result is weak.

## Source-available bonus (only if the user has the repo/URL to it)

This skill is deployed-site-only, but if the user *also* has the source, the strongest signals live there: `Co-Authored-By: Claude <noreply@anthropic.com>` (Claude Code, near-certain), `github-copilot[bot]` author (zero false positives), `cursor-`/`codex-` branch prefixes (weaker), and config artifacts `.claude/ .cursor/ .bolt/ CLAUDE.md AGENTS.md components.json`. Mention these as a follow-up when relevant; they are more definitive than any black-box tell.

## Extending

Add or tune fingerprints in `signatures.json` (`tierA_platforms`, `tierB_stack`, `tierC_aesthetic`, `security_passive`). Each signal has a `where` (`host|header|script_src|html|js|any`), a Python `pattern` (matched case-insensitively), and a `note`. Keep Tier A reserved for near-definitive markers so the smoking-gun override stays trustworthy.
