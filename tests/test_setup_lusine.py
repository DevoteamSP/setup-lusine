"""The public setup script: sign-in, access check, download and the two profiles."""

from __future__ import annotations

import io
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
        assert command[-1] == "cpython-3.12-windows-x86_64-none"


def test_a_failed_library_install_stops_the_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup, "run_quiet", lambda *a, **k: 1)
    monkeypatch.setattr(setup, "uv_binary", lambda: "uv")
    with pytest.raises(setup.SetupError, match="libraries could not be installed"):
        setup.prepare(tmp_path)
