import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, Mock, patch

from zotero_agent.net import AgentError, HTTP, HttpError


URL = "https://api.openai.com/v1/responses"


def failure(code=None, kind=None, status=429, headers=None, raw=None):
    body = raw if raw is not None else json.dumps({"error": {
        "code": code, "type": kind, "message": "private response text must not be printed"}}).encode()
    return urllib.error.HTTPError(URL, status, "error", headers or {}, io.BytesIO(body))


def success():
    response = MagicMock()
    response.__enter__.return_value = response
    response.status, response.headers = 200, {}
    response.read.return_value = b'{"status":"completed"}'
    return response


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.client = HTTP()
        self.client.opener = Mock()
        self.clock = 100.0
        self.monotonic = patch("zotero_agent.net.time.monotonic", side_effect=lambda: self.clock)
        self.sleep = patch("zotero_agent.net.time.sleep", side_effect=self.advance)
        self.monotonic.start()
        self.sleep_mock = self.sleep.start()
        self.jitter = patch("zotero_agent.net.random.uniform", return_value=.25)
        self.jitter.start()
        self.addCleanup(self.monotonic.stop)
        self.addCleanup(self.sleep.stop)
        self.addCleanup(self.jitter.stop)

    def advance(self, delay):
        self.clock += delay

    def test_quota_and_spending_errors_never_retry(self):
        for code in ("insufficient_quota", "credit_balance_exhausted", "project_spend_limit_exceeded",
                     "organization_spend_limit_exceeded", "organization_usage_limit_exceeded"):
            with self.subTest(code=code):
                self.client.opener.open.reset_mock()
                self.client.blocked_until.clear()
                self.client.opener.open.side_effect = failure(code, "insufficient_quota")
                with self.assertRaises(HttpError) as raised:
                    self.client.request("POST", URL, retry_429=True)
                self.assertEqual(self.client.opener.open.call_count, 1)
                self.assertIn(code, str(raised.exception))
                self.assertTrue(raised.exception.is_quota)
                self.assertNotIn("private response", str(raised.exception))

    def test_quota_type_with_unknown_code_is_still_not_retried(self):
        self.client.opener.open.side_effect = failure("new_billing_code", "insufficient_quota")
        with self.assertRaisesRegex(HttpError, "credit balance"):
            self.client.request("POST", URL, retry_429=True)
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_rate_limit_retries_after_header_delay(self):
        self.client.opener.open.side_effect = [failure("rate_limit_exceeded", headers={"Retry-After": "12"}), success()]
        self.assertEqual(self.client.request("POST", URL, retry_429=True).status, 200)
        self.assertEqual(self.client.opener.open.call_count, 2)
        self.assertGreaterEqual(self.sleep_mock.call_args.args[0], 12)

    def test_rate_limit_stops_after_three_retries(self):
        self.client.opener.open.side_effect = [failure("slow_down", "rate_limit_error") for _ in range(4)]
        with self.assertRaisesRegex(HttpError, "slow_down"):
            self.client.request("POST", URL, retry_429=True)
        self.assertEqual(self.client.opener.open.call_count, 4)
        delays = [c.args[0] for c in self.sleep_mock.call_args_list]
        self.assertEqual(len(delays), 3)
        self.assertLess(delays[0], delays[1])
        self.assertLess(delays[1], delays[2])

    def test_unrecognised_or_invalid_response_does_not_leak_or_retry(self):
        for body in (b"not json private text", b'[]', b'{"error":{"code":"secret-key","message":"secret-key"}}'):
            with self.subTest(body=body):
                self.client.opener.open.reset_mock()
                self.client.opener.open.side_effect = failure(raw=body)
                with self.assertRaisesRegex(HttpError, "recognised cause") as raised:
                    self.client.request("POST", URL, retry_429=True)
                self.assertNotIn("secret-key", str(raised.exception))
                self.assertNotIn("private text", str(raised.exception))
                self.assertEqual(self.client.opener.open.call_count, 1)

    def test_connection_failure_does_not_replay_model_post(self):
        self.client.opener.open.side_effect = urllib.error.URLError("connection lost")
        with self.assertRaises(AgentError):
            self.client.request("POST", URL, retry_429=True)
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_server_errors_do_not_replay_model_post(self):
        self.client.opener.open.side_effect = failure(status=503)
        with self.assertRaises(HttpError):
            self.client.request("POST", URL, retry_429=True)
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_zotero_mutation_is_not_retried(self):
        self.client.opener.open.side_effect = failure("rate_limit_exceeded")
        with self.assertRaises(HttpError):
            self.client.request("POST", "https://api.zotero.org/users/123/items", retry_429=True)
        self.assertEqual(self.client.opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
