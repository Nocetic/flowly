"""Petdex floating-pet support — assets, manifest, sprite analysis, storage.

This package powers the optional desktop "floating pet": a small animated
companion fed by the active bot's display config. It is import-light and has no
side effects at import time; the gateway only touches it when a ``pet.*`` feature
RPC is called.
"""
