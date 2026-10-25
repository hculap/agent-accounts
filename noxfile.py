"""Repeatable development checks; install Nox with `uvx nox` or `pipx install nox`."""

import os
import pathlib
import tarfile

import nox

nox.options.sessions = ["tests", "lint", "package"]


@nox.session
def tests(session):
    """Run account and CLI tests against an installed package."""
    session.install(".")
    if os.name == "nt":
        session.install("distlib>=0.3.9")
    session.run("python", "-m", "unittest", "discover", "-s", "tests", "-v", *session.posargs)


@nox.session
def lint(session):
    """Check Python syntax, imports, and common defects."""
    session.install("ruff>=0.9,<1")
    session.run("ruff", "check", ".", *session.posargs)


@nox.session
def package(session):
    """Build an sdist and wheel, then smoke-test an isolated wheel installation."""
    session.install("build>=1.2,<2")
    temporary = pathlib.Path(session.create_tmp()).resolve()
    artifacts = temporary / "dist"
    session.run("python", "-m", "build", "--outdir", str(artifacts))
    sources = list(artifacts.glob("*.tar.gz"))
    if len(sources) != 1:
        session.error(f"Expected one source archive, found {len(sources)}")
    with tarfile.open(sources[0]) as archive:
        members = {pathlib.PurePosixPath(name).parts[1:] for name in archive.getnames()}
    for source in pathlib.Path("tests").glob("*.py"):
        if source.parts not in members:
            session.error(f"Source archive is missing a required test fixture: {source}")
    wheels = list(artifacts.glob("*.whl"))
    if len(wheels) != 1:
        session.error(f"Expected one wheel, found {len(wheels)}")
    environment = temporary / "installed"
    session.run("python", "-m", "venv", str(environment))
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    interpreter = scripts / ("python.exe" if os.name == "nt" else "python")
    session.run(
        str(interpreter),
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--force-reinstall",
        str(wheels[0]),
        external=True,
    )
    # A different working directory prevents accidental imports from the checkout.
    session.chdir(temporary)
    for name in ("agent-accounts", "aa"):
        executable = scripts / (f"{name}.exe" if os.name == "nt" else name)
        session.run(str(executable), "--help", external=True)
        session.run(str(executable), "--version", external=True)
