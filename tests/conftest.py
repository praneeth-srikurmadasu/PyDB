"""Hypothesis profiles. Select one with the HYPOTHESIS_PROFILE environment variable.

* ``dev`` (default): enough examples to catch regressions quickly on every local run.
* ``ci``: the depth used by continuous integration.
* ``thorough``: a deep check before a release or after a risky change.
"""

import os

from hypothesis import settings

settings.register_profile("dev", max_examples=30)
settings.register_profile("ci", max_examples=100)
settings.register_profile("thorough", max_examples=500)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
