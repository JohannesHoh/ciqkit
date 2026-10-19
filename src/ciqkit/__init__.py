"""ciqkit -- developer tooling for Garmin Connect IQ watch apps.

Three independent pieces, usable separately:

* :mod:`ciqkit.sim`      -- a broker that owns the single Connect IQ Simulator
                            and schedules preemptible, priority-ordered jobs
                            (screenshots, recordings, navigation, unit tests).
* :mod:`ciqkit.qa`       -- static checks for watch-screen layout, screenshot
                            quality and launcher icons.
* :mod:`ciqkit.leases`   -- cross-process leases for machine-wide singletons.

Not affiliated with, endorsed by, or sponsored by Garmin Ltd. See TRADEMARKS.md.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
