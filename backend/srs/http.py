"""The ONLY HTTP client for SRS services. GET-only, host-allowlisted. Spec §0.

- Exactly one public method: `get`. There is no post/put/patch/delete to call.
- `_GuardTransport` refuses, at the transport layer, any non-GET request and any host outside
  the two pinned origins, and never follows redirects. Tokens cannot go anywhere else even if
  some caller bypasses `get` and hands the transport a request directly.
- Origins are constants (backend/constants.py). No parameter, setting or env var changes them.
- Exceptions raised from here never contain the token.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Literal

import httpx

from backend import constants

Service = Literal["wanikani", "bunpro"]

ALLOWED_ORIGINS: dict[Service, str] = {
    "wanikani": constants.WANIKANI_ORIGIN,
    "bunpro": constants.BUNPRO_ORIGIN,
}
ALLOWED_HOSTS: frozenset[str] = frozenset(httpx.URL(o).host for o in ALLOWED_ORIGINS.values())
_MIN_INTERVAL: dict[Service, float] = {
    "wanikani": constants.WANIKANI_MIN_INTERVAL_S,
    "bunpro": constants.BUNPRO_MIN_INTERVAL_S,
}
_TIMEOUT_S = 10.0


class ReadOnlyViolation(RuntimeError):
    """A non-GET request reached the SRS transport. This must never happen."""


class HostViolation(RuntimeError):
    """A request to a host outside the pinned origins reached the SRS transport."""


class SrsError(RuntimeError):
    """Sanitised transport/HTTP error (never contains the token)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _GuardTransport(httpx.BaseTransport):
    """Wraps any transport; enforces GET-only + host allowlist before anything is sent."""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() != "GET":
            raise ReadOnlyViolation(f"refused {request.method} {request.url.host}{request.url.path}")
        if request.url.host not in ALLOWED_HOSTS:
            raise HostViolation(f"refused request to host {request.url.host!r}")
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class SrsClient:
    """GET-only client for one SRS service. The single public method is `get`."""

    def __init__(
        self,
        service: Service,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        if service not in ALLOWED_ORIGINS:
            raise ValueError(f"unknown service {service!r}")
        self._service: Service = service
        self._token = token
        self._origin = ALLOWED_ORIGINS[service]
        self._min_interval = _MIN_INTERVAL[service]
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._lock = threading.Lock()
        headers = {"Accept": "application/json", "User-Agent": "atama-ai/0.0 (read-only)"}
        if service == "wanikani":
            headers["Authorization"] = f"Bearer {token}"
            headers["Wanikani-Revision"] = constants.WANIKANI_REVISION
        else:
            headers["Authorization"] = f"Token token={token}"
            headers["Origin"] = constants.BUNPRO_SITE_ORIGIN
            headers["Referer"] = constants.BUNPRO_SITE_ORIGIN + "/"
        self._base_params: dict[str, str] = (
            {constants.BUNPRO_TOKEN_OPT_IN_PARAM: "true"} if service == "bunpro" else {}
        )
        self._http = httpx.Client(
            base_url=self._origin,
            headers=headers,
            timeout=_TIMEOUT_S,
            follow_redirects=False,
            transport=_GuardTransport(transport or httpx.HTTPTransport()),
        )

    # ------------------------------------------------------------------ public
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET `path` (relative to the pinned origin) and return parsed JSON."""
        if not path.startswith("/"):
            raise ValueError("path must be origin-relative and start with '/'")
        self._throttle()
        merged = {**self._base_params, **(params or {})}
        try:
            resp = self._http.get(path, params=merged or None)
        except (ReadOnlyViolation, HostViolation):
            raise
        except httpx.HTTPError as e:
            raise SrsError(self._sanitize(f"{self._service}: {type(e).__name__}: {e}")) from None
        if resp.status_code in (301, 302, 303, 307, 308):
            raise SrsError(f"{self._service}: redirect not followed ({resp.status_code})", resp.status_code)
        if resp.status_code >= 400:
            raise SrsError(
                self._sanitize(f"{self._service}: HTTP {resp.status_code} for {path}: {resp.text[:200]}"),
                resp.status_code,
            )
        try:
            return resp.json()
        except ValueError:
            raise SrsError(f"{self._service}: non-JSON response for {path}", resp.status_code) from None

    # ----------------------------------------------------------------- private
    def _throttle(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last_request_at is not None:
                wait = self._min_interval - (now - self._last_request_at)
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._last_request_at = now

    def _sanitize(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def __del__(self):  # pragma: no cover
        try:
            self._http.close()
        except Exception:
            pass


def _self_check() -> None:
    """Import-time assertion (spec §0): the client exposes exactly one public method."""
    public = sorted(n for n, v in vars(SrsClient).items() if not n.startswith("_") and callable(v))
    if public != ["get"]:
        raise ImportError(f"SrsClient must expose exactly ['get'], found {public} — Golden Rule (spec §0)")


_self_check()
