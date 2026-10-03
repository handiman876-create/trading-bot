"""
Guards the 2026-10-03 venv migration: the bots run on .venv, not system pip.

WHY THIS EXISTS: numpy/pandas/python-dotenv used to come from system pip in
/usr/local/lib/python3.12/dist-packages. The 26.04 upgrade drops 3.12, so that
tree goes dark and every bot crash-loops on import. The fix installed them IN
.venv (same versions) and pointed every unit and wrapper at .venv/bin/python.
Both halves can silently regress: a `pip uninstall` in the venv falls back to
/usr/local without a single error, and a new wrapper copied from an old one
brings `/usr/bin/python3` back. Neither shows up until the OS upgrade.

The apt half is the opposite rule. requests/certifi stay apt-provided on
purpose: Ubuntu's certifi returns the system bundle (/etc/ssl/certs), pip's
ships its own. Shadowing them with pip copies would change which CAs the
broker connection trusts, which is a behaviour change, not a packaging one.

Must run under .venv/bin/python (run_test.py spawns children with
sys.executable). Under system python3 the first check fails, which is correct.
NOTE: /proc/<pid>/exe of a bot reads /usr/bin/python3.12 — the venv python is
a symlink. sys.prefix, not the executable, is what identifies the venv.
"""

import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(REPO, ".venv")
VENV_SITE = os.path.join(VENV, "lib")
APT_SITE = "/usr/lib/python3/dist-packages"
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

VENV_MODULES = ("numpy", "pandas", "dotenv")
APT_MODULES = ("requests", "certifi")
BAD_INTERPRETER = "/usr/bin/python3"
BOT_UNITS = ("trading-bot-equities.service", "trading-bot-futures.service")


def _origin(mod_name):
    mod = __import__(mod_name)
    return os.path.realpath(mod.__file__)


def test_running_in_repo_venv():
    assert os.path.realpath(sys.prefix) == os.path.realpath(VENV), \
        f"sys.prefix={sys.prefix}; run under {VENV}/bin/python"


def test_pip_deps_resolve_from_venv():
    for name in VENV_MODULES:
        path = _origin(name)
        assert path.startswith(os.path.realpath(VENV_SITE)), \
            f"{name} resolves from {path}, not the venv — reinstall it into .venv"


def test_requests_certifi_stay_apt():
    for name in APT_MODULES:
        path = _origin(name)
        assert path.startswith(APT_SITE), \
            f"{name} resolves from {path}; it must stay apt-provided ({APT_SITE})"


def test_ca_bundle_is_system():
    import certifi
    import requests.certs
    for label, where in (("certifi.where()", certifi.where()),
                         ("requests.certs.where()", requests.certs.where())):
        assert os.path.realpath(where) == os.path.realpath(SYSTEM_CA_BUNDLE), \
            f"{label}={where}; expected the system bundle {SYSTEM_CA_BUNDLE}"


def _scan(paths):
    hits = []
    for path in paths:
        with open(path) as f:
            for n, line in enumerate(f, 1):
                if BAD_INTERPRETER in line:
                    hits.append(f"{path}:{n}: {line.strip()}")
    return hits


def test_no_system_python_in_deploy():
    deploy = os.path.join(REPO, "deploy")
    files = [os.path.join(deploy, f) for f in sorted(os.listdir(deploy))
             if os.path.isfile(os.path.join(deploy, f))]
    hits = _scan(files)
    assert not hits, "system python in deploy/:\n  " + "\n  ".join(hits)


def test_installed_bot_units_use_venv():
    # The repo copies are covered above; this catches an install that never
    # happened (repo fixed, /etc still on the old ExecStart).
    installed = [os.path.join("/etc/systemd/system", u) for u in BOT_UNITS]
    present = [p for p in installed if os.path.exists(p)]
    hits = _scan(present)
    assert not hits, "system python in installed units:\n  " + "\n  ".join(hits)
    for path in present:
        with open(path) as f:
            execs = [l.strip() for l in f if l.startswith("ExecStart=")]
        assert execs and all(l.startswith(f"ExecStart={VENV}/bin/python ")
                             for l in execs), f"{path}: {execs}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"All {len(tests)} runtime-env checks passed.")
