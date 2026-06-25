"""Security scanner for skills — detects threats in SKILL.md and supporting files.

Six threat categories backed by a trust-based install policy
(builtin / trusted / community / agent-created).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ── Trust policy ──────────────────────────────────────────────────────

INSTALL_POLICY: dict[str, tuple[str, str, str]] = {
    #                  safe      caution    dangerous
    "builtin":       ("allow",  "allow",   "allow"),
    "trusted":       ("allow",  "allow",   "block"),
    "community":     ("allow",  "block",   "block"),
    "agent-created": ("allow",  "allow",   "ask"),
}

VERDICT_INDEX = {"safe": 0, "caution": 1, "dangerous": 2}


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class Finding:
    pattern_id: str
    severity: str      # critical | high | medium | low
    category: str
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    skill_name: str
    source: str
    trust_level: str
    verdict: str       # safe | caution | dangerous
    findings: list[Finding] = field(default_factory=list)
    scanned_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ── Threat patterns ───────────────────────────────────────────────────

_PATTERNS: list[tuple[str, str, str, str, re.Pattern]] = []


def _p(pid: str, sev: str, cat: str, desc: str, pattern: str, flags: int = re.IGNORECASE):
    _PATTERNS.append((pid, sev, cat, desc, re.compile(pattern, flags)))


# Exfiltration
_p("env_exfil_curl", "critical", "exfiltration", "curl/wget with secret vars",
   r'(?:curl|wget)\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)')
_p("ssh_dir_access", "high", "exfiltration", "SSH directory access",
   r'(?:\$HOME|~)/\.ssh')
_p("dump_all_env", "critical", "exfiltration", "Dump all environment variables",
   r'(?:env\b|printenv|set\b)\s*[|>]|os\.environ(?:\.items|\[)')
_p("dns_exfil", "high", "exfiltration", "DNS-based data exfiltration",
   r'nslookup\s+\$|dig\s+\$|host\s+\$')

# Prompt injection
_p("ignore_instructions", "critical", "prompt_injection", "Ignore previous instructions",
   r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+instructions')
_p("role_hijack", "critical", "prompt_injection", "Role hijacking attempt",
   r'you\s+are\s+now\s+(?:a|an|the)\s+\w+')
_p("system_prompt_override", "critical", "prompt_injection", "System prompt override",
   r'(?:system|new)\s+prompt\s*[:\-]')
_p("jailbreak_dan", "critical", "prompt_injection", "DAN jailbreak pattern",
   r'(?:DAN|do\s+anything\s+now|developer\s+mode)')

# Destructive
_p("rm_rf_root", "critical", "destructive", "Recursive root deletion",
   r'rm\s+-[rf]*\s+/')
_p("format_disk", "critical", "destructive", "Disk formatting",
   r'(?:mkfs|format)\s+/dev/')
_p("chmod_777", "high", "destructive", "Overly permissive chmod",
   r'chmod\s+(?:777|a\+rwx)')

# Persistence
_p("crontab_mod", "high", "persistence", "Crontab modification",
   r'crontab\s+-[el]|echo\s+.*>.*crontab')
_p("bashrc_mod", "high", "persistence", "Shell RC modification",
   r'>>?\s*(?:~/)?\.(?:bash|zsh|fish)rc')
_p("ssh_authorized", "critical", "persistence", "SSH authorized_keys modification",
   r'authorized_keys|id_rsa\.pub\s*>>')
_p("launchd", "high", "persistence", "macOS LaunchAgent/Daemon",
   r'LaunchAgents|LaunchDaemons|launchctl')

# Network
_p("reverse_shell", "critical", "network", "Reverse shell pattern",
   r'(?:bash|sh|nc|ncat)\s+-[ie]\s+/dev/tcp|mkfifo\s+/tmp/')
_p("tunnel_service", "high", "network", "Tunnel/proxy service",
   r'(?:ngrok|serveo|localtunnel|cloudflared)\s+(?:http|tcp|tunnel)')

# Obfuscation
_p("base64_exec", "high", "obfuscation", "Base64 decode piped to execution",
   r'base64\s+-[dD]\s*\|')
_p("eval_exec", "high", "obfuscation", "eval/exec on dynamic content",
   r'(?:eval|exec)\s*\(\s*(?:base64|decode|compile|__import__)')
_p("chr_building", "medium", "obfuscation", "Character code building",
   r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr')

# Invisible unicode — tag characters / bidi overrides / zero-width
_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',  # Zero-width
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',  # Bidi override
    '\u2066', '\u2067', '\u2068', '\u2069',             # Bidi isolate
}

# Scannable extensions
_SCANNABLE = {
    '.md', '.txt', '.py', '.sh', '.bash', '.js', '.ts', '.rb',
    '.yaml', '.yml', '.json', '.toml', '.cfg', '.ini',
    '.html', '.css', '.xml', '.tex',
}

_SUSPICIOUS_BINARY = {'.exe', '.dll', '.so', '.dylib', '.bin', '.msi', '.dmg'}

# Structural limits
_MAX_FILE_COUNT = 50
_MAX_TOTAL_SIZE_KB = 1024
_MAX_SINGLE_FILE_KB = 256


# ── Scanner ──────────────────────────────────────────────────────────

def scan_file(file_path: Path, rel_path: str) -> list[Finding]:
    """Scan a single file for threat patterns."""
    findings: list[Finding] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return findings

    # Check invisible unicode
    for i, line in enumerate(content.split("\n"), 1):
        for char in _INVISIBLE_CHARS:
            if char in line:
                findings.append(Finding(
                    "invisible_unicode", "high", "obfuscation",
                    rel_path, i, f"U+{ord(char):04X}", "Invisible unicode character detected"
                ))

    # Check regex patterns
    seen: set[str] = set()
    for pid, sev, cat, desc, regex in _PATTERNS:
        for m in regex.finditer(content):
            line_num = content[:m.start()].count("\n") + 1
            dedup_key = f"{pid}:{line_num}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            match_text = m.group()[:120]
            findings.append(Finding(pid, sev, cat, rel_path, line_num, match_text, desc))

    return findings


def scan_skill(skill_path: Path, source: str = "community") -> ScanResult:
    """Scan entire skill directory for threats."""
    skill_name = skill_path.name
    trust_level = "builtin" if source == "builtin" else ("trusted" if source in {"openai", "anthropic"} else source)

    result = ScanResult(
        skill_name=skill_name,
        source=source,
        trust_level=trust_level,
        verdict="safe",
    )

    if not skill_path.is_dir():
        return result

    # Structural checks
    files = list(skill_path.rglob("*"))
    real_files = [f for f in files if f.is_file()]

    if len(real_files) > _MAX_FILE_COUNT:
        result.findings.append(Finding(
            "too_many_files", "medium", "structural",
            str(skill_path), 0, f"{len(real_files)} files", f"Exceeds {_MAX_FILE_COUNT} file limit"
        ))

    total_kb = sum(f.stat().st_size for f in real_files) / 1024
    if total_kb > _MAX_TOTAL_SIZE_KB:
        result.findings.append(Finding(
            "too_large", "medium", "structural",
            str(skill_path), 0, f"{total_kb:.0f}KB", f"Exceeds {_MAX_TOTAL_SIZE_KB}KB limit"
        ))

    # Scan each file
    for f in real_files:
        if f.suffix.lower() in _SUSPICIOUS_BINARY:
            result.findings.append(Finding(
                "suspicious_binary", "high", "structural",
                str(f.relative_to(skill_path)), 0, f.name, "Suspicious binary file"
            ))
            continue

        if f.suffix.lower() not in _SCANNABLE:
            continue

        if f.stat().st_size > _MAX_SINGLE_FILE_KB * 1024:
            result.findings.append(Finding(
                "oversized_file", "medium", "structural",
                str(f.relative_to(skill_path)), 0, f"{f.stat().st_size // 1024}KB",
                f"File exceeds {_MAX_SINGLE_FILE_KB}KB"
            ))
            continue

        rel = str(f.relative_to(skill_path))
        result.findings.extend(scan_file(f, rel))

    # Determine verdict
    severities = [f.severity for f in result.findings]
    if "critical" in severities:
        result.verdict = "dangerous"
    elif severities.count("high") >= 2:
        result.verdict = "dangerous"
    elif "high" in severities:
        result.verdict = "caution"
    elif "medium" in severities:
        result.verdict = "caution"
    else:
        result.verdict = "safe"

    return result


def should_allow_install(result: ScanResult, force: bool = False) -> tuple[bool | None, str]:
    """Check if skill should be allowed based on trust policy.

    Returns (allowed, reason). None = ask user.
    """
    if force:
        return True, "Force install requested"

    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])
    idx = VERDICT_INDEX.get(result.verdict, 2)
    decision = policy[idx]

    if decision == "allow":
        return True, f"{result.trust_level} source, verdict: {result.verdict}"
    elif decision == "block":
        return False, f"Blocked: {result.trust_level} source with {result.verdict} verdict ({len(result.findings)} findings)"
    else:  # "ask"
        return None, f"Needs review: {len(result.findings)} findings in {result.trust_level} skill"
