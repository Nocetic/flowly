"""Read-only, identity-bound description of all installation steps.

The fingerprint is a review identifier, not an authorization capability. No
installation mutation is exposed until authenticated job admission is connected.
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
    return {"installPlanVersion": 1, **target, "planId": digest,
            "applySupported": False, "reason": "INSTALL_ADMISSION_UNAVAILABLE",
            "system": system, "python": python,
            "runtimeAccountRequired": system["runtimeAccountRequired"]}
