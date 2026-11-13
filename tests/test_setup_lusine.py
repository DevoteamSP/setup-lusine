"""The public setup script: sign-in, access check, download and the two profiles."""

from __future__ import annotations

import io
import os
import shlex
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

import setup_lusine as setup
from tests.conftest import REPO
from tests.fake_github import FakeGitHub


@pytest.fixture
def fake(github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGitHub:
    github.device_interval = 0
    monkeypatch.setattr(setup, "API", github.url)
    monkeypatch.setattr(setup, "WEB", github.url)
    monkeypatch.setattr(setup, "REPO", REPO)
    monkeypatch.setattr(setup, "sleep", lambda _seconds: None)
    monkeypatch.setattr(setup, "TOKEN_FILE", tmp_path / "none" / "github_token.json")
    monkeypatch.setattr(setup, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(setup, "MODE_FILE", tmp_path / "home" / "install-mode")
    for name in ("LUSINE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return github


def test_code_signin_waits_until_the_code_is_approved(fake: FakeGitHub, capsys) -> None:
    fake.pending_polls = 2
    assert setup.code_signin(interactive=False) == fake.token
    assert "ABCD-1234" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "words"),
    [("access_denied", "cancelled"), ("expired_token", "expired")],
)
def test_code_signin_says_why_it_stopped(fake: FakeGitHub, error: str, words: str) -> None:
    fake.device_error = error
    with pytest.raises(setup.SetupError, match=words):
        setup.code_signin(interactive=False)


def test_a_saved_token_that_works_skips_the_sign_in(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUSINE_TOKEN", fake.token)
    assert setup.get_token(interactive=False) == fake.token
    assert not [call for call in fake.seen if call[0] == "POST"]


def test_sign_in_without_access_to_the_repository_names_the_cause(fake: FakeGitHub) -> None:
    fake.device_token = "someone-else"
    with pytest.raises(setup.SetupError, match="DevoteamSP organization"):
        setup.get_token(interactive=False)


def test_download_unpacks_the_files_without_their_top_folder(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    target = tmp_path / "lusine"
    assert setup.download(target, fake.token) == fake.sha
    assert (target / "README.md").read_bytes() == b"# Acme\n"
    assert (target / "doc" / "guide.md").is_file()


def test_download_of_a_branch_asks_for_that_branch(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.branches = ("feat/x",)
    assert setup.download(tmp_path / "lusine", fake.token, "feat/x") == fake.sha
    assert any(call[1].endswith("/commits/feat/x") for call in fake.seen)


def test_download_of_an_unknown_branch_says_so(fake: FakeGitHub, tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError, match="branch nope does not exist"):
        setup.download(tmp_path / "lusine", fake.token, "nope")


def _tar(tmp_path: Path, members: list[tuple[str, bytes]]) -> Path:
    path = tmp_path / "evil.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_extract_skips_members_that_leave_the_folder(tmp_path: Path) -> None:
    archive = _tar(tmp_path, [("top/ok.txt", b"ok"), ("top/../../escape.txt", b"no")])
    target = tmp_path / "out"
    setup.extract(archive, target)
    assert (target / "ok.txt").is_file()
    assert not (tmp_path / "escape.txt").exists()


def test_extract_reports_a_damaged_download(tmp_path: Path) -> None:
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"not a tarball")
    with pytest.raises(setup.SetupError, match="damaged"):
        setup.extract(broken, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_visitor_downloads_then_marks_the_copy(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LUSINE_TOKEN", fake.token)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(setup, "installer", lambda target, *a, token="": calls.append(a))
    target = tmp_path / "snow" / "lusine"
    setup.visitor(target, interactive=False, dry_run=False)
    assert (target / "README.md").is_file()
    assert [c[0] for c in calls] == ["adopt", "save-token"]


def test_visitor_refuses_a_folder_that_is_not_ours(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LUSINE_TOKEN", fake.token)
    target = tmp_path / "mine" / "lusine"
    target.mkdir(parents=True)
    (target / "mine.txt").write_text("keep")
    with pytest.raises(setup.SetupError, match="not made by L'Usine"):
        setup.visitor(target, interactive=False, dry_run=False)


def test_developer_without_git_or_gh_is_told_what_to_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup, "have", lambda _name: False)
    with pytest.raises(setup.SetupError, match="git, gh"):
        setup.developer(tmp_path / "lusine", interactive=False, dry_run=False)


def test_dry_run_changes_nothing(tmp_path: Path, capsys) -> None:
    code = setup.main(["--visitor", "--yes", "--dry-run", "--dir", str(tmp_path)])
    assert code == 0
    assert not (tmp_path / "lusine").exists()
    assert "dry run" in capsys.readouterr().out


def test_the_script_is_plain_ascii() -> None:
    assert Path(setup.__file__).read_bytes().isascii()


def test_parse_sha_reads_the_sha_from_a_json_commit() -> None:
    assert setup.parse_sha('{"sha": "' + "b" * 40 + '", "commit": {}}\n') == "b" * 40


def test_parse_sha_accepts_a_bare_sha() -> None:
    assert setup.parse_sha("c" * 40 + "\n") == "c" * 40


def test_parse_sha_refuses_anything_else() -> None:
    with pytest.raises(setup.SetupError):
        setup.parse_sha("<html>rate limited</html>")


def test_on_windows_the_certificates_of_the_system_store_are_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behind a company proxy the root certificate lives in the Windows store, not in Python."""
    import ssl

    loaded: list[bytes] = []
    stores: list[str] = []

    def enum(store: str) -> list[tuple[bytes, str, bool]]:
        stores.append(store)
        return [(b"der-" + store.encode(), "x509_asn", True), (b"crl", "pkcs_7_asn", True)]

    class Context:
        def load_verify_locations(self, *, cadata: bytes) -> None:
            loaded.append(cadata)

    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(ssl, "enum_certificates", enum, raising=False)
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    setup.ssl_context.cache_clear()
    try:
        setup.ssl_context()
    finally:
        setup.ssl_context.cache_clear()
    assert stores == ["ROOT", "CA"]
    assert loaded == [b"der-ROOT", b"der-CA"]


def _untrusted() -> urllib.error.URLError:
    import ssl

    return urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer"))


def test_on_windows_a_missing_root_is_fetched_once_and_the_request_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pem = tmp_path / "roots.pem"
    contexts: list[object] = []

    def urlopen(_request, timeout, context):
        contexts.append(context)
        if len(contexts) == 1:
            raise _untrusted()
        return "reply"

    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(setup, "fetch_windows_roots", lambda: pem)
    monkeypatch.setattr(setup, "ssl_context", lambda extra=None: ("context", extra))
    assert setup._open("https://api.github.com/user") == "reply"
    assert contexts == [("context", None), ("context", pem)]


def test_elsewhere_a_certificate_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def urlopen(_request, timeout, context):
        calls.append(1)
        raise _untrusted()

    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.URLError):
        setup._open("https://api.github.com/user")
    assert calls == [1]


def test_when_windows_gives_no_roots_the_original_error_comes_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(_request, timeout, context):
        raise _untrusted()

    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(setup, "fetch_windows_roots", lambda: None)
    with pytest.raises(urllib.error.URLError):
        setup._open("https://api.github.com/user")


@pytest.mark.parametrize(
    ("platform_name", "machine", "x64"),
    [
        ("win32", "ARM64", True),
        ("win32", "AMD64", False),
        ("darwin", "arm64", False),
        ("linux", "aarch64", False),
    ],
)
def test_only_windows_on_arm_syncs_with_the_x64_python(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, machine: str, x64: bool
) -> None:
    monkeypatch.setattr(setup.sys, "platform", platform_name)
    monkeypatch.setattr(setup.platform, "machine", lambda: machine)
    monkeypatch.setattr(setup, "uv_binary", lambda: "uv")
    command = setup.sync_command()
    assert command[:2] == ["uv", "sync"]
    assert ("--python" in command) is x64
    if x64:
        assert command[-1] == "cpython-3.11-windows-x86_64-none"


def test_a_failed_library_install_stops_the_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup, "run_quiet", lambda *a, **k: 1)
    monkeypatch.setattr(setup, "uv_binary", lambda: "uv")
    with pytest.raises(setup.SetupError, match="libraries could not be installed"):
        setup.prepare(tmp_path)


@pytest.fixture
def fancy_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "fancy", lambda: True)
    monkeypatch.setattr(setup, "width", lambda: 80)


def _visible(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def test_the_bar_fills_in_eighths_and_has_a_fixed_width(fancy_terminal: None) -> None:
    for fraction in (0, 0.01, 0.5, 0.999, 1):
        parts = setup.bar_parts(fraction, 20)
        assert sum(len(text) for text, _style in parts) == 20
    assert setup.bar_parts(0, 10)[0][0] == ""
    assert setup.bar_parts(1, 10)[0] == ("█" * 10, "green")
    assert setup.bar_parts(0.5, 10)[0] == ("█" * 5, "cyan")


def test_the_plain_bar_uses_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "fancy", lambda: False)
    assert setup.bar_parts(0.5, 10) == [("#####-----", "")]


def test_the_sweeping_light_stays_inside_the_bar(fancy_terminal: None) -> None:
    for tick in range(200):
        parts = setup.marquee_parts(tick, 30)
        assert sum(len(text) for text, _style in parts) == 30


def test_every_window_row_has_the_same_width(fancy_terminal: None) -> None:
    lines = setup.window("Title", [[("short", "")], [("x" * 500, "bold")], []])
    assert {len(_visible(line)) for line in lines} == {setup.box_width()}
    assert "…" in _visible(lines[2])  # the long row is cut with an ellipsis


def test_the_road_map_shows_done_current_and_next(fancy_terminal: None) -> None:
    setup.STEPS.update(total=3, names=["Who", "Sign in", "Start"])
    line = _visible(setup.stepper(2))
    assert line.index("✔") < line.index("●") < line.index("○")


def test_explain_in_plain_text_keeps_the_words(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(setup, "fancy", lambda: False)
    setup.explain("Step 2/4 - Sign in", ["Why it matters."], tip="A tip.")
    out = capsys.readouterr().out
    assert "Step 2/4 - Sign in" in out and "Why it matters." in out and "Tip: A tip." in out


def test_the_task_window_folds_into_one_line(
    fancy_terminal: None, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(setup.Task, "_spin", lambda self: self._stop.wait())
    with setup.Task("Doing it") as task:
        task.result = "3.0 MB"
    out = capsys.readouterr().out
    assert "✔" in out and "Doing it" in out and "3.0 MB" in out


def test_progress_tells_speed_and_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    task = setup.Task("Downloading")
    clock_values = iter([10.0, 11.0])
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(clock_values))
    task._mark = (9.0, 0)
    task.progress(2_000_000, 10_000_000)
    assert task.fraction == pytest.approx(0.2)
    assert "2.0 of 10.0 MB" in task.detail and "MB/s" in task.detail


def test_the_mac_shortcut_goes_to_the_folder_then_runs_lusine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    monkeypatch.setattr(setup, "uv_binary", lambda: "/Users/me/.local/bin/uv")
    monkeypatch.setenv("LUSINE_DESKTOP", str(tmp_path))
    target = Path("/Users/me/dev/snow builders/lusine")
    file = setup.create_shortcut(target)
    assert file == tmp_path / "Lusine.command"
    lines = file.read_text().splitlines()
    assert lines[0] == "#!/bin/bash"
    assert lines[1] == f"cd {shlex.quote(str(target))} || exit 1"
    assert lines[2] == "/Users/me/.local/bin/uv run lusine"
    if os.name != "nt":  # Windows files have no executable bit
        assert file.stat().st_mode & 0o111  # a double-click needs it executable


def test_the_windows_shortcut_is_a_cmd_file_with_crlf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup, "uv_binary", lambda: r"C:\Users\me\.local\bin\uv.exe")
    monkeypatch.setenv("LUSINE_DESKTOP", str(tmp_path))
    monkeypatch.setattr(setup, "HOME_DIR", tmp_path / "home")
    file = setup.create_shortcut(Path(r"C:\Users\me\dev\100%\lusine"))  # no logo here
    assert file.name == "Lusine.cmd"  # no icon possible: the plain launcher stays on the Desktop
    raw = file.read_bytes().decode()
    assert "\r\n" in raw and "\n" not in raw.replace("\r\n", "")
    assert r'cd /d "C:\Users\me\dev\100%%\lusine"' in raw
    assert r'"C:\Users\me\.local\bin\uv.exe" run lusine' in raw


def _fake_logos(folder: Path) -> Path:
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    logos = folder / setup.LOGO_DIR
    logos.mkdir(parents=True)
    for size in setup.ICO_SIZES:
        (logos / f"usine-logo-red-{size}.png").write_bytes(png)
    return logos


def test_the_ico_holds_every_logo_size(tmp_path: Path) -> None:
    import struct

    logos = _fake_logos(tmp_path)
    out = tmp_path / "lusine.ico"
    setup.build_ico(logos, out)
    raw = out.read_bytes()
    assert struct.unpack("<HHH", raw[:6]) == (0, 1, len(setup.ICO_SIZES))
    size, offset = struct.unpack("<II", raw[6 + 8 : 6 + 16])
    assert raw[offset : offset + 4] == b"\x89PNG" and size > 0


def test_the_windows_desktop_gets_a_lnk_with_the_logo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup, "uv_binary", lambda: "uv.exe")
    monkeypatch.setattr(setup, "HOME_DIR", tmp_path / "home")
    monkeypatch.setenv("LUSINE_DESKTOP", str(tmp_path / "Desktop"))
    target = tmp_path / "lusine"
    _fake_logos(target)
    made: list[tuple[Path, Path, Path]] = []
    monkeypatch.setattr(setup, "make_windows_link", lambda *args: made.append(args))
    file = setup.create_shortcut(target)
    assert file.name == "Lusine.lnk"
    link, launcher, icon = made[0]
    assert launcher == tmp_path / "home" / "Lusine.cmd" and launcher.is_file()
    assert icon.is_file() and icon.suffix == ".ico"


def test_the_mac_shortcut_gets_the_logo_but_works_without_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    monkeypatch.setattr(setup, "uv_binary", lambda: "uv")
    monkeypatch.setenv("LUSINE_DESKTOP", str(tmp_path / "Desktop"))
    seen: list[tuple[Path, Path]] = []
    monkeypatch.setattr(setup, "set_mac_icon", lambda f, png: seen.append((f, png)))
    file = setup.create_shortcut(tmp_path / "lusine")
    assert seen == [(file, tmp_path / "lusine" / setup.LOGO_DIR / "usine-logo-red-512.png")]

    def refused(_f: Path, _png: Path) -> None:
        raise OSError("no")

    monkeypatch.setattr(setup, "set_mac_icon", refused)
    assert setup.create_shortcut(tmp_path / "lusine").is_file()


def test_the_shortcut_is_offered_only_to_people_who_can_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    monkeypatch.setattr(setup, "uv_binary", lambda: "uv")
    monkeypatch.setenv("LUSINE_DESKTOP", str(tmp_path))
    asked: list[str] = []
    monkeypatch.setattr(setup, "ask_yes", lambda question, **_: asked.append(question) or False)
    setup.offer_shortcut(tmp_path, interactive=False, force=False, skip=False)
    setup.offer_shortcut(tmp_path, interactive=True, force=False, skip=True)
    assert asked == [] and not list(tmp_path.iterdir())
    setup.offer_shortcut(tmp_path, interactive=True, force=False, skip=False)
    assert len(asked) == 1 and not list(tmp_path.iterdir())  # they said no
    monkeypatch.setattr(setup, "ask_yes", lambda *_a, **_k: True)
    setup.offer_shortcut(tmp_path, interactive=True, force=False, skip=False)
    assert (tmp_path / "Lusine.command").is_file()


def test_no_shortcut_on_other_systems(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setenv("LUSINE_DESKTOP", str(tmp_path))
    setup.offer_shortcut(tmp_path, interactive=True, force=True, skip=False)
    assert not list(tmp_path.iterdir())


def test_a_shortcut_that_cannot_be_written_does_not_stop_the_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "darwin")

    def broken(_target: Path) -> Path:
        raise OSError("read-only")

    monkeypatch.setattr(setup, "create_shortcut", broken)
    setup.offer_shortcut(tmp_path, interactive=False, force=True, skip=False)
    assert "No shortcut this time" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="runs the macOS launcher with bash")
def test_the_mac_launcher_really_starts_uv_in_the_lusine_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    fake_uv = tmp_path / "fake uv"
    fake_uv.write_text('#!/bin/sh\necho "uv $* in $(pwd -P)"\n')
    fake_uv.chmod(0o755)
    target = tmp_path / "snow builders" / "lusine"
    target.mkdir(parents=True)
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    monkeypatch.setattr(setup, "uv_binary", lambda: str(fake_uv))
    monkeypatch.setenv("LUSINE_DESKTOP", str(tmp_path / "Desktop"))
    launcher = setup.create_shortcut(target)
    done = subprocess.run([str(launcher)], capture_output=True, text=True, check=False)
    assert done.returncode == 0
    assert done.stdout.strip() == f"uv run lusine in {target.resolve()}"


def test_the_browser_opens_only_after_the_person_pressed_enter(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    events: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": events.append("enter") or "")
    monkeypatch.setattr(setup.webbrowser, "open", lambda url: events.append("browser"))
    monkeypatch.setattr(setup, "copy_to_clipboard", lambda text: events.append("copy") or True)
    assert setup.code_signin(interactive=True) == fake.token
    assert events == ["copy", "enter", "copy", "browser"]  # copied, asked, copied again, opened
    out = capsys.readouterr().out
    assert "ABCD-1234" in out and "Press Enter" in out and "already copied" in out


def test_without_a_keyboard_nothing_waits_and_no_browser_opens(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a, **_k):
        raise AssertionError("must not be called")

    monkeypatch.setattr("builtins.input", refuse)
    monkeypatch.setattr(setup.webbrowser, "open", refuse)
    assert setup.code_signin(interactive=False) == fake.token
