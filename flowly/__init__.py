"""
flowly - A lightweight AI agent framework
"""

from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("flowly-ai")
except Exception:
    __version__ = "0.0.0-dev"

__logo__ = "✦"
__banner__ = """\
        ,((((,
       (((  ((\\
      ((     ((\\
     ((    ~   (\\     ,~.    ~.
     (   ~     ~\\  .~ , ~. ~  ~.
      ( ~   ~.   \\~  ~  , ~  ~  ~.       flowly v{version}
       ~  ~.  ~.  ~ ~. ~  ~  ~  ~ ~      AI agent framework
     ~ ~ ~  ~  ~ ~ ~  ~ ~  ~ ~ ~  ~ ~
"""
