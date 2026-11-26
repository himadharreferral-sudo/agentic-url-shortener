from urllib.parse import urlparse
import hashlib
import ipaddress
import re

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"}
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def validate_long_url(url: str) -> tuple[bool, str]:
    if not url or not isinstance(url, str):
        return False, "long_url must be a non-empty string"
    if len(url) > 2048:
        return False, "long_url exceeds maximum allowed length of 2048 characters"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return False, "long_url cannot contain control characters"
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return False, f"Unsupported URL scheme: {parsed.scheme}"
        if not parsed.netloc:
            return False, "URL must include a valid host"
        if parsed.username is not None or parsed.password is not None:
            return False, "URLs containing embedded credentials are not allowed"
        parsed.port
        host = parsed.hostname.lower().rstrip(".") if parsed.hostname else ""
    except ValueError:
        return False, "URL contains an invalid host or port"

    if not host:
        return False, "URL must include a valid host"
    if host in BLOCKED_HOSTS or host.endswith((".internal", ".local", ".localhost", ".localdomain")):
        return False, f"Target host is restricted by SSRF/open-redirect guardrails: {host}"

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*", host):
            return False, "Non-canonical numeric IP host is not allowed"
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return False, "URL contains an invalid hostname"
        labels = ascii_host.split(".")
        if len(ascii_host) > 253 or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            return False, "URL contains an invalid hostname"
    else:
        if not address.is_global:
            return False, f"Target IP address is not public: {host}"
    return True, "valid"


def validate_custom_code(code: str) -> tuple[bool, str]:
    if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
        return False, "Custom code must be 3-32 alphanumeric, hyphen, or underscore characters"
    return True, "valid"


def hash_client_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:16]
