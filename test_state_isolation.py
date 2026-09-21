"""
Tests that test runs cannot write live trading state.

    pytest test_state_isolation.py
    python3 run_test.py test_state_isolation.py

This is the test the 2026-09-21 incident was missing. A test run had left a
synthetic SPY position (entry_price 100.0, broker_order_id "X") in the live
data/stop_prices.json and the equities bot loaded it on the next restart. The
isolation existed for LOGS and had never been extended to state, and nothing
asserted either way — so the gap was invisible until a live bot acted on it.

Asserting the PROPERTY (no guarded path resolves under data/) rather than the
mechanism, so it keeps holding if the mechanism is replaced. Coverage is
enforced from config itself, not from a hand-kept list, because a hand-kept
list of "the state files" is exactly the thing that drifts.
"""

import os
import subprocess
import sys

import config
import run_test

_HERE = os.path.dirname(os.path.abspath(__file__))


def test_state_dir_is_not_live_during_tests():
    """The whole point: a test process must never resolve STATE_DIR to data/."""
    assert config._IS_TEST, "test detection did not fire — everything below is vacuous"
    assert config.STATE_DIR != "data", (
        "STATE_DIR is live during a test run; a _save_*() here writes the "
        "running bots' state")


def test_every_guarded_state_path_is_redirected():
    """No guarded path may sit DIRECTLY in data/ — that is where the live bots
    read and write. data/test/ is fine and is the standalone floor; the test is
    on the containing directory, not on a path prefix, because data/test/ and
    data/ share one."""
    live = os.path.abspath(os.path.join(_HERE, "data"))
    for attr in run_test._GUARDED:
        path = getattr(config, attr)
        assert os.path.dirname(os.path.abspath(path)) != live, \
            f"{attr} resolves to live state: {path}"


def test_wrapper_guard_list_covers_every_state_path():
    """run_test._GUARDED must not drift behind config.

    Derives the truth from config.STATE_FILES, which _state_path() populates by
    construction. Scanning for "*_FILE living in STATE_DIR" does NOT work: under
    run_test.py the logs are redirected into the same tmpdir, so a directory
    comparison cannot tell a state file from a log file.

    Compared by BASENAME, not by current value. conftest layers a per-test
    monkeypatch on top for some of these (each test gets its own tmp_path for
    the A/B tracker), so the live attribute legitimately differs from the path
    config built at import. The invariant being defended is which state files
    EXIST, not where any one test has them pointed right now.
    """
    guarded = {os.path.basename(getattr(config, attr)) for attr in run_test._GUARDED}
    missing = {os.path.basename(p) for p in config.STATE_FILES} - guarded
    assert not missing, (
        f"config state files not guarded by run_test._GUARDED: {sorted(missing)}")


def test_tmpdir_env_is_ignored_outside_a_test_run():
    """The safety property that makes the env var acceptable in production code.

    TB_TEST_TMPDIR must only ever STRENGTHEN an isolation that test detection
    already decided on. If it could create one, a stray export in a production
    shell would boot the bot with its stop file pointing at a tmpdir — no
    stops, and nothing persisted across a reboot.
    """
    env = dict(os.environ, TB_TEST_TMPDIR="/tmp/must-be-ignored")
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import config; print(config.STOP_PRICE_FILE); "
         "print(config.TEST_TMPDIR_IGNORED)"],
        cwd=_HERE, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    path, ignored = proc.stdout.split()
    assert path == "data/stop_prices.json", f"live path hijacked: {path}"
    assert ignored == "True", "the ignored-but-set case must be observable"


def test_wrapper_refuses_to_run_when_the_redirect_does_not_take():
    """Drift between config and the wrapper must stop the run, not warn."""
    try:
        run_test._verify_redirect("/tmp/a-tmpdir-nothing-points-at", "test_stops.py")
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("a failed redirect must raise SystemExit(2)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"All {len(tests)} tests passed.")
