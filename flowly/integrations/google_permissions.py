"""Google permissions derived from actual OAuth grants, never a UI toggle."""

PREFIX = "https://www.googleapis.com/auth/"
SERVICE_SCOPES = {
    "gmail": (PREFIX + "gmail.readonly", PREFIX + "gmail.send"),
    "gmail_manage": (PREFIX + "gmail.modify",),
    "calendar": (PREFIX + "calendar.events",),
    "drive": (PREFIX + "drive.readonly", PREFIX + "drive.file"),
    "contacts": (PREFIX + "contacts.readonly",),
    "tasks": (PREFIX + "tasks",),
}


def normalize_services(value=None) -> list[str]:
    if value is None:
        return ["gmail"]
    if (not isinstance(value, list) or not 1 <= len(value) <= len(SERVICE_SCOPES)
            or any(not isinstance(item, str) or item not in SERVICE_SCOPES for item in value)
            or len(set(value)) != len(value) or "gmail" not in value):
        raise ValueError("INVALID_SERVICES")
    return [item for item in SERVICE_SCOPES if item in value]


def granted_services(credentials: dict) -> list[str]:
    raw = credentials.get("scopes", credentials.get("scope", ""))
    scopes = set(raw.split()) if isinstance(raw, str) else set()
    if "https://mail.google.com/" in scopes:
        scopes.add(PREFIX + "gmail.modify")
    if PREFIX + "gmail.modify" in scopes:
        scopes.update(SERVICE_SCOPES["gmail"])
    if PREFIX + "calendar" in scopes:
        scopes.add(PREFIX + "calendar.events")
    if PREFIX + "drive" in scopes:
        scopes.update(SERVICE_SCOPES["drive"])
    if PREFIX + "contacts" in scopes:
        scopes.add(PREFIX + "contacts.readonly")
    allowed = credentials.get("services", list(SERVICE_SCOPES))
    return [service for service, required in SERVICE_SCOPES.items()
            if service in allowed and set(required) <= scopes]
