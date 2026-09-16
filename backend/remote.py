"""The phone page (ADR-041): a second listener on this machine's own LAN address, HTTPS with a
self-signed certificate, admitting a socket only with the key the QR code carries.

Off by default. Everything else keeps ADR-017: VOICEVOX, SearXNG and the loopback page never
leave 127.0.0.1. This listener is one deliberate exception, on one address of this machine that
must be on a **private network** (RFC 1918 or link-local — never a wildcard, never loopback,
never a public address), and `backend/app.py` refuses a non-loopback socket unless it comes from
a private address, carries this listener's own `Origin`, and presents the **session key** in
`?k=` (constant-time). The key is made anew at every launch (user, 2026-09-16) and lives only in
this process: it is never written to settings.json, and the QR code carrying it is sent only to
pages on loopback (`Hub.welcome`), so it is never shown on a phone. "New key" replaces it within
the run.

Why HTTPS at all on a home network: a phone browser grants `getUserMedia` and the AudioWorklet
only in a secure context, and a LAN address over plain HTTP is not one. The certificate is
self-signed and generated once into the per-user state directory; the phone accepts it once
(Android: proceed past the warning; iPhone: install and trust the profile). Its fingerprint is
shown beside the QR so it can be compared.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import re
import secrets
import socket
from pathlib import Path
from typing import Any

from backend import config

#: How long the certificate is valid. Under Apple's 825-day ceiling for a leaf a user trusts.
CERT_DAYS = 800
CERT_NAME = "atama-ai.local"
#: The QR's error correction: M survives a smudged phone screen; the URL is short enough for it.
QR_ERROR = "m"


def lan_address() -> str | None:
    """This machine's address on its default route, without sending anything: a UDP connect only
    picks the interface (TEST-NET-1, RFC 5737, is never routed). None when there is no route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))
            address = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


def bad_host(host: str) -> str:
    """Why `host` may not serve the phone page: "" when it may."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return f"REMOTE_HOST must be an IP address of this machine, not {host!r}"
    if ip.is_unspecified:
        return "REMOTE_HOST may not be a wildcard (0.0.0.0 / ::): one address, never every interface (ADR-017)"
    if ip.is_loopback:
        return "REMOTE_HOST is loopback, which the phone cannot reach; leave it empty for the LAN address"
    if not ip.is_private:
        return (f"REMOTE_HOST {host} is not a private-network address (10.x, 172.16-31.x, 192.168.x): "
                "the phone page is for your own network only")
    return ""


def is_private_client(host: str | None) -> bool:
    """A client the phone listener may talk to: on a private network, and not loopback (that is
    the other listener's business)."""
    try:
        ip = ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


def new_key() -> str:
    return secrets.token_urlsafe(18)


def page_url(host: str, port: int, key: str) -> str:
    return f"https://{host}:{port}/?k={key}"


def qr_svg(url: str) -> str:
    """The URL as an inline SVG (segno 1.6.6: `make(...).svg_inline`, verified 2026-09-16)."""
    import segno
    # Solid white inside the SVG itself and the full four-module quiet zone the QR spec asks for:
    # over the frosted panel a transparent code was hard for a phone camera (user, 2026-09-16).
    svg = segno.make(url, error=QR_ERROR).svg_inline(scale=6, border=4, dark="#000000", light="#ffffff")
    # segno sets width/height but no viewBox, so a CSS size left the code pinned top-left of a
    # bigger box (user, 2026-09-16). With a viewBox it scales to whatever box the page gives it.
    m = re.match(r'<svg width="(\d+)" height="(\d+)"', svg)
    if m:
        w, h = m.groups()
        svg = svg.replace(m.group(0), f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}"', 1)
    return svg


def certificate(folder: Path, host: str) -> tuple[Path, Path, str]:
    """A self-signed certificate for `host` in `folder`, made once and reused while it names the
    address and has a year left. Returns (cert.pem, key.pem, SHA-256 fingerprint)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    address = ipaddress.ip_address(host)
    now = dt.datetime.now(dt.timezone.utc)
    if cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if address in san.get_values_for_type(x509.IPAddress) \
                    and cert.not_valid_after_utc > now + dt.timedelta(days=365):
                return cert_path, key_path, _fingerprint(cert)
        except Exception:  # noqa: BLE001 - unreadable: make a new one
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "atama-AI")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=CERT_DAYS))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address), x509.DNSName(CERT_NAME)]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass                                     # Windows: ACLs, not modes
    return cert_path, key_path, _fingerprint(cert)


def _fingerprint(cert) -> str:
    from cryptography.hazmat.primitives import hashes
    raw = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(raw[i:i + 2] for i in range(0, len(raw), 2))


def state_dir(cfg) -> Path:
    """Beside the claude cwd, in the per-user state directory — never in the repo."""
    return config.claude_cwd(cfg).parent / "remote"


class RemoteServer:
    """The listener, started and stopped to match the settings. Never raises out of `apply`."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        #: The session key: new at every launch, held here and nowhere else (user, 2026-09-16).
        self.key = new_key()
        self.host = ""
        self.port = 0
        self.url = ""
        self.fingerprint = ""
        self.error = ""

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def info(self, cfg) -> dict[str, Any]:
        """The `remote` message for a page on loopback."""
        enabled = bool(cfg.REMOTE_ENABLED)
        if not enabled:
            detail = "off - the page is served on this computer only"
        elif self.error:
            detail = self.error
        elif self.running:
            detail = f"serving https://{self.host}:{self.port} - scan the code on your phone"
        else:
            detail = "starting..."
        url = self.url if self.running and not self.error else ""
        return {"enabled": enabled, "url": url, "qr_svg": qr_svg(url) if url else "",
                "fingerprint": self.fingerprint if url else "", "detail": detail}

    async def apply(self, hub, cfg) -> None:
        """Start, move or stop the listener so it matches `cfg`."""
        want = bool(cfg.REMOTE_ENABLED)
        host = str(cfg.REMOTE_HOST or "").strip() or (lan_address() or "")
        port = int(cfg.REMOTE_PORT)
        if self.running and (not want or (host, port) != (self.host, self.port)):
            await self.stop(hub)
        if not want or self.running:
            return
        self.error = ""
        if not host:
            self.error = "no network address found - is this computer on a network?"
            return
        if why := bad_host(host):
            self.error = why
            return
        try:
            cert, keyfile, self.fingerprint = await asyncio.to_thread(certificate, state_dir(cfg), host)
            await self._serve(hub, cfg, host, port, cert, keyfile)
        except Exception as exc:  # noqa: BLE001 - said on the panel and the console, never fatal
            self.error = f"could not start: {type(exc).__name__}: {exc}"[:300]
            self.task = None
            return
        self.host, self.port = host, port
        self.url = page_url(host, port, self.key)
        hub.remote_key = self.key
        hub.remote_origin = f"https://{host}:{port}"

    async def _serve(self, hub, cfg, host: str, port: int, cert: Path, keyfile: Path) -> None:
        import uvicorn
        from backend import app as web
        sock = web.listen(host, port)
        server = uvicorn.Server(uvicorn.Config(
            web.build(hub, int(cfg.PORT)), host=host, port=port, log_level="warning",
            ws="websockets-sansio", timeout_graceful_shutdown=2,
            ssl_certfile=str(cert), ssl_keyfile=str(keyfile)))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        task.server = server  # type: ignore[attr-defined]
        for _ in range(200):
            if task.done() or getattr(server, "started", False):
                break
            await asyncio.sleep(0.05)
        if task.done() or not getattr(server, "started", False):
            await web.shutdown(task)
            exc = task.exception() if task.done() and not task.cancelled() else None
            raise RuntimeError(f"the phone page did not start on https://{host}:{port}"
                               + (f": {type(exc).__name__}: {exc}" if exc else ""))
        self.task = task

    def rotate(self) -> str:
        """A new session key for the rest of this run. Phones must scan again."""
        self.key = new_key()
        return self.key

    async def stop(self, hub) -> None:
        hub.remote_key = None
        hub.remote_origin = None
        if self.task is not None:
            from backend import app as web
            await web.shutdown(self.task)
            self.task = None
        self.url = ""
