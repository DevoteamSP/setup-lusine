# /// script
# requires-python = ">=3.11,<3.13"
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
import functools
import getpass
import itertools
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
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
SILENT = {  # for commands whose output nobody needs
    "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    "check": False,
}  # fmt: skip


class SetupError(Exception):
    """Something the person can act on; the message says what."""


# --- terminal ---------------------------------------------------------------------------------
# Standard library only, and ASCII in this file: the fancy glyphs are written as escapes. When the
# output is not a terminal (a pipe, CI, NO_COLOR) everything falls back to plain text.

_UNICODE = {
    "ok": "\u2714", "bad": "\u2716", "warn": "!", "dot": "\u25cf", "on": "\u2588", "off": "\u2591",
    "h": "\u2500", "v": "\u2502", "tl": "\u256d", "tr": "\u256e", "bl": "\u2570", "br": "\u256f",
    "frames": "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f",
}  # fmt: skip
_PLAIN = {
    "ok": "[ok]", "bad": "[!!]", "warn": "[!]", "dot": "*", "on": "#", "off": "-",
    "h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+", "frames": "|/-\\",
}  # fmt: skip
STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


@functools.cache
def fancy() -> bool:
    """True on an interactive terminal that understands colors and cursor control."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb" or not sys.stdout.isatty():
        return False
    if os.name == "nt":  # Windows 10+ understands ANSI once the console asks for it
        try:
            import ctypes

            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if not kernel.SetConsoleMode(handle, mode.value | 0x0004):
                return False
        except (AttributeError, OSError):
            return False
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False
    return True


def g(name: str) -> str:
    return (_UNICODE if fancy() else _PLAIN)[name]


def paint(text: str, *styles: str) -> str:
    if not fancy():
        return text
    codes = ";".join(STYLES[style] for style in styles)
    return f"\x1b[{codes}m{text}\x1b[0m"


def width() -> int:
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))


def say(text: str = "") -> None:
    print(f"    {text}" if text else "")


def good(text: str) -> None:
    print(f"  {paint(g('ok'), 'green')} {text}")


def warn(text: str) -> None:
    print(f"  {paint(g('warn'), 'yellow', 'bold')} {text}")


def box(lines: Sequence[str], *, color: str = "cyan", indent: int = 2) -> None:
    inner = max(len(line) for line in lines) + 4
    pad = " " * indent
    print(pad + paint(g("tl") + g("h") * inner + g("tr"), color))
    for line in lines:
        side = paint(g("v"), color)
        print(f"{pad}{side}  {paint(line.ljust(inner - 4), 'bold')}  {side}")
    print(pad + paint(g("bl") + g("h") * inner + g("br"), color))


def banner() -> None:
    print()
    box(
        [
            "L'USINE  -  where data gets its hard hat",
            "Snow Builders, Devoteam Snowflake Partner",
            "No snowflakes were harmed in this setup.",
        ]
    )


STEPS = {"total": 0}  # set once the profile is known: visitor 4 steps, developer 5


def step(number: int, title: str) -> None:
    label = f" {number}/{STEPS['total']} " if STEPS["total"] else f" {number} "
    rule = g("h") * max(4, width() - len(label) - len(title) - 6)
    print(f"\n  {paint(label, 'bold', 'cyan')} {paint(title, 'bold')} {paint(rule, 'dim')}")


def ask_yes(question: str, *, default: bool = True) -> bool:
    answer = input(f"  {paint('?', 'cyan', 'bold')} {question} [{'Y/n' if default else 'y/N'}] ")
    return default if not answer.strip() else answer.strip().lower().startswith("y")


def propose(command: Sequence[str], why: str, *, interactive: bool) -> bool:
    """Show the command line, then run it for the person, or let them run it themselves.

    Returns True when this script ran it, False when the person said they would (or did).
    """
    shown = " ".join(f'"{part}"' if " " in part else part for part in command)
    say(why)
    print(f"\n      {paint('$', 'dim')} {paint(shown, 'bold', 'cyan')}\n")
    if not interactive or ask_yes("Run it for you?"):
        return True
    say("Run it in another window, then come back here.")
    input("    Press Enter when it is done. ")
    return False


class Task:
    """One line that spins while work goes on, then settles into a tick (or a cross) and a time.

    Set ``detail`` from the work to show progress next to the label; ``failed`` for a cross.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.detail = ""
        self.failed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0

    def progress(self, done: int, total: int | None) -> None:
        mb = f"{done / 1e6:.1f} MB"
        if not total:
            self.detail = mb
            return
        cells = 20
        filled = min(cells, done * cells // total)
        bar = g("on") * filled + g("off") * (cells - filled)
        self.detail = f"{bar} {done * 100 // total:3d}%  {mb}"

    def _spin(self) -> None:
        for frame in itertools.cycle(g("frames")):
            room = max(0, width() - len(self.label) - 8)
            detail = paint(self.detail[:room], "dim") if self.detail else ""
            sys.stdout.write(f"\r\x1b[2K  {paint(frame, 'cyan')} {self.label}  {detail}")
            sys.stdout.flush()
            if self._stop.wait(0.08):
                return

    def __enter__(self) -> Task:
        self._started = time.monotonic()
        if fancy():
            sys.stdout.write("\x1b[?25l")  # hide the cursor while spinning
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"    {self.label}...")
        return self

    def __exit__(self, exc_type, *_exc) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join()
            sys.stdout.write("\r\x1b[2K\x1b[?25h")
        if exc_type is not None or self.failed:
            print(f"  {paint(g('bad'), 'red')} {self.label}")
        else:
            took = paint(f"{time.monotonic() - self._started:.1f}s", "dim")
            print(f"  {paint(g('ok'), 'green')} {self.label}  {took}")
        return False


def run_quiet(
    command: Sequence[str],
    label: str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Run a command behind a spinner showing its last line; on failure print what it said."""
    merged = {**os.environ, **(env or {})}
    tail: deque[str] = deque(maxlen=12)
    code = 1
    with Task(label) as task:
        try:
            proc = subprocess.Popen(  # noqa: S603
                list(command), cwd=cwd, env=merged, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )  # fmt: skip
        except OSError as exc:
            tail.append(str(exc))
            task.failed = True
        else:
            try:
                for line in proc.stdout or ():
                    if line.strip():
                        tail.append(line.rstrip())
                        task.detail = line.strip()
                code = proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
            task.failed = code != 0
    if code:
        for line in tail:
            say(paint(line, "dim"))
    return code


def copy_to_clipboard(text: str) -> bool:
    other = ["xclip", "-selection", "c"]
    tools = {"darwin": ["pbcopy"], "win32": ["clip"]}.get(sys.platform, other)
    try:
        return subprocess.run(tools, input=text, text=True, timeout=5, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- GitHub ----------------------------------------------------------------------------------


WINDOWS_ROOTS: dict[str, Path | None] = {}  # filled once: the file with Microsoft's roots


def fetch_windows_roots() -> Path | None:
    """Ask Windows Update for Microsoft's list of trusted root certificates, as a .pem file.

    A fresh Windows machine may lack a root that GitHub's chain needs (Sectigo E46, for one):
    Windows adds roots on demand for the browser, but Python only sees those already installed.
    The file is read by this script alone: no store is changed, no admin rights, no prompt.
    """
    if "file" in WINDOWS_ROOTS:
        return WINDOWS_ROOTS["file"]
    WINDOWS_ROOTS["file"] = None
    folder = Path(tempfile.mkdtemp(prefix="lusine-roots-"))
    sst, pem = folder / "roots.sst", folder / "roots.pem"
    export = (
        "$all = New-Object Security.Cryptography.X509Certificates.X509Certificate2Collection; "
        f"$all.Import('{sst}'); "
        "$text = foreach ($c in $all) { '-----BEGIN CERTIFICATE-----'; "
        "[Convert]::ToBase64String($c.RawData, 'InsertLineBreaks'); '-----END CERTIFICATE-----' }; "
        f"Set-Content -Path '{pem}' -Value $text -Encoding ascii"
    )
    steps = (
        ["certutil", "-generateSSTFromWU", str(sst)],
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", export],
    )
    try:
        for command in steps:
            if subprocess.run(command, timeout=120, check=False, **SILENT).returncode:
                return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    if pem.is_file() and pem.stat().st_size:
        WINDOWS_ROOTS["file"] = pem
    return WINDOWS_ROOTS["file"]


@functools.cache
def ssl_context(extra_roots: Path | None = None) -> ssl.SSLContext:
    """Python's default certificates, plus the ones Windows trusts (and, if given, a .pem file).

    The Python that uv brings ignores the Windows certificate store. Behind a company proxy that
    re-signs HTTPS with its own root certificate, GitHub then fails with CERTIFICATE_VERIFY_FAILED
    although the browser works. Reading the store (ROOT and CA) fixes that without turning the
    verification off.
    """
    context = ssl.create_default_context()
    if sys.platform == "win32":
        for store in ("ROOT", "CA"):
            try:
                certificates = ssl.enum_certificates(store)
            except OSError:
                continue
            for der, kind, _trust in certificates:
                if kind == "x509_asn":
                    try:
                        context.load_verify_locations(cadata=der)
                    except ssl.SSLError:
                        pass  # an unusable certificate in the store: ignore it
    if extra_roots:
        context.load_verify_locations(cafile=str(extra_roots))
    return context


def _open(url: str, *, data: dict[str, str] | None = None, token: str | None = None):
    headers = {"Accept": "application/json", "User-Agent": "setup-lusine"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, body, headers)
    try:
        return urllib.request.urlopen(request, timeout=60, context=ssl_context())
    except urllib.error.URLError as exc:
        untrusted = isinstance(exc.reason, ssl.SSLCertVerificationError)
        roots = fetch_windows_roots() if untrusted and sys.platform == "win32" else None
        if roots is None:
            raise
    return urllib.request.urlopen(request, timeout=60, context=ssl_context(roots))


NETWORK_HELP = (
    "Check your connection. If it says CERTIFICATE_VERIFY_FAILED, a company proxy is "
    "re-signing HTTPS: ask IT for the root certificate and set SSL_CERT_FILE to its .pem file."
)


def _post_json(url: str, data: dict[str, str]) -> dict:
    try:
        with _open(url, data=data) as reply:
            return json.loads(reply.read().decode())
    except urllib.error.URLError as exc:
        raise SetupError(f"Could not reach GitHub ({exc.reason}). {NETWORK_HELP}") from exc


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
    say("L'Usine's files are private, so GitHub must know who you are. It takes a minute.")
    print()
    box([f"Your code:   {reply['user_code']}"], color="yellow", indent=6)
    print()
    if interactive:
        copied = copy_to_clipboard(reply["user_code"])
        say("GitHub opens in your browser" + (", the code is copied: paste it." if copied else "."))
        webbrowser.open(uri)
    say(f"Not opening? Go to {paint(uri, 'cyan')} and type the code.")
    say('Then click "Continue" and "Authorize". Nothing to copy back.')
    print()
    interval = int(reply.get("interval", 5))
    deadline = time.monotonic() + int(reply.get("expires_in", 900))
    failures = 0
    with Task("Waiting for you to authorize on GitHub") as task:
        while time.monotonic() < deadline:
            left = int(deadline - time.monotonic())
            task.detail = f"{left // 60}:{left % 60:02d} left"
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
                task.label = "Authorized on GitHub"
                task.detail = ""
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
        good("Signed in to GitHub")
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
    good("Signed in to GitHub")
    return token


# --- download --------------------------------------------------------------------------------


def parse_sha(body: str) -> str:
    """The commit sha from the API reply: the JSON commit (what Accept: application/json gets)
    or a bare sha. It goes into a URL, so anything else is refused."""
    body = body.strip()
    if body.startswith("{"):
        body = str(json.loads(body).get("sha", ""))
    if len(body) != 40 or any(c not in "0123456789abcdef" for c in body):
        raise SetupError("GitHub did not return a commit id. Run this again.")
    return body


def download(target: Path, token: str) -> str:
    """Unpack the latest files of the repository into ``target`` (new or empty). Returns the sha."""
    archive = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        with Task("Downloading L'Usine") as task, archive:
            with _open(f"{API}/repos/{REPO}/commits/HEAD", token=token) as reply:
                sha = parse_sha(reply.read().decode())
            with _open(f"{API}/repos/{REPO}/tarball/{sha}", token=token) as reply:
                total = int(reply.headers.get("Content-Length") or 0) or None
                done = 0
                while chunk := reply.read(256 * 1024):
                    archive.write(chunk)
                    done += len(chunk)
                    task.progress(done, total)
        with Task("Unpacking the files"):
            extract(Path(archive.name), target)
    except urllib.error.URLError as exc:
        raise SetupError(f"Could not download L'Usine ({exc}). Run this again.") from exc
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
    command = [uv_binary(), "run", "--no-project", "--python", ">=3.11,<3.13", "python", "-m"]
    labels = {"adopt": "Marking the copy", "update": "Updating the files"}
    label = labels.get(args[0], "Saving your sign-in")
    if run_quiet([*command, "utils.installer", *args], label, env=env, cwd=target):
        raise SetupError("Could not finish unpacking L'Usine. Run this again.")


def warm_up_python() -> None:
    """While the person signs in, let uv fetch a Python if there is none: the wait costs nothing."""

    def work() -> None:
        try:
            uv = uv_binary()
            spec = ">=3.11,<3.13"
            if subprocess.run([uv, "python", "find", spec], **SILENT).returncode:
                subprocess.run([uv, "python", "install", default_python()], **SILENT)
        except (SetupError, OSError):
            pass

    threading.Thread(target=work, daemon=True).start()


def windows_on_arm() -> bool:
    return sys.platform == "win32" and platform.machine().upper() in ("ARM64", "AARCH64")


def default_python() -> str:
    """The Python L'Usine gets by default: 3.11, the lowest one the project accepts.

    On Windows for ARM it is the x64 build (see ``sync_command``).
    """
    return "cpython-3.11-windows-x86_64-none" if windows_on_arm() else "3.11"


def sync_command() -> list[str]:
    """``uv sync``, with the x64 Python on Windows for ARM.

    Libraries such as httptools (needed by Streamlit) have no prebuilt Windows ARM64 package, so
    uv would compile them and ask for the Microsoft C++ build tools. Windows 11 on ARM runs x64
    programs, and every library has a prebuilt x64 package.
    """
    command = [uv_binary(), "sync"]
    if windows_on_arm():
        command += ["--python", default_python()]
    return command


def prepare(target: Path) -> None:
    """Install the libraries now, behind a progress line, so that the first start is instant."""
    if run_quiet(sync_command(), "Installing the libraries", cwd=target):
        raise SetupError(
            "The libraries could not be installed (the lines above say why). Run this again;\n"
            "if it keeps failing, send those lines to the DSP team."
        )


def visitor(target: Path, *, interactive: bool, dry_run: bool) -> None:
    step(2, "Sign in to GitHub")
    if dry_run:
        say("(dry run) sign in, then download the latest files of " + REPO)
        return
    token = get_token(interactive=interactive)
    step(3, "Download L'Usine")
    if (target / ".lusine-snapshot.json").is_file():
        installer(target, "update", str(target), token=token)
    elif target.exists() and any(target.iterdir()):
        raise SetupError(f"The folder {target} exists and was not made by L'Usine. Use --dir.")
    else:
        sha = download(target, token)
        installer(target, "adopt", str(target), REPO, sha, token=token)
    installer(target, "save-token", token=token)


TOOL_DIRS = {
    "win32": [
        os.path.join(os.environ.get(var, ""), *rest)
        for var, rest in (
            ("ProgramFiles", ("GitHub CLI",)),
            ("ProgramFiles", ("Git", "cmd")),
            ("LOCALAPPDATA", ("Programs", "GitHub CLI")),
            ("LOCALAPPDATA", ("Programs", "Git", "cmd")),
        )
        if os.environ.get(var)
    ],
    "darwin": ["/opt/homebrew/bin", "/usr/local/bin"],
}
INSTALLERS = {
    "win32": ("winget", {"git": "Git.Git", "gh": "GitHub.cli"}),
    "darwin": ("brew", {"git": "git", "gh": "gh"}),
}


def find_tool(name: str) -> str | None:
    """Where ``name`` is. Looks past PATH: right after winget or brew, a window opened earlier
    does not see the new tool yet, and that is the 'gh is not installed' people reported."""
    found = shutil.which(name)
    if found:
        return found
    for folder in TOOL_DIRS.get(sys.platform, []):
        for file in (Path(folder) / name, Path(folder) / f"{name}.exe"):
            if file.is_file() and os.access(file, os.X_OK):
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
                return str(file)
    return None


def have(name: str) -> bool:
    return find_tool(name) is not None


def install_tools(missing: list[str]) -> None:
    """Install what is missing with the system's package manager, after a yes."""
    manager, ids = INSTALLERS.get(sys.platform, ("", {}))
    if not manager or not shutil.which(manager):
        return
    if not ask_yes(f"Install {' and '.join(missing)} now with {manager}?"):
        return
    for name in missing:
        if manager == "winget":
            args = ["install", "--id", ids[name], "-e", "--silent"]
            args += ["--accept-package-agreements", "--accept-source-agreements"]
        else:
            args = ["install", ids[name]]
        run_quiet([manager, *args], f"Installing {name}")


def developer(target: Path, *, interactive: bool, dry_run: bool) -> None:
    step(2, "Check git and the GitHub tool (gh)")
    missing = [name for name in ("git", "gh") if not have(name)]
    if missing and interactive and not dry_run:
        warn(f"Not found: {', '.join(missing)}")
        install_tools(missing)
        missing = [name for name in missing if not have(name)]
    if missing:
        hint = {"darwin": "brew install git gh", "win32": "winget install Git.Git GitHub.cli"}
        how = hint.get(sys.platform, "your package manager")
        if interactive and not dry_run and ask_yes("Continue as a visitor instead (no gh needed)?"):
            return visitor(target, interactive=interactive, dry_run=dry_run)
        raise SetupError(
            f"Missing: {', '.join(missing)}. Install with:  {how}\n"
            "then run this again, or add --visitor (nothing to install)."
        )
    good("git and gh are ready")
    step(3, "Sign in to GitHub")
    login = ["gh", "auth", "login", "--web", "--git-protocol", "https"]
    clone = ["gh", "repo", "clone", REPO, str(target)]
    pull = ["git", "-C", str(target), "pull", "--ff-only"]
    if subprocess.run(["gh", "auth", "status"], **SILENT).returncode:
        if dry_run:
            say(f"(dry run) not signed in; would propose:  {' '.join(login)}")
            say("(dry run) stops here: step 4 needs a signed-in gh.")
            return
        why = "A browser window opens: confirm the code GitHub shows there."
        if propose(login, why, interactive=interactive) and run(login):
            raise SetupError("The GitHub sign-in did not work.")
        if subprocess.run(["gh", "auth", "status"], **SILENT).returncode:
            raise SetupError("Not signed in to GitHub yet. Run the command above, then this again.")
    good("Signed in to GitHub")
    if not dry_run:
        run_quiet(["gh", "auth", "setup-git"], "Connecting git to GitHub")
    if run_quiet(["gh", "api", f"repos/{REPO}", "--jq", ".full_name"], "Checking your access"):
        raise SetupError(f"GitHub does not let you open {REPO}.\n{ACCESS_HELP}")
    if dry_run:
        step(4, "Download L'Usine")
        say(f"(dry run) would propose:  {' '.join(pull if (target / '.git').is_dir() else clone)}")
        return
    step(4, "Download L'Usine")
    if (target / ".git").is_dir():
        why = "L'Usine is already here: this brings it up to date."
        if propose(pull, why, interactive=interactive):
            run_quiet(pull, "Updating L'Usine")
    elif target.exists() and any(target.iterdir()):
        raise SetupError(f"The folder {target} exists and is not a clone. Use --dir.")
    else:
        why = "This copies the private repository (a minute the first time)."
        if propose(clone, why, interactive=interactive) and run_quiet(clone, "Cloning L'Usine"):
            raise SetupError("Could not clone L'Usine.")
        if not (target / ".git").is_dir():
            raise SetupError("The clone is not there yet. Run the command above, then this again.")


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
        f"\n      {paint('1', 'bold', 'cyan')}   {paint('Visitor', 'bold')}  "
        f"{paint('(quickest)', 'green')}\n"
        "          I just want to see it. Nothing to install but uv;\n"
        "          you sign in to GitHub once.\n\n"
        f"      {paint('2', 'bold', 'cyan')}   {paint('Developer', 'bold')}\n"
        "          I need to clone and have everything. Needs git and the GitHub tool (gh);\n"
        "          missing ones can be installed for you.\n"
    )
    choice = input(f"      Your choice, 1 or 2 [{default}]: ").strip() or default
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
    os.environ.pop("VIRTUAL_ENV", None)  # the one of this script, not of L'Usine
    if sys.platform == "win32":  # uv too must trust the certificates Windows trusts
        os.environ.setdefault("UV_NATIVE_TLS", "1")
        os.environ.setdefault("UV_SYSTEM_CERTS", "1")
    try:
        banner()
        step(1, "Who are you?")
        profile = choose_profile(args.profile, interactive=interactive)
        STEPS["total"] = 5 if profile == "developer" else 4
        target = args.dir.expanduser().resolve() / "lusine"
        good(f"{profile.capitalize()}: L'Usine will live in {paint(str(target), 'cyan')}")
        if not args.dry_run:
            warm_up_python()
        started = time.monotonic()
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
        step(STEPS["total"], "Start L'Usine")
        prepare(target)
        print()
        box([f"Ready in {time.monotonic() - started:.0f}s", f"Folder: {target}"], color="green")
        if args.no_launch:
            say(f"Start it later with:  cd {target}  then  uv run lusine")
            return 0
        print()
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
