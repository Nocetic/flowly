# Flowly Skills

This directory contains built-in skills that extend Flowly's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

## Available Skills

| Skill | Description |
|-------|-------------|
| `github` | Interact with GitHub using the `gh` CLI |
| `google-workspace` | Google Workspace (Drive, Gmail, Calendar, Sheets, Docs, Chat) via `gws` |
| `weather` | Get weather info using wttr.in and Open-Meteo |
| `summarize` | Summarize URLs, files, and YouTube videos |
| `tmux` | Remote-control tmux sessions |
| `skill-creator` | Create new skills |
| `plan` | Plan-only mode: write a markdown plan to `.flowly/plans/` instead of executing |
| `writing-plans` | Author bite-sized implementation plans with exact paths, code, and verification |
| `systematic-debugging` | 4-phase root-cause debugging discipline |
| `test-driven-development` | RED-GREEN-REFACTOR enforcement: tests before code |
| `requesting-code-review` | Pre-commit verification: static scan + reviewer subagent + auto-fix loop |
| `subagent-driven-development` | Execute plans via `delegate_to` subagents with two-stage review |
| `design-md` | Author/lint/export Google's DESIGN.md design-token spec files |
| `apple-notes` | Manage Apple Notes via the `memo` CLI (macOS) |
| `apple-reminders` | Manage Apple Reminders via the `remindctl` CLI (macOS) |
| `imessage` | Send / read iMessage and SMS via the `imsg` CLI (macOS) |
| `findmy` | Track Apple devices and AirTags via FindMy.app + AppleScript (macOS) |
| `arxiv` | Search arXiv papers + Semantic Scholar (citations, recommendations) |
| `polymarket` | Query Polymarket prediction markets (markets, prices, orderbooks, history) |
| `llm-wiki` | Build/query an interlinked markdown knowledge base (Karpathy pattern) |
| `literature-review` | Systematic literature reviews, paper matrices, evidence maps, and synthesis |
| `paper-deep-dive` | Deep-read scientific papers: claims, methods, figures, limitations, follow-ups |
| `statistical-analysis` | Scientific CSV analysis: EDA, test choice, effect sizes, reporting |
| `research-methods` | Study design: hypotheses, controls, confounders, preregistration, validity |
| `reproducible-research` | Environment capture, data provenance, seeds, manifests, rerun reports |
| `scientific-peer-review` | Review scientific papers, grants, datasets, code, and analyses for rigor |
| `lab-notebook` | Scientific lab notes: protocols, observations, deviations, raw data links |
| `scientific-writing` | Manuscripts, abstracts, rebuttals, related work, captions, limitations |
| `codebase-inspection` | Inspect codebases with pygount (LOC, languages, ratios) |
| `github-pr-workflow` | GitHub PR lifecycle (branch → commit → push → PR → CI → merge) via gh/curl |
| `github-code-review` | Local pre-push review + on-GitHub PR review (inline + summary comments) |
| `ascii-art` | ASCII art (pyfiglet, cowsay, boxes, image-to-ascii, curated art) |
| `architecture-diagram` | Dark-themed SVG architecture diagrams as standalone HTML |
| `excalidraw` | Hand-drawn Excalidraw JSON diagrams (arch, flow, sequence) |
| `maps` | Geocoding, POIs, routes, timezones via OpenStreetMap/OSRM (no API key) |
| `notion` | Notion API + ntn CLI (pages, databases, markdown, Workers) — requires `NOTION_API_KEY` |
| `linear` | Linear issues/projects/documents via GraphQL — requires `LINEAR_API_KEY` |
| `airtable` | Airtable REST API: CRUD, filters, upserts — requires `AIRTABLE_API_KEY` |
| `ocr-and-documents` | Extract text from PDFs/scans (pymupdf for fast text, marker-pdf for OCR) |
| `nano-pdf` | Edit PDF text/typos/titles via nano-pdf CLI using natural-language prompts |
| `youtube-content` | YouTube transcripts → structured outputs (chapters, summaries, threads, blogs) |
| `xurl` | X (Twitter) via official xurl CLI — post, search, DM, media, full v2 API |
| `memento-flashcards` | Spaced-repetition flashcards with chat-graded free-text answers + YouTube quiz |
| `obsidian` | Read, search, create, and edit filesystem-first Obsidian vault notes |
| `duckduckgo-search` | DuckDuckGo search via the `ddgs` CLI |
| `rest-graphql-debug` | Debug REST/GraphQL APIs: status codes, auth, schemas, repro |
| `node-inspect-debugger` | Debug Node.js via `--inspect` and Chrome DevTools Protocol |
| `python-debugpy` | Debug Python with `pdb`, `debugpy`, and `remote-pdb` |
| `spike` | Throwaway experiments to validate an idea before build |
| `github-issues` | Create, triage, label, assign, and search GitHub issues |
| `github-repo-management` | Clone/create/fork repos; manage remotes, releases, and settings |
| `macos-computer-use` | Drive macOS desktop apps through Flowly's `computer` tool |
| `dogfood` | Systematic browser-based exploratory QA for web apps |
| `adversarial-ux-test` | Test products as hostile or tech-resistant user personas |
| `blogwatcher` | Monitor blogs and RSS/Atom feeds with `blogwatcher-cli` |
| `watchers` | Poll RSS, GitHub, and JSON endpoints with watermark deduplication |
| `p5js` | Build production p5.js sketches, generative art, and canvas exports |
| `pixel-art` | Convert images into retro pixel art and short looped animations |
| `manim-video` | Create Manim CE educational/math/algorithm explainer videos |
| `meme-generation` | Generate real meme PNG files from templates or custom images |
| `concept-diagrams` | Produce flat SVG educational diagrams as standalone HTML |
| `excel-author` | Build auditable openpyxl Excel workbooks headlessly |
| `dcf-model` | Build institutional DCF valuation models in Excel |
| `3-statement-model` | Build integrated income statement, balance sheet, and cash-flow models |
| `pptx-author` | Generate python-pptx decks with model-backed numbers |
| `sec-filings` | Read/dissect SEC filings (10-K/10-Q/S-1/8-K) from EDGAR; red-flag extraction |
| `earnings-analysis` | Earnings call + release + 10-Q: beat/miss, guidance, KPIs, margin bridge, Q&A |
| `comps-analysis` | Trading comps — peer selection, multiples, normalization, implied valuation range |
| `lbo-model` | LBO returns — sources & uses, debt sweep, IRR/MOIC, exit sensitivity grid |
| `macro-research` | Interpret CPI/jobs/GDP/Fed/yield curve; regime call + macro note |
| `credit-analysis` | Creditworthiness — leverage/coverage scorecard, maturity wall, covenants, memo |
| `portfolio-review` | Holdings exposure, concentration (HHI), factor/sector risk, rebalancing notes |
| `risk-modeling` | VaR/CVaR (historical + parametric), vol, drawdown, stress scenarios, sensitivity |
| `real-estate-underwriting` | NOI, cap rate, DSCR, cash-on-cash, levered IRR + sensitivity for property deals |
| `startup-unit-economics` | CAC, LTV, payback, churn/NRR, burn multiple, magic number, Rule of 40, runway |
| `crypto-token-analysis` | Tokenomics — supply/FDV, unlocks/emissions, allocation, value accrual, risk flags |
| `merger-model` | M&A accretion/dilution, financing mix, PPA/goodwill, synergies, breakeven |
| `economic-indicators` | Read a data release vs consensus (surprise/z-score), trend, leading/lagging, calendar |
| `prediction-market-research` | Market-implied probability vs evidence — edge, calibration, resolution risk |
| `contract-review` | Plain-English contract review — red flags, missing protections, negotiation points |
| `privacy-review` | Data-privacy review — PII map, GDPR/CCPA checklist, gaps + PII/tracker scanner |
| `policy-drafting` | Draft enforceable internal policies (AUP, security, retention, handbook sections) |
| `regulatory-research` | Cited regulatory summaries — applicability, obligations, penalties, changes |
| `openscad` | Parametric 3D CAD as code (.scad → STL/3MF) with a render helper |
| `cadquery` | Python parametric B-rep CAD — fillets/chamfers, STEP export for CAM |
| `3d-printing` | Design-for-FDM, slicer settings, material guide + stdlib STL analyzer |
| `circuit-analysis` | DC/AC circuits, filters, op-amps, SPICE + EE calculator (Ohm/divider/E-series) |
| `mechanical-engineering` | Statics, beams, stress, FoS, fasteners, gears + mechanics calculator |
| `engineering-units` | Unit conversion + dimensional analysis with a dimension-guarded converter |
| `control-systems` | Stability (Routh-Hurwitz), PID tuning, 2nd-order response + calculator |
| `pcb-kicad` | KiCad headless: ERC/DRC, Gerbers, BOM, fab files via a kicad-cli wrapper |
| `signal-processing` | Sampling/Nyquist, FFT/spectrum, filters + stdlib DFT & dominant-frequency helper |
| `thermodynamics` | Cycles/Carnot/COP, ideal gas, conduction/convection/radiation, R-networks + calc |
| `fluid-mechanics` | Reynolds, Bernoulli, Darcy-Weisbach head loss, pump power + flow calculator |
| `robotics-kinematics` | FK/IK (DH + 2-link closed form), Jacobian, singularities + kinematics calc |
| `gcode-cnc` | Feeds & speeds, G-code gen + safety check, material/tool data calculator |
| `materials-selection` | Ashby index ranking + property database (metals/polymers/composites) |
| `tolerance-analysis` | Worst-case + RSS stack-ups, fits, Cpk + stack-up calculator |
| `power-sizing` | Motor/battery/solar/wire sizing with constraint checks + calculator |
| `sql-query` | Write/optimize/explain SQL — joins, windows, indexing, EXPLAIN, schema design |
| `regex-builder` | Build/test/explain regex + stdlib tester (matches, groups, sub, validate) |
| `docker` | Dockerfiles (multi-stage, cache, non-root) + compose + stdlib Dockerfile linter |
| `kubernetes` | Manifests + kubectl debugging (CrashLoop/Pending/OOM/endpoints) |
| `ab-testing` | Experiment design, sample size, significance + stdlib A/B calculator |
| `data-visualization` | Chart selection + honest encoding + matplotlib/plotly code generation |
| `business-case` | Problem→options→ROI/payback/NPV→recommendation memo + ROI calculator |
| `market-sizing` | TAM/SAM/SOM top-down + bottom-up reconciled + sizing calculator |
| `competitor-analysis` | Feature/pricing matrix, positioning, moat, SWOT, white-space |
| `pricing-strategy` | Value metric, tiers/packaging, WTP, discounting, price tests |
| `sales-call-analysis` | Transcript → pains, objections, MEDDICC/BANT, next steps, follow-up |
| `customer-research` | Interview synthesis, JTBD, personas, pain maps, unbiased research design |
| `product-requirements` | PRDs, user stories, Given/When/Then acceptance, edge cases, launch checklist |
| `ops-runbook` | SOPs, incident playbooks, severity/escalation, blameless postmortems |
| `chemistry` | Balance equations, molar mass, stoichiometry, molarity + chemistry calculator |
| `physics-solver` | Kinematics, dynamics, energy, momentum, projectiles + physics solver |
| `bioinformatics` | DNA/RNA/protein — GC, rev-comp, translate, ORFs, FASTA + sequence toolkit |
| `clinical-evidence` | PICO, evidence appraisal, RR/NNT, GRADE (informational, not medical advice) |
| `medical-literature-review` | PubMed search, PRISMA screening, evidence tables, synthesis |
| `nutrition-analysis` | BMR/TDEE, calorie targets, macros, meal-log analysis + nutrition calculator |
| `newsletter-editor` | Links/notes → issue, subject lines, value-added summaries, CTA |
| `podcast-production` | Episode briefs, guest research, question prep, show notes, chapters, clips |
| `video-scriptwriter` | Hooks, beat structure, narration + visual cues, retention, by format |
| `brand-voice` | Codify a brand voice + audit/rewrite copy to match it consistently |

Some workflow skills are adapted from [obra/superpowers](https://github.com/obra/superpowers).
