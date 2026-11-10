import asyncio
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

try:
    from fineapi_core.client import NewAPIClient, NewAPIError
except ModuleNotFoundError:  # aiohttp 未安装时跳过
    NewAPIClient = None
    NewAPIError = None


class FakeResponse:
    def __init__(self, status, body, content_type="application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


@unittest.skipUnless(NewAPIClient, "aiohttp is not installed")
class ClientTest(unittest.IsolatedAsyncioTestCase):
    def make_client(self, responses):
        client = NewAPIClient("https://xingya.site", "pat", timeout_seconds=3)
        client._session = FakeSession(responses)
        return client

    async def test_get_user(self):
        client = self.make_client(
            [FakeResponse(200, '{"success":true,"data":{"id":123,"username":"alice"}}')]
        )
        user = await client.get_user(123)
        self.assertEqual(user["username"], "alice")

    async def test_success_false(self):
        client = self.make_client(
            [FakeResponse(200, '{"success":false,"message":"User does not exist"}')]
        )
        with self.assertRaises(NewAPIError) as ctx:
            await client.get_user(123)
        self.assertIn("User does not exist", ctx.exception.message)

    async def test_unauthorized(self):
        client = self.make_client(
            [FakeResponse(401, '{"success":false,"message":"unauthorized"}')]
        )
        with self.assertRaises(NewAPIError) as ctx:
            await client.get_user(123)
        self.assertIn("访问令牌", ctx.exception.message)

    async def test_non_json(self):
        client = self.make_client(
            [FakeResponse(403, "<html>blocked</html>", "text/html")]
        )
        with self.assertRaises(NewAPIError) as ctx:
            await client.get_user(123)
        self.assertIn("非 JSON", ctx.exception.message)

    async def test_add_quota_payload(self):
        session = FakeSession([FakeResponse(200, '{"success":true,"message":""}')])
        client = NewAPIClient("https://xingya.site", "pat")
        client._session = session
        await client.add_quota(123, 500000)

        method, url, kwargs = session.requests[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/user/manage"))
        self.assertEqual(
            kwargs["json"],
            {"id": 123, "action": "add_quota", "mode": "add", "value": 500000},
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer pat")

    async def test_get_logs_params(self):
        session = FakeSession(
            [FakeResponse(200, '{"success":true,"data":{"items":[],"total":0}}')]
        )
        client = NewAPIClient("https://xingya.site", "pat")
        client._session = session
        await client.get_logs(
            log_type=5,
            start_timestamp=100,
            end_timestamp=200,
            page=2,
            page_size=500,
        )
        params = session.requests[0][2]["params"]
        self.assertEqual(params["type"], 5)
        self.assertEqual(params["p"], 2)
        self.assertEqual(params["page_size"], 100)
        self.assertEqual(params["start_timestamp"], 100)
        self.assertEqual(params["end_timestamp"], 200)

    async def test_get_perf_summary(self):
        session = FakeSession(
            [FakeResponse(200, '{"success":true,"data":{"models":[]}}')]
        )
        client = NewAPIClient("https://xingya.site", "pat")
        client._session = session
        data = await client.get_perf_summary(hours=1)
        self.assertEqual(data["models"], [])
        self.assertEqual(session.requests[0][2]["params"]["hours"], 1)

    async def test_server_error_no_retry(self):
        client = self.make_client(
            [FakeResponse(500, '{"success":false,"message":"boom"}')]
        )
        with self.assertRaises(NewAPIError):
            await client.add_quota(123, 500000)


if __name__ == "__main__":
    unittest.main()
