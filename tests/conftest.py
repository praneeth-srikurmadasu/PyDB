"""Hypothesis profiles. Select one with the HYPOTHESIS_PROFILE environment variable.

* ``dev`` (default): enough examples to catch regressions quickly on every run.
* ``thorough``: ten times as many, for a deep check before a release or after a risky change.
"""

import os

from hypothesis import settings

settings.register_profile("dev", max_examples=50)
settings.register_profile("thorough", max_examples=500)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
