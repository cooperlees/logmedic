"""Package wrapper: re-export the sibling plugin module.

The daemon loads the plugin file by path (module stem ``loki_detector``) while
``plugins/`` is also on ``sys.path`` for ``logmedic_common`` — without this
file the directory would resolve as a namespace package shadowing the real
module. Re-exporting here makes ``import loki_detector`` return the same names
under the daemon, unittest, and ``ty``.
"""

from loki_detector.loki_detector import (  # noqa: F401
    DetectorPlugin,
)
