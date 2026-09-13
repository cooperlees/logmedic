"""Package wrapper: re-export the sibling plugin module.

The daemon loads the plugin file by path (module stem ``meta_remediator``) while
``plugins/`` is also on ``sys.path`` for ``logmedic_common`` — without this
file the directory would resolve as a namespace package shadowing the real
module. Re-exporting here makes ``import meta_remediator`` return the same names
under the daemon, unittest, and ``ty``.
"""

from meta_remediator.meta_remediator import (  # noqa: F401
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    LATEST_ALIASES,
    RemediatorPlugin,
)
