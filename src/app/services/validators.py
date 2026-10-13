import ipaddress
import socket
from urllib.parse import urlsplit

from app.models.schemas import ErrorCode

ALLOWED_PORTS = {None, 80, 443}


class ValidationError(Exception):
    """Raised for bad input. The route turns it into a 4xx error response."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Unwrap ::ffff:127.0.0.1 style addresses so they can't sneak past.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> None:
    """Allow only public http(s) URLs. Raises ValidationError otherwise."""
    parts = urlsplit(url)

    if parts.scheme not in ("http", "https"):
        raise ValidationError(ErrorCode.INVALID_URL, "Only http and https URLs are allowed.")

    host = parts.hostname
    if not host:
        raise ValidationError(ErrorCode.INVALID_URL, "URL has no host.")

    if parts.username or parts.password:
        raise ValidationError(ErrorCode.INVALID_URL, "URLs with credentials are not allowed.")

    try:
        port = parts.port
    except ValueError:
        raise ValidationError(ErrorCode.INVALID_URL, "Invalid port.") from None
    if port not in ALLOWED_PORTS:
        raise ValidationError(ErrorCode.INVALID_URL, "Only ports 80 and 443 are allowed.")

    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValidationError(ErrorCode.INVALID_URL, "Could not resolve the host.") from None

    # Check every address the name resolves to, not just the first.
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public(ip):
            raise ValidationError(ErrorCode.INVALID_URL, "That address is not allowed.")
