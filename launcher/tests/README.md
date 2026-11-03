# Launcher tests

Run on Windows from the repository root with Python 3.11 or newer:

```powershell
python -B -m unittest discover -s launcher/tests -p 'test_*.py' -v
```

The suites cover launcher manifest signatures and verified setup downloads,
explicit update consent in the Tk window, local HTTP update transport and cache
fallbacks, offline publication with mocked GitHub calls, and safe game writes.
They create synthetic data in temporary directories under `launcher/tests` and
remove it afterward. `test_launcher_auth` uses Windows PowerShell to generate an
ephemeral RSA key. `test_safe_apply` requires Windows junction support. The Tk
test requires an interactive desktop. The transport suite requires loopback TCP.
Windows sandboxes that deny loopback connections or junction creation skip only
those checks.

The larger checks are opt-in scripts under `integration/`:

```powershell
python -B launcher/tests/integration/consent_ui.py
python -B launcher/tests/integration/acceptance.py --old-archive OLD.zip --exe UmaRU.exe --channel-dir CHANNEL --scratch SCRATCH --require-delta
python -B launcher/tests/integration/verify_live.py --manifest launcher.json --channel channel.json --scratch SCRATCH
```

`acceptance.py` creates a disposable synthetic game, installs the previous real
archive, upgrades through a local server using the approved channel assets,
verifies every installed hash, then checks current and offline cache behavior.
It requires a separately prepared old archive, packaged EXE, approved channel
and a writable scratch directory. `verify_live.py` checks the public signed 1.2
manifest and downloads its installer for hash verification without running it;
it requires network access and an approved signed manifest. The historical
`public_smoke.py` and `verify_*` checks are not shipped because they depend on a
past release and local receipts. Live game behavior, Steam, and installation on
the user's machine still require separate validation. No game files, release
archives, build outputs, or publication receipts are part of this source tree.
