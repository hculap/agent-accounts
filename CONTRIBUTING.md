# Contributing

Use Python 3.11 or later. The application uses only the standard library; Nox, Ruff, and the build frontend are development tools.

## Run checks

From a checkout:

```sh
uvx nox
```

Or install Nox with pipx, then run it:

```sh
pipx install nox
nox
```

The default sessions run the test suite, lint the Python files, and build an sdist and wheel. The package session installs the wheel in another virtual environment and checks both console scripts from outside the checkout.

Run one session with `nox -s tests`, `nox -s lint`, or `nox -s package`. For a local editable environment:

```sh
python -m venv .venv
```

Use that environment's Python to install `.[dev]`, then run `python -m unittest discover -s tests -v`. Activation commands depend on your shell; the package itself does not.

CI runs tests and wheel installation on Linux, macOS, and Windows with Python 3.11 and 3.14. A passing local run covers only the platform and interpreter actually used; check the full CI matrix before claiming portability.

## Change behavior with tests

Write a focused failing test for the behavior, confirm the failure, then implement the change. Exercise public commands, provider arguments and environment, persisted state, subprocess signals, and failure recovery. Use temporary directories and synthetic credential fixtures only. Tests must never read a developer's live credentials, invoke a real login, or make paid model requests.

For rotation changes, cover both the true quota signal and a similar message that must not trigger rotation. Include terminal failures, cancellations, bounded retries, cooldowns, and the point at which replay becomes unsafe. Do not weaken these boundaries to make a new provider version pass.

## Pull requests

Explain the user-visible problem and resulting behavior. Include the checks you ran, relevant platform limitations, and any provider version assumptions. Keep source credentials, provider transcripts, and private account identifiers out of patches and test fixtures.

For packaging changes, inspect both distribution archives before publication. Python distributions must contain source and project metadata only; no provider homes or developer state should be included.
