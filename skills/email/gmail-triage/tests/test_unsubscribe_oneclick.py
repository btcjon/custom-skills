"""Security tests for the one-click unsubscribe helper.

No test issues a live unsubscribe request: every send path uses a stub transport
and a stub resolver, and the only real network primitive exercised is rejection.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from helpers import unsubscribe_oneclick as helper

URL = "https://list.example.com/u?id=abc123"
ARGS = {
    "address": "news@example.com",
    "selected_action": "unsubscribe_only",
    "evidence_message_id": "19b11732c1b578fd",
}


def resolver_for(*addresses):
    def resolve(hostname, port):
        assert hostname and port == 443
        return list(addresses)
    return resolve


def transport_for(status, location="", body_size=10):
    calls = []

    def transport(hostname, ip, port, path, timeout, max_bytes):
        calls.append({
            "hostname": hostname, "ip": ip, "port": port, "path": path,
            "timeout": timeout, "max_bytes": max_bytes,
        })
        return {
            "status": status,
            "bytes_read": min(body_size, max_bytes),
            "response_truncated": body_size > max_bytes,
            "location_host": location,
        }

    transport.calls = calls
    return transport


def submit(url=URL, **overrides):
    kwargs = {
        **ARGS,
        "resolver": resolver_for("93.184.216.34"),
        "transport": transport_for(200),
        **overrides,
    }
    return helper.submit(url, **kwargs)


class TargetValidationTests(unittest.TestCase):
    def reject(self, url):
        with self.assertRaises(helper.RejectedTarget) as caught:
            submit(url)
        return str(caught.exception)

    def test_non_https_schemes_are_rejected(self):
        for url in ("http://list.example.com/u", "ftp://list.example.com/u",
                    "file:///etc/passwd", "gopher://list.example.com/u", "//list.example.com/u"):
            self.assertIn("https", self.reject(url))

    def test_credentials_in_the_url_are_rejected(self):
        self.assertIn("credentials", self.reject("https://user:pass@list.example.com/u"))

    def test_alternate_ports_are_rejected(self):
        self.assertIn("443", self.reject("https://list.example.com:8443/u"))
        self.assertIn("443", self.reject("https://list.example.com:22/u"))

    def test_local_and_internal_names_are_rejected(self):
        for host in ("localhost", "printer.local", "wiki.internal", "db.intranet", "gw.home.arpa"):
            self.assertIn("local", self.reject(f"https://{host}/u").lower())

    def test_bare_hostnames_are_rejected(self):
        self.assertIn("fully qualified", self.reject("https://intranet/u"))

    def test_control_characters_and_non_ascii_are_rejected(self):
        self.assertIn("control characters", self.reject("https://list.example.com/u\r\nX-Injected: 1"))
        self.assertIn("ASCII", self.reject("https://exámple.com/u"))

    def test_oversized_url_is_rejected(self):
        self.assertIn("2048", self.reject("https://list.example.com/u?x=" + "a" * 2100))

    def test_empty_target_is_rejected(self):
        self.assertIn("no unsubscribe URL", self.reject("   "))


class SsrfTests(unittest.TestCase):
    def reject(self, url, **overrides):
        with self.assertRaises(helper.RejectedTarget) as caught:
            submit(url, **overrides)
        return str(caught.exception)

    def test_private_and_reserved_ip_literals_are_rejected(self):
        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.4.4", "169.254.169.254",
                     "100.64.0.1", "0.0.0.0", "224.0.0.1", "[::1]", "[fd00::1]", "[fe80::1]"):
            self.assertIn("non-public", self.reject(f"https://{host}/u"))

    def test_hostname_resolving_to_a_private_address_is_rejected(self):
        message = self.reject(URL, resolver=resolver_for("169.254.169.254"))
        self.assertIn("non-public", message)

    def test_mixed_public_and_private_answers_are_rejected(self):
        message = self.reject(URL, resolver=resolver_for("93.184.216.34", "127.0.0.1"))
        self.assertIn("non-public", message)

    def test_ipv4_mapped_ipv6_loopback_is_rejected(self):
        self.assertIn("non-public", self.reject(URL, resolver=resolver_for("::ffff:127.0.0.1")))

    def test_no_dns_answer_is_rejected(self):
        self.assertIn("no address records", self.reject(URL, resolver=resolver_for()))

    def test_connection_uses_the_pinned_address_not_a_second_lookup(self):
        """A second DNS answer cannot change the destination after validation."""
        answers = ["93.184.216.34"]

        def rebinding_resolver(hostname, port):
            current = list(answers)
            answers[:] = ["127.0.0.1"]
            return current

        transport = transport_for(200)
        result = submit(resolver=rebinding_resolver, transport=transport)
        self.assertEqual(result["pinned_address"], "93.184.216.34")
        self.assertEqual(transport.calls[0]["ip"], "93.184.216.34")
        self.assertEqual(transport.calls[0]["hostname"], "list.example.com")
        self.assertEqual(answers, ["127.0.0.1"])

    def test_public_target_is_allowed(self):
        result = submit()
        self.assertEqual(result["outcome"], "submitted")
        self.assertEqual(result["target_host"], "list.example.com")


class ConfusedHostTests(unittest.TestCase):
    """Host-confusion tricks must not smuggle a private destination through."""

    def resolved_host(self, url, answers=("93.184.216.34",)):
        result = submit(url, resolver=resolver_for(*answers))
        return result["target_host"]

    def test_fragment_and_query_tricks_resolve_to_the_real_host(self):
        for url in (
            "https://evil.example.com#@good.example.com/u",
            "https://evil.example.com/u?next=https://good.example.com",
        ):
            self.assertEqual(self.resolved_host(url), "evil.example.com", url)

    def test_backslash_in_the_authority_is_rejected(self):
        with self.assertRaises(helper.RejectedTarget):
            submit("https://evil.example.com\\@good.example.com/u")

    def test_second_at_sign_is_rejected(self):
        with self.assertRaises(helper.RejectedTarget):
            submit("https://user@evil.example.com@good.example.com/u")

    def test_numeric_host_shorthands_are_rejected(self):
        for url in ("https://2130706433/u", "https://0x7f000001/u"):
            with self.assertRaises(helper.RejectedTarget) as caught:
                submit(url)
            self.assertIn("fully qualified", str(caught.exception))

    def test_short_dotted_form_of_loopback_is_rejected_after_resolution(self):
        with self.assertRaises(helper.RejectedTarget) as caught:
            submit("https://127.1/u", resolver=resolver_for("127.0.0.1"))
        self.assertIn("non-public", str(caught.exception))

    def test_metadata_service_in_ipv6_form_is_rejected(self):
        with self.assertRaises(helper.RejectedTarget):
            submit("https://[::ffff:169.254.169.254]/u")

    def test_trailing_dot_host_is_normalized(self):
        self.assertEqual(self.resolved_host("https://list.example.com./u"), "list.example.com")


class SelectionRequirementTests(unittest.TestCase):
    def test_missing_or_wrong_selected_action_is_refused(self):
        for action in ("", "archive_label", "trash", "triage", "unsubscribe"):
            with self.assertRaises(helper.RejectedTarget) as caught:
                submit(selected_action=action)
            self.assertIn("selected", str(caught.exception))

    def test_all_unsubscribe_actions_are_accepted(self):
        for action in sorted(helper.UNSUBSCRIBE_ACTIONS):
            self.assertEqual(submit(selected_action=action)["selected_action"], action)

    def test_evidence_message_id_is_required(self):
        with self.assertRaises(helper.RejectedTarget) as caught:
            submit(evidence_message_id="  ")
        self.assertIn("same message", str(caught.exception))

    def test_sender_must_be_one_exact_address(self):
        for address in ("", "not-an-address", "*@example.com", "a@example.com,b@example.com"):
            with self.assertRaises(helper.RejectedTarget):
                submit(address=address)

    def test_bounds_are_enforced(self):
        for kwargs in ({"timeout": 0}, {"timeout": 120}, {"max_bytes": 0}, {"max_bytes": 5_000_000}):
            with self.assertRaises(helper.RejectedTarget):
                submit(**kwargs)


class OutcomeTests(unittest.TestCase):
    def test_dry_run_validates_without_sending(self):
        transport = transport_for(200)
        result = submit(dry_run=True, transport=transport)
        self.assertEqual(result["outcome"], "validated_not_sent")
        self.assertEqual(transport.calls, [])

    def test_redirect_is_not_followed(self):
        result = submit(transport=transport_for(302, location="tracker.example.net"))
        self.assertEqual(result["outcome"], "review_required")
        self.assertFalse(result["redirects_followed"])
        self.assertEqual(result["redirect_host"], "tracker.example.net")

    def test_auth_and_rate_limit_need_review(self):
        for status in (401, 403, 405, 429):
            result = submit(transport=transport_for(status))
            self.assertEqual(result["outcome"], "review_required")
            self.assertIn(str(status), result["detail"])

    def test_server_error_is_a_failure(self):
        result = submit(transport=transport_for(500))
        self.assertEqual(result["outcome"], "failed")

    def test_network_error_is_reported_without_leaking_the_url(self):
        def broken(*_args, **_kwargs):
            raise OSError("connection reset")

        result = submit(transport=broken)
        self.assertEqual(result["outcome"], "failed")
        self.assertNotIn("abc123", json.dumps(result))

    def test_large_response_is_bounded_and_flagged(self):
        result = submit(transport=transport_for(200, body_size=200_000), max_bytes=1024)
        self.assertEqual(result["response_bytes"], 1024)
        self.assertTrue(result["response_truncated"])

    def test_request_body_follows_rfc8058(self):
        self.assertEqual(helper.BODY, "List-Unsubscribe=One-Click")

    def test_url_never_appears_in_the_result(self):
        result = submit()
        serialized = json.dumps(result)
        self.assertNotIn("abc123", serialized)
        self.assertNotIn(URL, serialized)
        self.assertEqual(result["target_url_digest"], helper.digest(URL))


class TransportTests(unittest.TestCase):
    """The transport must require real TLS and never fall back to plaintext."""

    def test_plaintext_endpoint_is_refused_by_tls_verification(self):
        import http.server
        import ssl
        import threading

        class QuietHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            with self.assertRaises(ssl.SSLError):
                helper.http_transport(
                    "list.example.com", "127.0.0.1", server.server_port, "/u", 5.0, 1024
                )
        finally:
            server.server_close()
            thread.join(timeout=5)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = helper.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_dry_run_cli_validates_a_public_target(self):
        code, payload = self.run_cli([
            "--account", "you@example.com", "--address", "news@example.com",
            "--selected-action", "unsubscribe_only",
            "--evidence-message-id", "19b11732c1b578fd",
            "--url", "https://93.184.216.34/unsubscribe", "--dry-run",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "validated_not_sent")
        self.assertEqual(payload["account"], "you@example.com")

    def test_cli_rejects_a_loopback_target_without_network_access(self):
        code, payload = self.run_cli([
            "--account", "you@example.com", "--address", "news@example.com",
            "--selected-action", "unsubscribe_only",
            "--evidence-message-id", "19b11732c1b578fd",
            "--url", "https://127.0.0.1/unsubscribe", "--dry-run",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(payload["outcome"], "rejected")
        self.assertIn("non-public", payload["reason"])

    def test_cli_reads_the_url_from_a_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "target.url"
            path.write_text("https://93.184.216.34/unsubscribe\n", encoding="utf-8")
            code, payload = self.run_cli([
                "--account", "you@example.com", "--address", "news@example.com",
                "--selected-action", "unsubscribe_only",
                "--evidence-message-id", "19b11732c1b578fd",
                "--url-file", str(path), "--dry-run",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["target_host"], "93.184.216.34")

    def test_cli_refuses_both_url_forms_at_once(self):
        code, payload = self.run_cli([
            "--account", "you@example.com", "--address", "news@example.com",
            "--selected-action", "unsubscribe_only",
            "--evidence-message-id", "19b11732c1b578fd",
            "--url", "https://list.example.com/u", "--url-file", "/tmp/x", "--dry-run",
        ])
        self.assertEqual(code, 2)
        self.assertIn("not both", payload["reason"])


if __name__ == "__main__":
    unittest.main()
