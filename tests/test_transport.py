import io
import json
import unittest
import urllib.error

from notify_me.errors import NotifyMeError
from notify_me.transport import BarkEndpoint, BarkTransport


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        return self._body[:limit]

    def close(self):
        return None


class _Opener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class BarkTransportContractTests(unittest.TestCase):
    def endpoint(self):
        return BarkEndpoint.parse(
            "http://127.0.0.1:8080/Abcdef12_key/example/title"
        )

    def test_endpoint_accepts_https_and_loopback_http_but_rejects_unsafe_forms(self):
        https = BarkEndpoint.parse("HTTPS://Bark.Example/Abcdef12_key/title?body=x")
        self.assertEqual(https.server, "https://bark.example")
        self.assertEqual(https.push_url, "https://bark.example/push")
        self.assertEqual(https.key, "Abcdef12_key")
        self.assertEqual(
            BarkEndpoint.parse("https://bark.example/Abcdef12_key/Title%20with%20space").key,
            "Abcdef12_key",
        )
        self.assertEqual(self.endpoint().server, "http://127.0.0.1:8080")
        for value in (
            "http://bark.example/Abcdef12_key",
            "https://user:pass@bark.example/Abcdef12_key",
            "https://bark.example/Abcdef12_key#fragment",
            "https://bark.example/key",
            "https://bark.example/your-device-key",
            "https://bark.example/Abc%2Fdef12_key",
        ):
            with self.subTest(value=value):
                with self.assertRaises(NotifyMeError):
                    BarkEndpoint.parse(value)

    def test_transport_posts_v2_json_to_push_without_putting_key_in_url(self):
        endpoint = self.endpoint()
        opener = _Opener(_Response(200, b'{"code":200}'))
        payload = {"device_key": endpoint.key, "title": "test"}

        result = BarkTransport(timeout=1, opener=opener).send(endpoint, payload)

        self.assertTrue(result.accepted)
        self.assertEqual(result.category, "accepted")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8080/push")
        self.assertEqual(request.method, "POST")
        self.assertNotIn(endpoint.key, request.full_url)
        self.assertEqual(json.loads(request.data)["device_key"], endpoint.key)
        self.assertEqual(timeout, 1)

    def test_redirect_is_rejected_without_a_second_request(self):
        endpoint = self.endpoint()
        request = None
        opener = _Opener(
            error=urllib.error.HTTPError(
                endpoint.push_url,
                302,
                "redirect",
                {},
                io.BytesIO(b"redirect"),
            )
        )

        result = BarkTransport(timeout=1, opener=opener).send(
            endpoint, {"device_key": endpoint.key, "title": "test"}
        )

        self.assertFalse(result.accepted)
        self.assertFalse(result.retryable)
        self.assertEqual(result.category, "redirect_rejected")
        self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(request)

    def test_http_and_bark_failures_are_classified_without_response_body(self):
        endpoint = self.endpoint()
        retryable = BarkTransport(
            timeout=1,
            opener=_Opener(_Response(503, b"server detail contains no secret")),
        ).send(endpoint, {"device_key": endpoint.key, "title": "test"})
        self.assertTrue(retryable.retryable)
        self.assertEqual(retryable.category, "retryable_http")

        permanent = BarkTransport(
            timeout=1,
            opener=_Opener(_Response(400, b"permanent detail")),
        ).send(endpoint, {"device_key": endpoint.key, "title": "test"})
        self.assertFalse(permanent.retryable)
        self.assertEqual(permanent.category, "permanent_http")

        invalid_json = BarkTransport(
            timeout=1,
            opener=_Opener(_Response(200, b"not-json")),
        ).send(endpoint, {"device_key": endpoint.key, "title": "test"})
        self.assertTrue(invalid_json.retryable)
        self.assertEqual(invalid_json.category, "invalid_response")

        bark_rejected = BarkTransport(
            timeout=1,
            opener=_Opener(_Response(200, b'{"code":400,"message":"bad"}')),
        ).send(endpoint, {"device_key": endpoint.key, "title": "test"})
        self.assertFalse(bark_rejected.retryable)
        self.assertEqual(bark_rejected.category, "bark_rejected")
