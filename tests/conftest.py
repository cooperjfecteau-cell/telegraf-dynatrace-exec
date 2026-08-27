"""Fixtures: a real Telegraf binary and a POSIX shell to run the wrapper with."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Pinned so a Telegraf release cannot turn a green suite red overnight. Bump it
# deliberately; the installer itself always resolves the latest version at run time.
TELEGRAF_TEST_VERSION = "1.39.3"
DOWNLOAD_BASE = "https://dl.influxdata.com/telegraf/releases"
CACHE_DIR = REPO_ROOT / ".telegraf-cache"


@pytest.fixture(scope="session")
def telegraf_bin() -> str:
    """The telegraf executable: TELEGRAF_BIN, one on PATH, or a cached download."""
    from_env = os.environ.get("TELEGRAF_BIN")
    if from_env:
        if not Path(from_env).exists():
            pytest.fail(f"TELEGRAF_BIN is set to {from_env}, which does not exist")
        return from_env

    on_path = shutil.which("telegraf")
    if on_path:
        return on_path

    return _download_telegraf()


@pytest.fixture(scope="session")
def shell_bin() -> str:
    """A POSIX shell Telegraf can exec.

    On Linux this is what the installer uses in production. On Windows the tests borrow
    Git's sh.exe from its real path, spaces and all: the generated config uses
    Telegraf's argv array form, so a space in the path must not break it.
    """
    if platform.system() != "Windows":
        return "/bin/sh"

    candidates = [
        Path(r"C:\Program Files\Git\usr\bin\sh.exe"),
        Path(r"C:\Program Files (x86)\Git\usr\bin\sh.exe"),
    ]
    found = next((path for path in candidates if path.exists()), None)
    if found is None:
        located = shutil.which("sh")
        if located is None:
            pytest.skip("no POSIX shell available to run the command wrapper")
        found = Path(located)
    return str(found).replace("\\", "/")


def _download_telegraf() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        pytest.skip(f"no Telegraf build mapped for architecture {machine}")

    CACHE_DIR.mkdir(exist_ok=True)
    binary_name = "telegraf.exe" if system == "windows" else "telegraf"
    cached = next(CACHE_DIR.rglob(binary_name), None)
    if cached is not None:
        return str(cached)

    if system == "windows":
        url = f"{DOWNLOAD_BASE}/telegraf-{TELEGRAF_TEST_VERSION}_windows_{arch}.zip"
    elif system == "linux":
        url = f"{DOWNLOAD_BASE}/telegraf-{TELEGRAF_TEST_VERSION}_linux_{arch}.tar.gz"
    else:
        pytest.skip(f"no Telegraf download mapped for {system}")

    archive = CACHE_DIR / Path(url).name
    try:
        urllib.request.urlretrieve(url, archive)  # noqa: S310 - fixed InfluxData URL
    except Exception as exception:
        pytest.skip(f"could not download Telegraf for the tests: {exception}")

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(CACHE_DIR)
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(CACHE_DIR)  # noqa: S202 - trusted InfluxData release

    found = next(CACHE_DIR.rglob(binary_name), None)
    if found is None:
        pytest.skip("the Telegraf archive did not contain the expected binary")
    if system != "windows":
        found.chmod(0o755)
    return str(found)


@pytest.fixture(scope="session", autouse=True)
def _require_bash():
    """Every test renders the config with the script, so bash has to be available."""
    if shutil.which("bash") is None:
        pytest.skip("bash is required to render the configuration")
    subprocess.run(["bash", "-n", str(REPO_ROOT / "install-telegraf-dynatrace.sh")], check=True)
