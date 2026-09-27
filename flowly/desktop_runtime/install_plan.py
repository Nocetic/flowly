"""Read-only, identity-bound description of all installation steps.

The fingerprint is a review identifier, not an authorization capability.
Applying it still requires an authenticated display connection, a fresh plan
match and a privileged path: this process runs as root (the job provisions the
unprivileged runtime account) or the host has sudo, whose password the user
supplies per operation over a direct connection only.
"""

import hashlib
import json

from flowly.desktop_runtime.installer import install_plan
from flowly.desktop_runtime.python_installer import python_plan


def installation_plan(identity: dict[str, str]) -> dict:
    target = {key: identity[key] for key in ("hostId", "botId", "profileId", "screenId")}
    system = install_plan()
    python = python_plan()
    # upToDate is a live observation. It must not invalidate a reviewed recipe
    # or make an accepted operation impossible to query after installation.
    fingerprint = {"installPlanVersion": 1, **target,
                   "systemPlanId": system["planId"], "pythonPlanId": python["planId"]}
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    # Live admission, deliberately outside the fingerprint. The reason stays the
    # single documented code so older Desktop parsers keep reading the plan.
    admissible = bool(system["supported"] and python["supported"]
                      and (system["runtimeAccountRequired"] or system["sudoAvailable"]))
    return {"installPlanVersion": 1, **target, "planId": digest,
            "applySupported": admissible, "reason": None if admissible else "INSTALL_ADMISSION_UNAVAILABLE",
            "system": system, "python": python,
            "runtimeAccountRequired": system["runtimeAccountRequired"]}
