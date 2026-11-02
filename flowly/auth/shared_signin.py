"""Subscription sign-ins are the computer's (provider-switching.md).

A sign-in (ChatGPT, Grok) is made once and every agent on the computer runs
on it: a named agent reads it from the main agent's home, never from its own.
Copying it was not an option: the issuer rotates the refresh token on every
refresh, so two copies log each other out.

One store read by several processes (the gateway and each named agent's
runtime) needs two guards:

* refreshing is serialised with an inter-process lock, and whoever takes the
  lock reads the store again first: another agent may have refreshed it
  already, and its new token is the one to use;
* a refresh token the issuer rejects only clears the store when the store
  still holds that token: a different one means another agent refreshed in
  between, and its sign-in is kept.

A home that is not a named agent's (another product built on this codebase,
with a ``FLOWLY_HOME`` of its own) keeps its own store, as before.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flowly import profile as profiles


def signin_home() -> Path:
    """The home whose sign-ins this process uses: the main agent's for a
    named agent, else its own."""
    if profiles.current_profile_name() != "default":
        return profiles._DEFAULT_HOME
    return profiles.get_flowly_home()


def signin_scope_suffix() -> str:
    """The keychain service suffix of :func:`signin_home` (the same rule as
    :func:`flowly.profile.credential_scope_suffix`): none for the default home,
    so its existing entries keep resolving."""
    home = signin_home()
    if home == profiles._DEFAULT_HOME:
        return ""
    return hashlib.sha256(str(home).encode()).hexdigest()[:12]


@contextmanager
def refresh_lock(name: str) -> Iterator[None]:
    """Hold the computer-wide lock for refreshing sign-in ``name``.

    Blocking: the holder is a refresh of a few seconds at most. Where the
    platform has no advisory locks (Windows) the body runs unguarded, as before."""
    path = signin_home() / "credentials" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
