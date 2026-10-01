"""
SSRF-safe outbound HTTP for tenant-supplied URLs (S-3.3, S-3.1 JWKS).

Why not just ``urlopen`` after an ``is_safe_url`` check?
  * the check and the connection resolved DNS separately (rebinding window);
  * ``urlopen`` silently FOLLOWS REDIRECTS, so a public URL could bounce the
    request to ``http://169.254.169.254/`` or an internal host.

Here the hostname is resolved ONCE, every address is validated as public, and
the socket connects to that exact validated IP (TLS still verifies the original
hostname via SNI / certificate). Redirects are never followed: any 3xx is
reported as a failure.
"""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit


class UnsafeURL(Exception):
    """The URL is not an allowed public http(s) endpoint."""


def ip_is_public(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    bad = (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
           or addr.is_multicast or addr.is_unspecified)
    if bad:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None and ip_is_public(str(mapped)) is False:
        return False
    # 6to4 / Teredo wrappers around private space
    sixtofour = getattr(addr, "sixtofour", None)
    if sixtofour is not None and ip_is_public(str(sixtofour)) is False:
        return False
    teredo = getattr(addr, "teredo", None)
    if teredo is not None and (ip_is_public(str(teredo[0])) is False or ip_is_public(str(teredo[1])) is False):
        return False
    return True


def resolve_public(url, require_https=False):
    """Validate ``url`` and return ``(scheme, host, port, path, ip)``.
    Raises UnsafeURL unless EVERY resolved address is public."""
    parts = urlsplit(url or "")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or (require_https and scheme != "https"):
        raise UnsafeURL("scheme not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("missing host")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in URL not allowed")
    port = parts.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeURL("cannot resolve host") from exc
    ips = []
    for info in infos:
        ip = info[4][0]
        if not ip_is_public(ip):
            raise UnsafeURL("host resolves to a non-public address")
        ips.append(ip)
    if not ips:
        raise UnsafeURL("no addresses")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path, ips[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def request(method, url, body=None, headers=None, timeout=10, max_bytes=1_000_000, require_https=False):
    """One request, pinned to a validated IP, no redirects.
    Returns ``(status, response_bytes)``. Raises UnsafeURL / OSError."""
    scheme, host, port, path, ip = resolve_public(url, require_https=require_https)
    conn_cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
    conn = conn_cls(host, port, ip, timeout)
    try:
        hdrs = {"Host": host if port in (80, 443) else "%s:%d" % (host, port),
                "User-Agent": "Futurematch/1.0"}
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read(max_bytes)
        return resp.status, data
    finally:
        try:
            conn.close()
        except Exception:
            pass


def post_json(url, body, headers=None, timeout=10):
    """POST ``body`` (bytes). Returns ``(ok, detail)``; 3xx is a failure."""
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    status, _ = request("POST", url, body=body, headers=hdrs, timeout=timeout)
    if 300 <= status < 400:
        return False, "redirect refused (HTTP %d)" % status
    if 200 <= status < 300:
        return True, "HTTP %d" % status
    return False, "HTTP %d" % status
