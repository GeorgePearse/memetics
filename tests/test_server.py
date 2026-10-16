import hashlib
import hmac
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from memetics.db import Store
from memetics.server import make_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.server = make_server(
            self.store, port=0, api_token="test-token", webhook_secret="secret"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_dashboard_and_api_auth(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Memetics", body)
        self.assertEqual(self.request("/api/status")[0], 401)
        status, body = self.request(
            "/api/status", headers={"Authorization": "Bearer test-token"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["listeners"], [])

    def test_webhook_signature_dedup_and_wakeup(self):
        self.store.execute(
            "INSERT INTO sources(id,repo,ref,next_poll) VALUES(?,?,?,?)",
            ("source", "Owner/Repo", "main", 9999999999),
        )
        body = json.dumps(
            {"repository": {"full_name": "owner/repo"}, "ref": "refs/heads/main"}
        ).encode()
        headers = {
            "X-GitHub-Delivery": "one",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256="
            + hmac.new(b"secret", body, hashlib.sha256).hexdigest(),
        }
        self.assertEqual(self.request("/webhooks/github", body)[0], 401)
        status, response = self.request("/webhooks/github", body, headers)
        self.assertEqual(status, 202)
        self.assertFalse(json.loads(response)["duplicate"])
        self.assertEqual(
            self.store.one("SELECT next_poll FROM sources")["next_poll"], 0
        )
        self.assertTrue(
            json.loads(self.request("/webhooks/github", body, headers)[1])["duplicate"]
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM events")["n"], 1)

    def test_remote_bind_requires_token(self):
        with self.assertRaises(ValueError):
            make_server(self.store, host="0.0.0.0", port=0)
