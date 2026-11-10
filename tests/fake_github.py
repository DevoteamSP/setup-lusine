"""A small stand-in for github.com and api.github.com, served from 127.0.0.1 for the tests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class FakeGitHub:
    """What the fake serves, and what it saw."""

    token: str = "good"
    login: str = "laurent"
    #: ``owner/repo`` -> files of the default branch.
    repos: dict[str, dict[str, bytes]] = field(default_factory=dict)
    sha: str = "a" * 40
    #: How many "not yet" answers the device flow gives before approving.
    pending_polls: int = 1
    #: Seconds GitHub asks to wait between two questions (the shell installer really sleeps).
    device_interval: int = 5
    #: The token the device flow hands out, when it is not ``token`` (an account without access).
    device_token: str | None = None
    #: A final answer instead of approving (``access_denied``, ``expired_token``).
    device_error: str = ""
    #: An error GitHub gives instead of a code (``device_flow_disabled``).
    code_error: str = ""
    #: What the account may do in a repository, and what the token lets it do.
    can_push: bool = True
    can_write: bool = True
    #: Extra tar members as ``(name, content)``, to test hostile downloads.
    extra_members: list[tuple[str, bytes]] = field(default_factory=list)
    seen: list[tuple[str, str, str]] = field(default_factory=list)
    url: str = ""

    def tarball(self, repo: str) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            top = f"{repo.replace('/', '-')}-{self.sha[:7]}"
            for name, content in {**self.repos[repo]}.items():
                info = tarfile.TarInfo(f"{top}/{name}")
                info.size = len(content)
                info.mtime = 1_700_000_000
                tar.addfile(info, io.BytesIO(content))
            for name, content in self.extra_members:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        return buffer.getvalue()


def _handler(state: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # silence the test output
            pass

        def _send(self, code: int, body: bytes = b"", kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode())

        def _authorized(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {state.token}"

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            state.seen.append(("GET", path, self.headers.get("Authorization", "")))
            parts = path.strip("/").split("/")
            if parts[0] == "codeload":
                repo = "/".join(parts[1:3])
                return self._send(200, state.tarball(repo), "application/gzip")
            if not self._authorized():
                return self._json(401, {"message": "Bad credentials"})
            if path == "/user":
                return self._json(200, {"login": state.login})
            if parts[0] != "repos" or "/".join(parts[1:3]) not in state.repos:
                return self._json(404, {"message": "Not Found"})
            repo = "/".join(parts[1:3])
            if len(parts) == 3:
                return self._json(
                    200, {"full_name": repo, "permissions": {"push": state.can_push}}
                )
            if parts[3:] == ["commits", "HEAD"]:
                if "json" in self.headers.get("Accept", ""):  # what the real API sends back
                    return self._json(200, {"sha": state.sha, "commit": {"message": "{x}"}})
                return self._send(200, state.sha.encode(), "text/plain")
            if parts[3] == "contents":
                rel = "/".join(parts[4:])
                files = state.repos[repo]
                if "raw" in self.headers.get("Accept", ""):
                    if rel not in files:
                        return self._json(404, {"message": "Not Found"})
                    return self._send(200, files[rel], "text/plain")
                if rel in files:
                    return self._json(
                        200,
                        {
                            "type": "file",
                            "sha": hashlib.sha1(files[rel]).hexdigest(),  # noqa: S324
                            "content": base64.b64encode(files[rel]).decode(),
                        },
                    )
                children = sorted(
                    name.removeprefix(rel + "/")
                    for name in files
                    if name.startswith(rel + "/") and "/" not in name.removeprefix(rel + "/")
                )
                if not children:
                    return self._json(404, {"message": "Not Found"})
                return self._send(
                    200,
                    json.dumps([{"type": "file", "name": name} for name in children]).encode(),
                )
            if parts[3] == "tarball":
                self.send_response(302)
                self.send_header("Location", f"/codeload/{repo}/{parts[4]}")
                self.end_headers()
                return None
            return self._json(404, {"message": "Not Found"})

        def do_PUT(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode() or "{}")
            state.seen.append(("PUT", path, self.headers.get("Authorization", "")))
            if not self._authorized():
                return self._json(401, {"message": "Bad credentials"})
            parts = path.strip("/").split("/")
            repo = "/".join(parts[1:3])
            if parts[0] != "repos" or repo not in state.repos or parts[3:4] != ["contents"]:
                return self._json(404, {"message": "Not Found"})
            if not state.can_write:
                return self._json(
                    403, {"message": "Resource not accessible by personal access token"}
                )
            rel = "/".join(parts[4:])
            files = state.repos[repo]
            if rel in files and body.get("sha") != hashlib.sha1(files[rel]).hexdigest():  # noqa: S324
                return self._json(409, {"message": "does not match"})
            files[rel] = base64.b64decode(body["content"])
            return self._json(200, {"content": {"path": rel}})

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            state.seen.append(("POST", path, ",".join(sorted(form))))
            if path == "/login/device/code":
                if state.code_error:
                    why = f"{state.code_error} (test)"
                    return self._json(
                        200, {"error": state.code_error, "error_description": why}
                    )
                return self._json(
                    200,
                    {
                        "device_code": "dev123",
                        "user_code": "ABCD-1234",
                        "verification_uri": f"{state.url}/login/device",
                        "expires_in": 900,
                        "interval": state.device_interval,
                    },
                )
            if path == "/login/oauth/access_token":
                if state.pending_polls > 0:
                    state.pending_polls -= 1
                    return self._json(200, {"error": "authorization_pending"})
                if state.device_error:
                    return self._json(200, {"error": state.device_error})
                handed = state.device_token or state.token
                return self._json(200, {"access_token": handed, "token_type": "bearer"})
            return self._json(404, {"message": "Not Found"})

    return Handler


def serve(state: FakeGitHub) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, state.url
