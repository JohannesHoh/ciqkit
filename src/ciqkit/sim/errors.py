"""The error every SimSession operation, and the seeding it delegates to, raises.

Its own module so :mod:`ciqkit.sim.seed` can raise it without importing
:mod:`ciqkit.sim.session`, which imports seed. ``session.SimError`` re-exports it.
"""


class SimError(RuntimeError):
    pass
