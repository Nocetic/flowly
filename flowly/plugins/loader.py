"""Plugin module loader — namespace-isolated import.

Plugins live in arbitrary directories outside the main ``flowly``
package.  We import each plugin's ``__init__.py`` under the synthetic
namespace ``flowly_plugins.<slug>`` so multiple plugins (even ones
with colliding names from different sources) coexist in
``sys.modules``.

The slug derives from the manifest *key*:

* ``disk-cleanup``         → ``flowly_plugins.disk_cleanup``
* ``image_gen/openai``     → ``flowly_plugins.image_gen__openai``

(The category form is reserved for future ``kind: backend`` support;
v1 plugins use the flat form.)
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

logger = logging.getLogger(__name__)

_NS_PARENT = "flowly_plugins"


def _ensure_namespace_parent() -> None:
    """Create the synthetic ``flowly_plugins`` parent package once."""
    if _NS_PARENT in sys.modules:
        return
    pkg = types.ModuleType(_NS_PARENT)
    pkg.__path__ = []  # type: ignore[attr-defined]
    pkg.__package__ = _NS_PARENT
    sys.modules[_NS_PARENT] = pkg


def _slug_for(key: str) -> str:
    """Convert a manifest key to a Python-safe module slug."""
    return key.replace("/", "__").replace("-", "_")


def load_plugin_module(
    plugin_dir: Path, key: str,
) -> types.ModuleType:
    """Import ``plugin_dir/__init__.py`` as ``flowly_plugins.<slug>``.

    Raises:
        FileNotFoundError: if the plugin has no ``__init__.py``.
        ImportError: if the module spec cannot be created.
    """
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"no __init__.py in {plugin_dir}")

    _ensure_namespace_parent()

    slug = _slug_for(key)
    module_name = f"{_NS_PARENT}.{slug}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_file,
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create module spec for {init_file}")

    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
