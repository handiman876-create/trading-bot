"""
Standing guard against time-bomb dates in the test suite.

WHY THIS EXISTS: on 2026-08-17 test_option_positions::test_legacy_position_is_adopted
started failing with what looked like broken option adoption. Adoption was fine —
the captured log even said "OPTION POSITION ADOPTED ... (adoptions #1)". The
module had `_EXP = "2026-08-21"` hardcoded, and the calendar had walked inside
OPTION_MIN_DAYS_TO_EXPIRY, so the expiry rule closed the contract in the same
call and _close_option_position cleared the store the assertion reads. The test
had passed for weeks and broke on a day nobody touched the code.

That is the whole failure mode: a test whose result depends on today's date, with
no seam to control it. Two properties make a date safe, and a fixture needs one:

  INJECTED   — the reference point is a parameter or a monkeypatched hook, so the
               absolute dates around it never move relative to it. This is what
               test_futures_calendar (_dt(...) as "now"), test_screen_ab
               (_today_et), test_sentiment (now=...) and
               test_performance_analyzer (an explicit `cutoff`) all do, and why
               their ~90 absolute date literals are not bombs.
  RELATIVE    — the date is computed from date.today(), so its DISTANCE from
               today is fixed. Required when the production helper has no seam.

strategy._trading_days_to_expiry(expiration) is the case with no seam: it reads
today internally and takes no reference date, unlike
performance_analyzer._reference_now(), which exists specifically so tests can pin
it. So option expirations in tests MUST be relative. These tests pin that.

Note the asymmetry in which direction is dangerous. A past date meant to be STALE
is safe — it only gets staler, and staleness is what the test asserts. A date
meant to be FRESH, or an expiration meant to be FAR, is a bomb: the calendar
walks toward it.
"""

import ast
import glob
import os
import re

import config
import strategy

_HERE = os.path.dirname(os.path.abspath(__file__))
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _test_modules():
    return sorted(glob.glob(os.path.join(_HERE, "test_*.py")))


def test_no_absolute_expiration_constants():
    """No test module may assign a bare ISO date to an expiration-ish name.

    Catches the exact shape that broke: `_EXP = "2026-08-21"`. A relative value
    (date.today() + timedelta(...)) is an ast.BinOp, not an ast.Constant, so it
    passes untouched.
    """
    offenders = []
    for path in _test_modules():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:                      # not our problem to police here
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any("EXP" in n.upper() for n in names):
                continue
            if isinstance(node.value, ast.Constant) and \
                    isinstance(node.value.value, str) and \
                    _ISO.match(node.value.value):
                offenders.append(
                    f"{os.path.basename(path)}:{node.lineno} "
                    f"{names[0]} = {node.value.value!r}")

    assert not offenders, (
        "absolute expiration date(s) in the test suite — these expire as the "
        "calendar advances and fail on a day nobody touched the code. Use "
        "date.today() + timedelta(days=N):\n  " + "\n  ".join(offenders))


def test_option_test_expirations_are_outside_the_expiry_window():
    """The property that actually broke, checked with the production helper.

    Any module-level _EXP must sit far enough out that the expiry rule cannot
    fire on it — otherwise every test in that module silently exercises a forced
    close instead of whatever it meant to test.
    """
    import importlib

    checked = 0
    for path in _test_modules():
        name = os.path.splitext(os.path.basename(path))[0]
        if name == "test_time_bomb_dates":
            continue
        try:
            mod = importlib.import_module(name)
        except Exception:                        # import side effects are not ours
            continue
        exp = getattr(mod, "_EXP", None)
        if not (isinstance(exp, str) and _ISO.match(exp)):
            continue
        checked += 1
        days = strategy._trading_days_to_expiry(exp)
        assert days is not None, f"{name}._EXP = {exp!r} is unparseable"
        assert days > config.OPTION_MIN_DAYS_TO_EXPIRY, (
            f"{name}._EXP = {exp!r} is {days} trading sessions out, at or inside "
            f"OPTION_MIN_DAYS_TO_EXPIRY ({config.OPTION_MIN_DAYS_TO_EXPIRY}). "
            f"The expiry rule will close every contract in that module on sight.")

    assert checked >= 1, (
        "no module-level _EXP found to check — if the option tests were renamed, "
        "point this guard at the new constant rather than letting it pass vacuously")


def test_relative_dates_stay_relative_across_a_year():
    """Sanity: the replacement idiom is genuinely date-independent.

    Not a tautology — it pins that the fixture is an OFFSET rather than a value
    that merely happens to be far away today.
    """
    from datetime import date, timedelta

    import test_option_positions as top

    expected = (date.today() + timedelta(days=40)).isoformat()
    assert top._EXP == expected, (
        f"_EXP = {top._EXP!r} is not today+40d ({expected!r}); it has been "
        f"pinned to a literal again")


if __name__ == "__main__":
    test_no_absolute_expiration_constants()
    print("OK (direct run: static scan only; the import-based checks need pytest)")
