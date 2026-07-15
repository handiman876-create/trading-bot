"""
Test-only guard against clobbering live bot state. NOT collected by pytest (the
leading underscore keeps it out of the `test_*.py` glob).

WHY THIS EXISTS: test setups wipe the bot's JSON state files to get a clean
slate. Those paths are module globals on `strategy` (_STOPS_PATH,
_MOM_ENTRIES_PATH) pointing at the REAL data/ files by default, and are only
redirected to a tmpdir by two separate mechanisms:

  * conftest.py's autouse fixture  -> covers `pytest`
  * each module's `__main__` block -> covers `python3 test_x.py`

Miss either one and the test deletes live state. test_exit_state.py shipped
without the `__main__` redirect, so running it directly wiped the live
data/stop_prices.json — destroying the ratcheting trailing stops on every open
position. That happened twice (2026-07-15 being the second time).

The redirects are the fix; this is the seatbelt. Route every destructive test
operation through here and a missing redirect fails LOUDLY at the call site
instead of silently eating live money-protecting state.
"""

import os
import tempfile


def assert_disposable(path) -> str:
    """Return the real path, or raise if it is not inside the system temp dir.

    Both sanctioned redirect mechanisms land under tempfile.gettempdir():
    pytest's `tmp_path` (/tmp/pytest-of-*/...) and `tempfile.mkdtemp()`. Anything
    else — above all the live data/ files — is refused.

    Checks the RESOLVED path (realpath) so a symlink or a `..` segment can't
    smuggle a live path past the guard. A substring test like `"tmp" in path`
    would be both leakier and jumpier: it passes any live path that happens to
    contain the letters (e.g. /root/la-test-bot/, /var/tmp-backups/live/) and
    fails a legitimate tmpdir that doesn't.
    """
    real = os.path.realpath(str(path))
    tmproot = os.path.realpath(tempfile.gettempdir())
    if real != tmproot and not real.startswith(tmproot + os.sep):
        raise AssertionError(
            f"SAFETY: refusing to touch {real!r} — it is outside the temp dir "
            f"({tmproot!r}), so it is presumed to be LIVE bot state.\n"
            f"The calling test did not redirect this path. Fix the test, not "
            f"this guard: set strategy._STOPS_PATH / _MOM_ENTRIES_PATH to a "
            f"tempfile.mkdtemp() location in its `__main__` block (see "
            f"test_stops.py), and rely on conftest.py under pytest."
        )
    return real


def safe_remove(path) -> None:
    """os.remove(path), but only if `path` is a disposable temp file.

    Raises AssertionError on a live path — deliberately louder than a silent
    no-op, since a test reaching here with a live path is a real bug that has
    already cost us the stop file twice.
    """
    real = assert_disposable(path)
    if os.path.exists(real):
        os.remove(real)
