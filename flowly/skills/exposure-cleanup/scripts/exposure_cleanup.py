#!/usr/bin/env python3
"""Consent-gated personal exposure cleanup helper for Flowly.

This CLI is intentionally local-only: no network calls, no SMTP/IMAP, no browser
launching, and no credential discovery. The agent uses Flowly tools to inspect
sites and drive forms; this script owns deterministic state, planning, drafts,
and the audit ledger.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any


STATES = [
    "new",
    "searching",
    "not_found",
    "found",
    "indirect_exposure",
    "action_selected",
    "submitted",
    "verification_pending",
    "awaiting_processing",
    "confirmed_removed",
    "reappeared",
    "human_task_queued",
    "blocked",
]

TRANSITIONS: dict[str, set[str]] = {
    "new": {"searching", "found", "not_found", "indirect_exposure", "blocked"},
    "searching": {"not_found", "found", "indirect_exposure", "blocked"},
    "not_found": {"searching", "found", "indirect_exposure", "blocked"},
    "found": {"action_selected", "submitted", "human_task_queued", "indirect_exposure", "blocked", "not_found"},
    "indirect_exposure": {"submitted", "human_task_queued", "not_found", "found", "blocked"},
    "action_selected": {"submitted", "human_task_queued", "blocked"},
    "submitted": {"verification_pending", "awaiting_processing", "human_task_queued", "blocked"},
    "verification_pending": {"awaiting_processing", "confirmed_removed", "human_task_queued", "blocked"},
    "awaiting_processing": {"confirmed_removed", "human_task_queued", "blocked"},
    "confirmed_removed": {"reappeared", "confirmed_removed"},
    "reappeared": {"found", "indirect_exposure"},
    "human_task_queued": {
        "found",
        "indirect_exposure",
        "action_selected",
        "submitted",
        "verification_pending",
        "awaiting_processing",
        "confirmed_removed",
        "blocked",
    },
    "blocked": {"searching", "found", "not_found", "indirect_exposure", "action_selected", "human_task_queued"},
}

NEVER_DISCLOSE = {"ssn", "social_security_number", "passport", "drivers_license", "driver_license"}
PRIORITY_ORDER = {"crucial": 0, "high": 1, "standard": 2, "long_tail": 3}
HUMAN_STATES = {"human_task_queued", "blocked"}
IN_FLIGHT = {"submitted", "verification_pending", "awaiting_processing"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("EXPOSURE_CLEANUP_DIR")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("FLOWLY_HOME") or (Path.home() / ".flowly"))
    return home / "exposure-cleanup"


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def atomic_write_json(path: Path, obj: Any) -> Path:
    secure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, record: dict[str, Any]) -> Path:
    secure_dir(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def subjects_dir() -> Path:
    return data_dir() / "subjects"


def subject_dir(subject_id: str) -> Path:
    return subjects_dir() / subject_id


def dossier_path(subject_id: str) -> Path:
    return subject_dir(subject_id) / "dossier.json"


def ledger_path(subject_id: str) -> Path:
    return subject_dir(subject_id) / "ledger.json"


def audit_path(subject_id: str) -> Path:
    return subject_dir(subject_id) / "audit.jsonl"


def drafts_dir(subject_id: str) -> Path:
    return subject_dir(subject_id) / "drafts"


def evidence_dir(subject_id: str) -> Path:
    return subject_dir(subject_id) / "evidence"


def catalog_path() -> Path:
    return skill_root() / "references" / "brokers.json"


def load_brokers() -> list[dict[str, Any]]:
    brokers = read_json(catalog_path(), [])
    if not isinstance(brokers, list):
        raise ValueError("references/brokers.json must contain a list")
    brokers.sort(key=lambda b: (PRIORITY_ORDER.get(str(b.get("priority", "standard")), 9), str(b.get("id", ""))))
    return brokers


def broker_by_id(broker_id: str) -> dict[str, Any] | None:
    for broker in load_brokers():
        if broker.get("id") == broker_id:
            return broker
    return None


def new_subject_id() -> str:
    return "sub_" + hashlib.sha1(secrets.token_bytes(16)).hexdigest()[:12]


def parse_prior_location(values: list[str] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in values or []:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            continue
        entry: dict[str, str] = {"city": parts[0]}
        if len(parts) > 1:
            entry["state"] = parts[1]
        if len(parts) > 2:
            entry["postal"] = parts[2]
        out.append(entry)
    return out


def create_dossier(args: argparse.Namespace) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "full_name": args.full_name,
        "also_known_as": args.alias or [],
        "emails": args.email or [],
        "phones": args.phone or [],
    }
    current = {k: v for k, v in {
        "line1": args.street,
        "city": args.city,
        "state": args.state,
        "postal": args.postal,
    }.items() if v}
    if current:
        identity["current_address"] = current
    priors = parse_prior_location(args.prior_location)
    if priors:
        identity["prior_addresses"] = priors
    if args.date_of_birth:
        identity["date_of_birth"] = args.date_of_birth

    dossier = {
        "subject_id": new_subject_id(),
        "identity": identity,
        "consent": {
            "authorized": bool(args.consent),
            "method": args.consent_method,
            "recorded_at": now() if args.consent else None,
            "scope": "find and reduce this subject's exposure on data broker and people-search sites",
        },
        "residency_jurisdiction": args.residency,
        "preferences": {
            "contact_email_for_optouts": args.contact_email or (identity["emails"][0] if identity["emails"] else None),
            "rescan_interval_days": args.rescan_interval_days,
            "mode": args.mode,
        },
        "created_at": now(),
    }
    atomic_write_json(dossier_path(dossier["subject_id"]), dossier)
    return dossier


def load_dossier(subject_id: str) -> dict[str, Any]:
    dossier = read_json(dossier_path(subject_id), None)
    if not dossier:
        raise FileNotFoundError(f"unknown subject {subject_id!r}")
    return dossier


def is_authorized(dossier: dict[str, Any]) -> bool:
    consent = dossier.get("consent") or {}
    return bool(consent.get("authorized")) and consent.get("method") in {"self", "written_authorization", "poa"}


def require_authorized(dossier: dict[str, Any]) -> None:
    if not is_authorized(dossier):
        raise PermissionError("no recorded consent for this subject; refusing to plan, submit, or record actions")


def load_ledger(subject_id: str) -> dict[str, Any]:
    return read_json(ledger_path(subject_id), {})


def save_ledger(subject_id: str, ledger: dict[str, Any]) -> Path:
    return atomic_write_json(ledger_path(subject_id), ledger)


def new_case(subject_id: str, broker_id: str) -> dict[str, Any]:
    return {
        "case_id": f"case_{subject_id}_{broker_id}",
        "subject_id": subject_id,
        "broker_id": broker_id,
        "state": "new",
        "found": None,
        "evidence": {},
        "disclosure_log": [],
        "history": [],
    }


def transition(subject_id: str, broker_id: str, state: str, fields: dict[str, Any]) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    ledger = load_ledger(subject_id)
    case = ledger.get(broker_id) or new_case(subject_id, broker_id)
    old = case.get("state", "new")
    if state != old and state not in TRANSITIONS.get(old, set()):
        raise ValueError(f"illegal transition {old!r} -> {state!r}")
    case["state"] = state
    case.update(fields)
    stamp = now()
    case.setdefault("history", []).append({"at": stamp, "from": old, "to": state})
    ledger[broker_id] = case
    save_ledger(subject_id, ledger)
    append_jsonl(audit_path(subject_id), {"at": stamp, "event": "transition", "broker_id": broker_id, "from": old, "to": state})
    return case


def all_names(dossier: dict[str, Any]) -> list[str]:
    ident = dossier.get("identity") or {}
    seen: set[str] = set()
    out: list[str] = []
    for name in [ident.get("full_name"), *(ident.get("also_known_as") or [])]:
        key = str(name or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(str(name).strip())
    return out


def all_addresses(dossier: dict[str, Any]) -> list[dict[str, Any]]:
    ident = dossier.get("identity") or {}
    out: list[dict[str, Any]] = []
    if ident.get("current_address"):
        out.append({**ident["current_address"], "kind": "current"})
    for item in ident.get("prior_addresses") or []:
        out.append({**item, "kind": item.get("kind", "prior")})
    return out


def all_locations(dossier: dict[str, Any]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for address in all_addresses(dossier):
        city = str(address.get("city") or "").strip()
        state = str(address.get("state") or "").strip()
        key = (city.lower(), state.lower())
        if city and key not in seen:
            seen.add(key)
            out.append({"city": city, "state": state})
    return out


def contact_email(dossier: dict[str, Any]) -> str | None:
    prefs = dossier.get("preferences") or {}
    ident = dossier.get("identity") or {}
    return prefs.get("contact_email_for_optouts") or next(iter(ident.get("emails") or []), None)


def search_vectors(dossier: dict[str, Any], broker: dict[str, Any]) -> list[dict[str, Any]]:
    ident = dossier.get("identity") or {}
    by = set(broker.get("search_by") or ["name"])
    vectors: list[dict[str, Any]] = []
    if "name" in by:
        locations = all_locations(dossier)
        if locations:
            for name in all_names(dossier):
                for loc in locations:
                    vectors.append({"by": "name", "query": {"full_name": name, **loc}})
        else:
            for name in all_names(dossier):
                vectors.append({"by": "name", "query": {"full_name": name}})
    if "phone" in by:
        for phone in ident.get("phones") or []:
            vectors.append({"by": "phone", "query": {"phone": phone}})
    if "email" in by:
        for email in ident.get("emails") or []:
            vectors.append({"by": "email", "query": {"email": email}})
    if "address" in by:
        for address in all_addresses(dossier):
            if address.get("line1"):
                vectors.append({"by": "address", "query": {k: address.get(k) for k in ("line1", "city", "state", "postal") if address.get(k)}})
    return vectors


def select_disclosure(dossier: dict[str, Any], broker: dict[str, Any], listing_urls: list[str] | None = None) -> dict[str, Any]:
    ident = dossier.get("identity") or {}
    current = ident.get("current_address") or {}
    phones = ident.get("phones") or []
    available = {
        "full_name": ident.get("full_name"),
        "contact_email": contact_email(dossier),
        "phone": phones[0] if phones else None,
        "date_of_birth": ident.get("date_of_birth"),
        "street": current.get("line1"),
        "city": current.get("city"),
        "state": current.get("state"),
        "postal": current.get("postal"),
        "current_address": current or None,
        "profile_url": listing_urls[0] if listing_urls else None,
        "listing_urls": listing_urls or None,
    }
    requested = list((broker.get("optout") or {}).get("inputs") or ["full_name", "contact_email"])
    out: dict[str, Any] = {}
    for key in requested:
        if key in NEVER_DISCLOSE:
            continue
        value = available.get(key)
        if value:
            out[key] = value
    if listing_urls:
        out["listing_urls"] = listing_urls
    return out


def honest_kind(dossier: dict[str, Any], requested: str) -> str:
    if requested != "auto":
        if requested == "ccpa" and not str(dossier.get("residency_jurisdiction", "")).upper().startswith("US-CA"):
            raise ValueError("ccpa kind requires residency_jurisdiction starting with US-CA; use generic otherwise")
        if requested == "gdpr" and not str(dossier.get("residency_jurisdiction", "")).upper().startswith(("EU", "UK", "GB")):
            raise ValueError("gdpr kind requires EU/UK/GB residency; use generic otherwise")
        return requested
    residency = str(dossier.get("residency_jurisdiction") or "").upper()
    if residency.startswith("US-CA"):
        return "ccpa"
    if residency.startswith(("EU", "UK", "GB")):
        return "gdpr"
    return "generic"


def render_draft(kind: str, broker: dict[str, Any], fields: dict[str, Any]) -> str:
    broker_name = broker.get("name", "the data broker")
    name = fields.get("full_name", "[full name]")
    email = fields.get("contact_email", "[contact email]")
    listings = fields.get("listing_urls") or []
    listing_block = "\n".join(f"- {url}" for url in listings) if listings else "- [listing URL or description]"

    if kind == "ccpa":
        subject = f"Subject: CCPA deletion and opt-out request - {name}"
        body = [
            subject,
            "",
            f"Hello {broker_name} privacy team,",
            "",
            "I am requesting deletion of my personal information and opt-out of sale or sharing under the CCPA/CPRA.",
            "",
            f"Name: {name}",
            f"Contact email: {email}",
            "Relevant listing(s):",
            listing_block,
            "",
            "Please confirm completion or tell me the minimum additional information required to verify this request.",
            "",
            "Do not ask for or collect SSN, full government ID numbers, or unrelated third-party information for this request.",
        ]
    elif kind == "gdpr":
        subject = f"Subject: GDPR erasure request - {name}"
        body = [
            subject,
            "",
            f"Hello {broker_name} privacy team,",
            "",
            "I am requesting erasure of my personal data under GDPR/UK-GDPR Article 17.",
            "",
            f"Name: {name}",
            f"Contact email: {email}",
            "Relevant listing(s):",
            listing_block,
            "",
            "Please confirm completion or specify the minimum information required to verify this request.",
            "",
            "Do not request SSN, full government ID numbers, or unrelated third-party information.",
        ]
    elif kind == "indirect":
        identifiers = fields.get("my_identifiers") or [name, email]
        ids = "\n".join(f"- {item}" for item in identifiers if item)
        subject = f"Subject: Remove my personal information from an associated record - {name}"
        body = [
            subject,
            "",
            f"Hello {broker_name} privacy team,",
            "",
            "My personal information appears on a record that is not my own. I am asking you to remove only my personal information from that associated record.",
            "",
            f"Name: {name}",
            f"Contact email: {email}",
            "My identifiers to remove:",
            ids or "- [identifier]",
            "Relevant listing(s):",
            listing_block,
            "",
            "Please do not require me to submit information about the third party whose record contains my identifiers.",
        ]
    else:
        subject = f"Subject: Personal information opt-out request - {name}"
        body = [
            subject,
            "",
            f"Hello {broker_name} privacy team,",
            "",
            "Please remove or suppress my personal information from your people-search and data-broker listings.",
            "",
            f"Name: {name}",
            f"Contact email: {email}",
            "Relevant listing(s):",
            listing_block,
            "",
            "Please confirm when the listing is removed and tell me the minimum additional information required if you cannot process this request as written.",
            "",
            "Do not ask for SSN, full government ID numbers, or unrelated third-party information.",
        ]
    return "\n".join(body) + "\n"


def followup_fields(state: str, broker: dict[str, Any], dossier: dict[str, Any]) -> dict[str, Any]:
    if state in {"submitted", "awaiting_processing"}:
        days = int((broker.get("optout") or {}).get("est_processing_days") or 14)
        return {"next_recheck_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if state == "verification_pending":
        return {"next_recheck_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if state == "confirmed_removed":
        days = int((dossier.get("preferences") or {}).get("rescan_interval_days") or 120)
        return {
            "removal_confirmed_at": now(),
            "next_recheck_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    return {}


def cmd_doctor(_args: argparse.Namespace) -> None:
    brokers = load_brokers()
    secure_dir(data_dir())
    writable = os.access(data_dir(), os.W_OK)
    print(json.dumps({
        "data_dir": str(data_dir()),
        "writable": writable,
        "broker_catalog": str(catalog_path()),
        "brokers": len(brokers),
        "network": "none; this helper is local-only",
    }, indent=2))


def cmd_create_subject(args: argparse.Namespace) -> None:
    dossier = create_dossier(args)
    print(json.dumps({
        "subject_id": dossier["subject_id"],
        "authorized": is_authorized(dossier),
        "dossier_path": str(dossier_path(dossier["subject_id"])),
        "next": f"python3 scripts/exposure_cleanup.py plan {dossier['subject_id']}",
    }, indent=2))


def cmd_brokers(args: argparse.Namespace) -> None:
    brokers = load_brokers()
    if args.priority:
        wanted = set(args.priority)
        brokers = [b for b in brokers if b.get("priority") in wanted]
    rows = []
    for b in brokers:
        opt = b.get("optout") or {}
        rows.append({
            "id": b.get("id"),
            "name": b.get("name"),
            "priority": b.get("priority"),
            "category": b.get("category"),
            "search_by": b.get("search_by") or ["name"],
            "optout_method": opt.get("method"),
            "optout_url": opt.get("url"),
            "email": opt.get("email"),
            "covers": b.get("covers") or [],
            "verify_live_before_use": True,
        })
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def cmd_plan(args: argparse.Namespace) -> None:
    dossier = load_dossier(args.subject)
    require_authorized(dossier)
    ledger = load_ledger(args.subject)
    brokers = load_brokers()
    if args.priority:
        wanted = set(args.priority)
        brokers = [b for b in brokers if b.get("priority") in wanted]
    child_to_parent: dict[str, str] = {}
    for b in brokers:
        for child in b.get("covers") or []:
            child_to_parent[child] = b["id"]

    items = []
    for broker in brokers:
        broker_id = broker["id"]
        parent = child_to_parent.get(broker_id)
        covered_by = parent if parent and (ledger.get(parent) or {}).get("state") in {"submitted", "awaiting_processing", "confirmed_removed"} else None
        opt = broker.get("optout") or {}
        case = ledger.get(broker_id) or new_case(args.subject, broker_id)
        items.append({
            "broker_id": broker_id,
            "broker_name": broker.get("name"),
            "state": case.get("state"),
            "priority": broker.get("priority"),
            "covered_by_parent": covered_by,
            "search_by": broker.get("search_by") or ["name"],
            "search_url": broker.get("search_url"),
            "search_vectors": [] if covered_by else search_vectors(dossier, broker),
            "optout": {
                "method": opt.get("method"),
                "url": opt.get("url"),
                "email": opt.get("email"),
                "inputs": [x for x in opt.get("inputs", []) if x not in NEVER_DISCLOSE],
                "human_gates": opt.get("human_gates") or [],
                "est_processing_days": opt.get("est_processing_days"),
            },
            "notes": broker.get("notes") or [],
        })

    counts: dict[str, int] = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    print(json.dumps({
        "subject": args.subject,
        "mode": (dossier.get("preferences") or {}).get("mode", "assisted"),
        "counts": counts,
        "next_order": [
            "scan all search_vectors read-only",
            "record found/not_found/indirect_exposure/blocked with evidence",
            "draft requests only after confirmed matches",
            "submit only with explicit operator confirmation",
            "re-scan before confirmed_removed",
        ],
        "brokers": items,
    }, indent=2, ensure_ascii=False))


def cmd_record(args: argparse.Namespace) -> None:
    dossier = load_dossier(args.subject)
    require_authorized(dossier)
    broker = broker_by_id(args.broker)
    if not broker:
        raise FileNotFoundError(f"unknown broker {args.broker!r}")
    fields = followup_fields(args.state, broker, dossier)
    if args.found is not None:
        fields["found"] = args.found
    if args.evidence_json:
        fields["evidence"] = json.loads(args.evidence_json)
    if args.reason:
        fields["human_task_reason"] = args.reason
    if args.disclosed:
        existing = (load_ledger(args.subject).get(args.broker) or {}).get("disclosure_log") or []
        disclosure_record = {
            "at": now(),
            "fields": sorted(set(args.disclosed)),
            "channel": args.channel or "unknown",
        }
        fields["disclosure_log"] = [*existing, disclosure_record]
        append_jsonl(audit_path(args.subject), {
            "at": now(),
            "event": "disclosure",
            "broker_id": args.broker,
            "fields": disclosure_record["fields"],
            "channel": disclosure_record["channel"],
        })
    case = transition(args.subject, args.broker, args.state, fields)
    print(json.dumps({
        "broker": args.broker,
        "state": case["state"],
        "next_recheck_at": case.get("next_recheck_at"),
        "case": case,
    }, indent=2, ensure_ascii=False))


def cmd_draft(args: argparse.Namespace) -> None:
    dossier = load_dossier(args.subject)
    require_authorized(dossier)
    broker = broker_by_id(args.broker)
    if not broker:
        raise FileNotFoundError(f"unknown broker {args.broker!r}")
    kind = honest_kind(dossier, args.kind)
    listings = args.listing or []
    if kind != "indirect" and not listings and not args.allow_no_listing:
        raise ValueError("--listing is required unless --allow-no-listing is set; verify before disclosing")
    fields = select_disclosure(dossier, broker, listings)
    full_name = (dossier.get("identity") or {}).get("full_name")
    if full_name:
        fields.setdefault("full_name", full_name)
    if kind == "indirect":
        fields["my_identifiers"] = args.identifier or [x for x in [fields.get("full_name"), fields.get("contact_email")] if x]
    draft = render_draft(kind, broker, fields)
    disclosed = sorted(k for k, v in fields.items() if v and k not in {"listing_urls"})
    if args.print:
        print(draft)
        return
    secure_dir(drafts_dir(args.subject))
    out = drafts_dir(args.subject) / f"{args.broker}-{kind}.txt"
    out.write_text(draft, encoding="utf-8")
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({
        "draft_path": str(out),
        "kind": kind,
        "to": (broker.get("optout") or {}).get("email"),
        "disclosed_fields_if_sent": disclosed,
        "next": f"after explicit confirmation and sending, run: python3 scripts/exposure_cleanup.py record {args.subject} {args.broker} submitted --disclosed {' --disclosed '.join(disclosed)} --channel email",
    }, indent=2, ensure_ascii=False))


def cmd_show(args: argparse.Namespace) -> None:
    load_dossier(args.subject)
    case = (load_ledger(args.subject).get(args.broker) or new_case(args.subject, args.broker))
    print(json.dumps(case, indent=2, ensure_ascii=False))


def cmd_status(args: argparse.Namespace) -> None:
    load_dossier(args.subject)
    ledger = load_ledger(args.subject)
    counts = {state: 0 for state in STATES}
    for case in ledger.values():
        counts[case.get("state", "new")] = counts.get(case.get("state", "new"), 0) + 1
    counts = {k: v for k, v in counts.items() if v}
    due = []
    stamp = now()
    for case in ledger.values():
        if case.get("next_recheck_at") and case["next_recheck_at"] <= stamp:
            due.append({"broker_id": case.get("broker_id"), "state": case.get("state"), "next_recheck_at": case.get("next_recheck_at")})
    print(json.dumps({
        "subject": args.subject,
        "counts": counts,
        "confirmed_removed": counts.get("confirmed_removed", 0),
        "in_flight": sum(counts.get(s, 0) for s in IN_FLIGHT),
        "human_tasks": sum(counts.get(s, 0) for s in HUMAN_STATES),
        "due_rechecks": due,
    }, indent=2, ensure_ascii=False))


def cmd_tasks(args: argparse.Namespace) -> None:
    load_dossier(args.subject)
    ledger = load_ledger(args.subject)
    brokers = {b["id"]: b for b in load_brokers()}
    tasks = []
    for broker_id, case in sorted(ledger.items()):
        if case.get("state") not in HUMAN_STATES:
            continue
        broker = brokers.get(broker_id, {})
        tasks.append({
            "broker_id": broker_id,
            "broker_name": broker.get("name", broker_id),
            "state": case.get("state"),
            "why": case.get("human_task_reason") or ("site blocks automation" if case.get("state") == "blocked" else "manual step required"),
            "where": (broker.get("optout") or {}).get("url") or (broker.get("optout") or {}).get("email") or broker.get("search_url"),
            "withhold": ["SSN", "full driver license number", "full passport number", "unrelated third-party data"],
        })
    print(json.dumps({"subject": args.subject, "tasks": tasks}, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exposure-cleanup", description="Flowly local helper for data broker cleanup")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("create-subject")
    s.add_argument("--full-name", required=True)
    s.add_argument("--alias", action="append")
    s.add_argument("--email", action="append")
    s.add_argument("--phone", action="append")
    s.add_argument("--street")
    s.add_argument("--city")
    s.add_argument("--state")
    s.add_argument("--postal")
    s.add_argument("--prior-location", action="append", help="City,ST or City,ST,ZIP; repeatable")
    s.add_argument("--date-of-birth", help="YYYY-MM-DD; collect only if required by a broker")
    s.add_argument("--contact-email")
    s.add_argument("--residency", default="US", help="US, US-CA, EU, UK, etc.")
    s.add_argument("--rescan-interval-days", type=int, default=120)
    s.add_argument("--mode", choices=["assisted", "draft-only"], default="assisted")
    s.add_argument("--consent", action="store_true", help="subject authorized this cleanup")
    s.add_argument("--consent-method", choices=["self", "written_authorization", "poa"], default="self")
    s.set_defaults(func=cmd_create_subject)

    s = sub.add_parser("brokers")
    s.add_argument("--priority", action="append", choices=sorted(PRIORITY_ORDER))
    s.set_defaults(func=cmd_brokers)

    s = sub.add_parser("plan")
    s.add_argument("subject")
    s.add_argument("--priority", action="append", choices=sorted(PRIORITY_ORDER))
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("record")
    s.add_argument("subject")
    s.add_argument("broker")
    s.add_argument("state", choices=STATES)
    s.add_argument("--found", type=lambda v: str(v).lower() in {"1", "true", "yes", "y"})
    s.add_argument("--evidence-json")
    s.add_argument("--disclosed", action="append")
    s.add_argument("--channel")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("draft")
    s.add_argument("subject")
    s.add_argument("broker")
    s.add_argument("--kind", choices=["auto", "generic", "ccpa", "gdpr", "indirect"], default="auto")
    s.add_argument("--listing", action="append")
    s.add_argument("--identifier", action="append", help="for indirect exposure requests")
    s.add_argument("--allow-no-listing", action="store_true")
    s.add_argument("--print", action="store_true")
    s.set_defaults(func=cmd_draft)

    s = sub.add_parser("show")
    s.add_argument("subject")
    s.add_argument("broker")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("status")
    s.add_argument("subject")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("tasks")
    s.add_argument("subject")
    s.set_defaults(func=cmd_tasks)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
