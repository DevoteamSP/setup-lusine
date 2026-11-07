# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Set up L'Usine with `uv` as the only prerequisite for a visitor.

This file is the only public piece of L'Usine: everything else (the app, the registry, the assets)
stays in private DevoteamSP repositories. It signs you in to GitHub, downloads the private
`DevoteamSP/lusine` repository and starts it. Standard library only, no secret inside (the GitHub
App client id is public by design).

    uv run https://raw.githubusercontent.com/<public repo>/main/setup_lusine.py
    uvx --from git+https://github.com/<public repo> setup_lusine        (needs git)

Two profiles:
  visitor    "I just want to see it": the files as plain files. Nothing to install but uv.
  developer  "I need to clone and have everything": a git clone. Needs git and the GitHub CLI (gh).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Sequence
from pathlib import Path

REPO = os.environ.get("LUSINE_REPO_SLUG", "DevoteamSP/lusine")
API = os.environ.get("LUSINE_GITHUB_API", "https://api.github.com")
WEB = os.environ.get("LUSINE_GITHUB_WEB", "https://github.com")
# Public, like any client id: the one of the DevoteamSP GitHub App "L'Usine Navigator".
CLIENT_ID = os.environ.get("LUSINE_GITHUB_CLIENT_ID", "Iv23liOuq0FtQsxn4fgC")
HOME_DIR = Path(os.environ.get("LUSINE_HOME") or Path.home() / ".lusine")
TOKEN_FILE = HOME_DIR / "github_token.json"
MODE_FILE = HOME_DIR / "install-mode"
DEFAULT_DIR = Path.home() / "dev" / "snow-builders"
TOKEN_PAGE = (
    "https://github.com/settings/personal-access-tokens/new?name=L%27Usine"
    "&description=Lets+L%27Usine+download+the+DSP+assets+for+me&target_name=DevoteamSP"
    "&expires_in=365&contents=read"
)
ACCESS_HELP = (
    "Usually your GitHub account is not in the DevoteamSP organization yet (ask the DSP team),\n"
    "or you were invited but have not accepted yet: open the invitation e-mail from GitHub,\n"
    "accept it, then run this again."
)
sleep = time.sleep  # replaced in the tests


class SetupError(Exception):
    """Something the person can act on; the message says what."""


def say(text: str = "") -> None:
    print(f"    {text}" if text else "")


def step(number: int, total: int, title: str) -> None:
    print(f"\n==> Step {number} of {total}: {title}")


# --- GitHub ----------------------------------------------------------------------------------


def _open(url: str, *, data: dict[str, str] | None = None, token: str | None = None):
    headers = {"Accept": "application/json", "User-Agent": "setup-lusine"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    return urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=60)


def _post_json(url: str, data: dict[str, str]) -> dict:
    try:
        with _open(url, data=data) as reply:
            return json.loads(reply.read().decode())
    except urllib.error.URLError as exc:
        raise SetupError(f"Could not reach GitHub ({exc.reason}). Check your connection.") from exc


def repo_status(token: str) -> int:
    """The HTTP status GitHub gives for the repository with this token (0: unreachable)."""
    try:
        with _open(f"{API}/repos/{REPO}", token=token) as reply:
            return reply.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return 0


def code_signin(*, interactive: bool) -> str:
    """The sign-in with a code: GitHub shows a code, you type it in the browser, this waits."""
    reply = _post_json(f"{WEB}/login/device/code", {"client_id": CLIENT_ID})
    if "device_code" not in reply:
        why = reply.get("error_description") or reply.get("error") or "no reason given"
        raise SetupError(f"GitHub did not give a code: {why}.")
    uri = reply.get("verification_uri") or f"{WEB}/login/device"
    say("L'Usine's files are private, so GitHub must know who you are. It takes a minute:")
    say("  1. Your code is below. 2. GitHub opens in your browser.")
    say('  3. Type the code there, click "Continue", then click "Authorize".')
    say("Nothing to copy back: L'Usine receives the sign-in by itself.")
    print(f"\n        Your code:  {reply['user_code']}\n")
    if interactive:
        input("    Press Enter to open GitHub (or Control-C to stop). ")
        webbrowser.open(uri)
    say("Waiting for you to authorize it on GitHub...")
    interval = int(reply.get("interval", 5))
    deadline = time.monotonic() + int(reply.get("expires_in", 900))
    failures = 0
    while time.monotonic() < deadline:
        sleep(interval)
        try:
            poll = _post_json(
                f"{WEB}/login/oauth/access_token",
                {
                    "client_id": CLIENT_ID,
                    "device_code": reply["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except SetupError:
            failures += 1
            if failures >= 5:
                raise
            continue
        failures = 0
        if poll.get("access_token"):
            return poll["access_token"]
        error = poll.get("error", "")
        if error == "slow_down":
            interval = int(poll.get("interval", interval + 5))
        elif error == "expired_token":
            raise SetupError("The code expired. Run this again for a new one.")
        elif error == "access_denied":
            raise SetupError("The sign-in was cancelled on GitHub.")
        elif error != "authorization_pending":
            why = poll.get("error_description") or error or "no reason given"
            raise SetupError(f"GitHub refused the sign-in: {why}.")
    raise SetupError("Nobody authorized the code in time. Run this again.")


def token_signin(*, interactive: bool) -> str:
    """The fallback: a fine-grained read-only access token created once and pasted here."""
    if not interactive:
        raise SetupError("I need to ask you for a token, but there is no keyboard here.")
    say("The sign-in with a code did not work. An access token does the same job:")
    say('  1. Press Enter: GitHub opens, owner DevoteamSP. Choose "All repositories".')
    say('  2. Click "Generate token" (twice), copy it, come back here and paste it.')
    say("DevoteamSP's owners may have to approve it: tell the DSP team if GitHub says pending.")
    input("    Press Enter to open GitHub (or Control-C to stop). ")
    webbrowser.open(TOKEN_PAGE)
    for _ in range(3):
        pasted = "".join(getpass.getpass("Paste the token (nothing shows): ").split())
        if pasted and repo_status(pasted) == 200:
            return pasted
        say("That token does not open the repository. Check the steps and try again.")
    raise SetupError("The token did not work three times. Check the steps, then run this again.")


def saved_token() -> str:
    for name in ("LUSINE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    try:
        return str(json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("token", "")).strip()
    except (OSError, ValueError):
        return ""


def get_token(*, interactive: bool) -> str:
    token = saved_token()
    if token and repo_status(token) == 200:
        say("GitHub sign-in: ready")
        return token
    if not interactive and not CLIENT_ID:
        raise SetupError("No GitHub sign-in and no keyboard to ask for one.")
    try:
        token = code_signin(interactive=interactive) if CLIENT_ID else ""
    except SetupError as exc:
        say(str(exc))
        token = ""
    if not token:
        return token_signin(interactive=interactive)
    status = repo_status(token)
    if status == 0:
        raise SetupError("Could not reach GitHub. Check your connection and run this again.")
    if status != 200:
        raise SetupError(
            f"You are signed in, but GitHub does not let you open {REPO} (code {status}).\n"
            f"{ACCESS_HELP}"
        )
    say("GitHub sign-in: ready")
    return token


# --- download --------------------------------------------------------------------------------


def download(target: Path, token: str) -> str:
    """Unpack the latest files of the repository into ``target`` (new or empty). Returns the sha."""
    try:
        with _open(f"{API}/repos/{REPO}/commits/HEAD", token=token) as reply:
            sha = reply.read().decode().strip()
        archive = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        with archive, _open(f"{API}/repos/{REPO}/tarball/{sha}", token=token) as reply:
            shutil.copyfileobj(reply, archive)
    except urllib.error.URLError as exc:
        raise SetupError(f"Could not download L'Usine ({exc}). Run this again.") from exc
    try:
        extract(Path(archive.name), target)
    finally:
        Path(archive.name).unlink(missing_ok=True)
    return sha


def extract(archive: Path, target: Path) -> None:
    """Unpack a GitHub tarball without its top folder, refusing anything that leaves ``target``."""
    root = target.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = []
            for member in tar.getmembers():
                parts = Path(member.name).parts[1:]
                if not parts or member.name.startswith("/") or ".." in parts:
                    continue
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    continue
                member.name = "/".join(parts)
                if root not in (root / member.name).resolve().parents:
                    raise SetupError("The download holds a file outside its folder.")
                members.append(member)
            target.mkdir(parents=True, exist_ok=True)
            tar.extractall(target, members=members, filter="data")
    except (tarfile.TarError, EOFError, OSError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise SetupError("The download is damaged. Run this again.") from exc


# --- the rest ---------------------------------------------------------------------------------


def uv_binary() -> str:
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        raise SetupError("uv not found. Install it from https://docs.astral.sh/uv/")
    return found


def run(
    command: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> int:
    merged = {**os.environ, **(env or {})}
    try:
        return subprocess.run(list(command), cwd=cwd, env=merged, check=False).returncode
    except KeyboardInterrupt:
        return 130


def installer(target: Path, *args: str, token: str = "") -> None:
    """L'Usine's own Python helper, now unpacked: it marks the copy and keeps the token."""
    env = {"PYTHONPATH": str(target / "navigator"), "PYTHONDONTWRITEBYTECODE": "1"}
    if token:
        env["LUSINE_TOKEN"] = token
    command = [uv_binary(), "run", "--no-project", "--python", ">=3.11", "python", "-m"]
    if run([*command, "utils.installer", *args], env=env, cwd=target):
        raise SetupError("Could not finish unpacking L'Usine. Run this again.")


def visitor(target: Path, *, interactive: bool, dry_run: bool) -> None:
    step(2, 4, "Sign in to GitHub")
    if dry_run:
        say("(dry run) sign in, then download the latest files of " + REPO)
        return
    token = get_token(interactive=interactive)
    step(3, 4, "Download L'Usine")
    if (target / ".lusine-snapshot.json").is_file():
        say("L'Usine is already here: updating it")
        installer(target, "update", str(target), token=token)
    elif target.exists() and any(target.iterdir()):
        raise SetupError(f"The folder {target} exists and was not made by L'Usine. Use --dir.")
    else:
        say("Downloading (the first time takes a minute)")
        sha = download(target, token)
        installer(target, "adopt", str(target), REPO, sha, token=token)
    installer(target, "save-token", token=token)


def have(name: str) -> bool:
    return shutil.which(name) is not None


def developer(target: Path, *, interactive: bool, dry_run: bool) -> None:
    step(2, 4, "Check git and the GitHub tool (gh)")
    missing = [name for name in ("git", "gh") if not have(name)]
    if missing:
        hint = {"darwin": "brew install git gh", "win32": "winget install Git.Git GitHub.cli"}
        how = hint.get(sys.platform, "your package manager")
        raise SetupError(
            f"Missing: {', '.join(missing)}. Install with:  {how}\nthen run this again."
        )
    say("git and gh: ready")
    step(3, 4, "Sign in and download")
    if dry_run:
        say(f"(dry run) gh auth login, then gh repo clone {REPO} {target}")
        return
    login = ["gh", "auth", "login", "--web", "--git-protocol", "https"]
    if run(["gh", "auth", "status"]) and run(login):
        raise SetupError("The GitHub sign-in did not work.")
    run(["gh", "auth", "setup-git"])
    if run(["gh", "api", f"repos/{REPO}", "--jq", ".full_name"]):
        raise SetupError(f"GitHub does not let you open {REPO}.\n{ACCESS_HELP}")
    if (target / ".git").is_dir():
        say("L'Usine is already here: updating it")
        run(["git", "-C", str(target), "pull", "--ff-only"])
    elif target.exists() and any(target.iterdir()):
        raise SetupError(f"The folder {target} exists and is not a clone. Use --dir.")
    elif run(["gh", "repo", "clone", REPO, str(target)]):
        raise SetupError("Could not clone L'Usine.")


def choose_profile(asked: str | None, *, interactive: bool) -> str:
    if asked:
        return asked
    try:
        previous = MODE_FILE.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        previous = "visitor"
    default = "2" if previous == "developer" else "1"
    if not interactive:
        return "developer" if default == "2" else "visitor"
    print(
        "Who are you?\n\n"
        "  1. Visitor: I just want to see it.\n"
        "     Installs nothing but uv. You sign in to GitHub once with a code.\n\n"
        "  2. Developer: I need to clone and have everything.\n"
        "     L'Usine and the assets are git clones. Needs git and the GitHub tool (gh).\n"
    )
    choice = input(f"Your choice, 1 or 2 [{default}]: ").strip() or default
    return "developer" if choice == "2" else "visitor"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_lusine", description="Set up L'Usine.")
    parser.add_argument("--visitor", dest="profile", action="store_const", const="visitor")
    parser.add_argument("--developer", dest="profile", action="store_const", const="developer")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="where to keep the files")
    parser.add_argument("--yes", action="store_true", help="take every default, never ask")
    parser.add_argument("--dry-run", action="store_true", help="say what would change")
    parser.add_argument("--no-launch", action="store_true", help="stop before starting L'Usine")
    args = parser.parse_args(argv)
    interactive = not args.yes and sys.stdin.isatty()
    try:
        step(1, 4, "Who are you?")
        profile = choose_profile(args.profile, interactive=interactive)
        target = args.dir.expanduser().resolve() / "lusine"
        say(f"{profile.capitalize()}: L'Usine will live in {target}")
        (visitor if profile == "visitor" else developer)(
            target, interactive=interactive, dry_run=args.dry_run
        )
        if args.dry_run:
            return 0
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        MODE_FILE.write_text(profile + "\n", encoding="utf-8")
        credentials = Path.home() / ".streamlit" / "credentials.toml"
        if not credentials.exists():  # without it Streamlit asks for an e-mail and waits
            credentials.parent.mkdir(parents=True, exist_ok=True)
            credentials.write_text('[general]\nemail = ""\n', encoding="utf-8")
        step(4, 4, "Start L'Usine")
        if args.no_launch:
            say(f"Ready. Start it later with:  cd {target}  then  uv run lusine")
            return 0
        say("Your browser opens in a moment. Keep this window open; close it to stop.")
        code = run([uv_binary(), "run", "lusine"], cwd=target)
        return 0 if code == 130 else code
    except SetupError as exc:
        print(f"\n!! {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
