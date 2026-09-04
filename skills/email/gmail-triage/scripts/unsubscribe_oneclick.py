#!/usr/bin/env python3
"""Submit one RFC 8058 one-click unsubscribe POST, defensively.

The target URL comes from an untrusted email header, so this helper treats it as
hostile input: HTTPS only, public destinations only, no redirects followed, DNS
answers pinned before connecting, bounded time and response size, and one
explicitly selected sender per run. The URL is never printed or logged; only its
host and a short digest appear in the output.

    python3 unsubscribe_oneclick.py --account you@example.com \
        --address news@example.com --selected-action unsubscribe_only \
        --evidence-message-id 19b11732c1b578fd --url-file /tmp/target.url --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

UNSUBSCRIBE_ACTIONS = {
    "unsubscribe_only",
    "unsubscribe_and_archive_label",
    "unsubscribe_and_trash",
}

BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".intranet", ".home.arpa")
MAX_URL_LENGTH = 2048
MAX_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 1_048_576
BODY = "List-Unsubscribe=One-Click"


class RejectedTarget(ValueError):
    """Raised when the advertised target fails a safety check."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def validate_action(value: str) -> str:
    action = (value or "").strip().lower()
    if action not in UNSUBSCRIBE_ACTIONS:
        raise RejectedTarget(
            "no unsubscribe was selected for this sender; --selected-action must be "
            "one of " + ", ".join(sorted(UNSUBSCRIBE_ACTIONS))
        )
    return action


def validate_address(value: str) -> str:
    address = (value or "").strip().lower().strip("<>")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address) or any(c in address for c in "*?,; "):
        raise RejectedTarget("--address must be one exact sender address")
    return address


def parse_target(url: str) -> tuple[str, str, int, str]:
    """Return (hostname, path_with_query, port, normalized_url)."""
    raw = (url or "").strip()
    if not raw:
        raise RejectedTarget("no unsubscribe URL was supplied")
    if len(raw) > MAX_URL_LENGTH:
        raise RejectedTarget("unsubscribe URL is longer than the allowed 2048 characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise RejectedTarget("unsubscribe URL contains control characters")
    if not raw.isascii():
        raise RejectedTarget("unsubscribe URL must be ASCII; convert IDN hosts to punycode first")
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise RejectedTarget(f"only https targets are allowed, got {parts.scheme.lower() or 'none'!r}")
    if parts.username or parts.password or "@" in parts.netloc:
        raise RejectedTarget("unsubscribe URL must not embed credentials")
    hostname = (parts.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise RejectedTarget("unsubscribe URL has no host")
    try:
        port = parts.port or 443
    except ValueError as error:
        raise RejectedTarget("unsubscribe URL has an invalid port") from error
    if port != 443:
        raise RejectedTarget(f"only port 443 is allowed, got {port}")
    if hostname == "localhost" or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        raise RejectedTarget(f"host {hostname} is a local or internal name")
    if "." not in hostname and not is_ip(hostname):
        raise RejectedTarget(f"host {hostname} is not a public fully qualified name")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return hostname, path, port, raw


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return True


def public_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise RejectedTarget(f"unusable address {value!r}") from error
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return public_address(str(address.ipv4_mapped))
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise RejectedTarget(f"target resolves to a non-public address ({address})")
    return address


def default_resolver(hostname: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise RejectedTarget(f"cannot resolve {hostname}: {error.strerror or error}") from error
    return [info[4][0] for info in infos]


def pin_destination(
    hostname: str,
    port: int,
    resolver: Callable[[str, int], list[str]] = default_resolver,
) -> tuple[str, list[str]]:
    """Resolve once and reject unless every answer is a public address.

    Pinning one checked address closes the DNS rebinding window: the socket
    connects to an address that was validated, not to whatever the name resolves
    to at connect time.
    """
    if is_ip(hostname):
        return str(public_address(hostname.strip("[]"))), [hostname]
    answers = resolver(hostname, port)
    if not answers:
        raise RejectedTarget(f"{hostname} has no address records")
    checked = [str(public_address(answer)) for answer in answers]
    return checked[0], checked


def http_transport(
    hostname: str,
    ip: str,
    port: int,
    path: str,
    timeout: float,
    max_bytes: int,
) -> dict[str, Any]:
    """POST to the pinned address with SNI and Host set to the original name."""
    context = ssl.create_default_context()
    raw_socket = socket.create_connection((ip, port), timeout=timeout)
    try:
        secure = context.wrap_socket(raw_socket, server_hostname=hostname)
    except BaseException:
        raw_socket.close()
        raise
    connection = http.client.HTTPConnection(hostname, port, timeout=timeout)
    connection.sock = secure
    try:
        connection.request(
            "POST",
            path,
            body=BODY,
            headers={
                "Host": hostname,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(BODY)),
                "User-Agent": "gmail-triage-unsubscribe/1.0 (+RFC8058)",
                "Accept": "*/*",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        payload = response.read(max_bytes + 1)
        location = response.getheader("Location") or ""
        return {
            "status": response.status,
            "bytes_read": min(len(payload), max_bytes),
            "response_truncated": len(payload) > max_bytes,
            "location_host": (urlsplit(location).hostname or "relative") if location else "",
        }
    finally:
        connection.close()


def submit(
    url: str,
    address: str,
    selected_action: str,
    evidence_message_id: str,
    timeout: float = 10.0,
    max_bytes: int = 65_536,
    dry_run: bool = False,
    resolver: Callable[[str, int], list[str]] = default_resolver,
    transport: Callable[..., dict[str, Any]] = http_transport,
) -> dict[str, Any]:
    action = validate_action(selected_action)
    sender = validate_address(address)
    if not str(evidence_message_id or "").strip():
        raise RejectedTarget(
            "--evidence-message-id is required so the URL and the "
            "List-Unsubscribe-Post header come from the same message"
        )
    if not 0 < timeout <= MAX_TIMEOUT:
        raise RejectedTarget(f"timeout must be between 0 and {MAX_TIMEOUT:.0f} seconds")
    if not 0 < max_bytes <= MAX_RESPONSE_BYTES:
        raise RejectedTarget(f"max-bytes must be between 1 and {MAX_RESPONSE_BYTES}")

    hostname, path, port, normalized = parse_target(url)
    pinned, checked = pin_destination(hostname, port, resolver=resolver)
    base = {
        "address": sender,
        "selected_action": action,
        "evidence_message_id": str(evidence_message_id).strip(),
        "target_host": hostname,
        "target_url_digest": digest(normalized),
        "pinned_address": pinned,
        "resolved_addresses": checked,
        "redirects_followed": False,
    }
    if dry_run:
        return {**base, "outcome": "validated_not_sent", "detail": "dry run; no request was sent"}

    try:
        response = transport(hostname, pinned, port, path, timeout, max_bytes)
    except RejectedTarget:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return {
            **base,
            "outcome": "failed",
            "detail": f"{type(error).__name__} while contacting {hostname}",
        }

    status = int(response.get("status", 0))
    result = {
        **base,
        "http_status": status,
        "response_bytes": response.get("bytes_read", 0),
        "response_truncated": bool(response.get("response_truncated")),
    }
    if 200 <= status < 300:
        return {**result, "outcome": "submitted", "detail": "endpoint accepted the one-click request"}
    if 300 <= status < 400:
        return {
            **result,
            "outcome": "review_required",
            "detail": "endpoint answered with a redirect; redirects are not followed",
            "redirect_host": response.get("location_host", ""),
        }
    if status in {401, 403, 405, 429}:
        return {
            **result,
            "outcome": "review_required",
            "detail": f"endpoint returned {status}; it needs a human or a later retry",
        }
    return {**result, "outcome": "failed", "detail": f"endpoint returned {status}"}


def read_url(args: argparse.Namespace) -> str:
    if args.url and args.url_file:
        raise RejectedTarget("pass either --url or --url-file, not both")
    if args.url_file:
        if str(args.url_file) == "-":
            return sys.stdin.read().strip()
        try:
            return Path(args.url_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RejectedTarget(f"cannot read the URL file: {error}") from error
    if args.url:
        return args.url
    raise RejectedTarget("supply the target with --url, --url-file, or --url-file -")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--account", required=True, help="exact connected Gmail address")
    parser.add_argument("--address", required=True, help="exact sender the user selected")
    parser.add_argument("--selected-action", required=True, help="the action the user selected")
    parser.add_argument(
        "--evidence-message-id", required=True,
        help="message that supplied both the URL and the List-Unsubscribe-Post header",
    )
    parser.add_argument("--url", help="target URL (prefer --url-file to keep it out of shell history)")
    parser.add_argument("--url-file", help="file containing the target URL, or - for stdin")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-bytes", type=int, default=65_536)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate the target and stop without sending a request",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        account = validate_address(args.account)
        result = submit(
            read_url(args),
            args.address,
            args.selected_action,
            args.evidence_message_id,
            timeout=args.timeout,
            max_bytes=args.max_bytes,
            dry_run=args.dry_run,
        )
    except RejectedTarget as error:
        sys.stdout.write(json.dumps({"outcome": "rejected", "reason": str(error)}, indent=2) + "\n")
        return 2
    result["account"] = account
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0 if result["outcome"] in {"submitted", "validated_not_sent"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
