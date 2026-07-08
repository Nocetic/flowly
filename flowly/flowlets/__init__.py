"""Flowlets — agent-generated dynamic mini-screens.

A flowlet is a personal, persistent mini-app the agent builds for the user
(a water tracker, a habit grid, a mood log). It is a declarative JSON
component tree written against a versioned component catalog and rendered
natively on Desktop (React) and iOS (SwiftUI).

Three separated concerns:

* **Definition** — the component tree + state schema + computed values +
  declared actions. Authored by the agent, versioned, rarely changes.
* **State** — the live data (today's water intake). The bot is the single
  source of truth; every change is broadcast to all connected clients.
* **Action** — declared inside the definition and applied by a deterministic
  interpreter on the bot. Tapping a button never calls the LLM.

Public surface:

* :data:`flowly.flowlets.catalog.CATALOG_VERSION`
* :func:`flowly.flowlets.schema.validate_definition`
* :class:`flowly.flowlets.store.FlowletStore` / :func:`get_store`
* :func:`flowly.flowlets.queries.resolve_values`
* :func:`flowly.flowlets.actions.apply_action`
"""

from flowly.flowlets.catalog import CATALOG_VERSION

__all__ = ["CATALOG_VERSION"]
