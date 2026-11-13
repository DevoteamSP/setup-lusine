"""A whole setup, the way a person runs it, against a fake GitHub (no network, no secret).

    uv run tests/e2e.py visitor

The fake carries a small stand-in for L'Usine: a project `uv sync` can install and the
`utils.installer` helper that the real L'Usine ships. What is under test is the setup script:
sign-in check, download, unpack, mark the copy, save the token, install the libraries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fake_github import FakeGitHub, serve  # noqa: E402

REPO = "DevoteamSP/lusine"

INSTALLER = '''"""Stand-in for L'Usine's utils.installer."""
import json, os, sys
from pathlib import Path

command, *rest = sys.argv[1:]
if command in ("adopt", "update"):
    Path(".lusine-snapshot.json").write_text(json.dumps({"args": rest}))
elif command == "save-token":
    home = Path(os.environ["LUSINE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "github_token.json").write_text(json.dumps({"token": os.environ["LUSINE_TOKEN"]}))
else:
    sys.exit(f"unknown command {command}")
'''

FILES = {
    "README.md": b"# L'Usine (test copy)\n",
    "pyproject.toml": b'[project]\nname = "lusine"\nversion = "0"\ndependencies = []\n',
    "navigator/utils/__init__.py": b"",
    "navigator/utils/installer.py": INSTALLER.encode(),
}


def preview() -> None:
    """The setup in your own terminal against the fake GitHub, slowed down to watch the screen."""
    files = {**FILES, "pad.bin": os.urandom(12 * 1024 * 1024)}
    state = FakeGitHub(repos={REPO: files}, throttle=0.06, pending_polls=3, device_interval=2)
    server, url = serve(state)
    work = Path(tempfile.mkdtemp(prefix="sl-preview-"))
    env = {
        **os.environ,
        "LUSINE_REPO_SLUG": REPO,
        "LUSINE_GITHUB_API": url,
        "LUSINE_GITHUB_WEB": url,
        "LUSINE_GITHUB_CLIENT_ID": "preview",
        "LUSINE_HOME": str(work / "home"),
        "BROWSER": "true",  # do not open a real browser
        "LUSINE_DESKTOP": str(work / "Desktop"),  # not your real Desktop
    }
    for name in ("LUSINE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(name, None)
    command = ["uv", "run", str(ROOT / "setup_lusine.py"), "--no-launch"]
    command += ["--dir", str(work / "files"), *sys.argv[2:]]
    subprocess.run(command, env=env, check=False)
    server.shutdown()


def visitor() -> None:
    state = FakeGitHub(repos={REPO: FILES})
    server, url = serve(state)
    work = Path(tempfile.mkdtemp(prefix="sl-e2e-"))
    home = work / "home"
    env = {
        **os.environ,
        "LUSINE_REPO_SLUG": REPO,
        "LUSINE_GITHUB_API": url,
        "LUSINE_GITHUB_WEB": url,
        "LUSINE_HOME": str(home),
        "LUSINE_TOKEN": state.token,
        "LUSINE_DESKTOP": str(work / "Desktop"),
        "PYTHONUTF8": "1",
    }
    command = ["uv", "run", str(ROOT / "setup_lusine.py")]
    command += ["--visitor", "--yes", "--no-launch", "--shortcut", "--dir", str(work / "files")]
    done = subprocess.run(command, env=env, check=False)
    server.shutdown()
    target = work / "files" / "lusine"
    problems = []
    if done.returncode:
        problems.append(f"the script exited with {done.returncode}")
    for name in ("README.md", "pyproject.toml", ".lusine-snapshot.json", ".venv"):
        if not (target / name).exists():
            problems.append(f"missing in the copy: {name}")
    saved = home / "github_token.json"
    if not saved.is_file() or json.loads(saved.read_text())["token"] != state.token:
        problems.append("the token was not saved")
    if sys.platform in ("darwin", "win32"):  # the Desktop shortcut: where it goes, what it runs
        name = "Lusine.cmd" if sys.platform == "win32" else "Lusine.command"
        launcher = work / "Desktop" / name
        if sys.platform == "win32" and (work / "Desktop" / "Lusine.lnk").is_file():
            launcher = home / name  # the .lnk with the logo points at the launcher kept here
        text = launcher.read_text(encoding="utf-8") if launcher.is_file() else ""
        # the script writes the resolved path (a runner's temp folder has a short 8.3 name too)
        if str(target.resolve()) not in text or "run lusine" not in text:
            problems.append("the Desktop shortcut is missing or does not start L'Usine")
    if not (home / "install-mode").read_text().startswith("visitor"):
        problems.append("the profile was not remembered")
    if problems:
        sys.exit("VISITOR SETUP FAILED:\n  " + "\n  ".join(problems))
    print("visitor setup complete:", target)


def developer(target: Path) -> None:
    """After the real setup: a git clone with the files, a virtualenv, the profile remembered."""
    problems = [
        f"missing in the clone: {name}"
        for name in (".git", "pyproject.toml", ".venv")
        if not (target / name).exists()
    ]
    mode = Path(os.environ["LUSINE_HOME"]) / "install-mode"
    if not mode.is_file() or not mode.read_text().startswith("developer"):
        problems.append("the profile was not remembered")
    if problems:
        sys.exit("DEVELOPER SETUP FAILED:\n  " + "\n  ".join(problems))
    print("developer setup complete:", target)


if __name__ == "__main__":
    if sys.argv[1] == "visitor":
        visitor()
    elif sys.argv[1] == "preview":
        preview()
    else:
        developer(Path(sys.argv[2]))
