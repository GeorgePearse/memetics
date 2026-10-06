import io
import json
import os
import subprocess
import unittest
import urllib.error
from unittest.mock import patch, Mock

from memetics.github import GitHub, GitHubError
from memetics.model import Model
from memetics.config import Blocked


class BoundaryTests(unittest.TestCase):
    def test_app_jwt_and_cached_installation_token(self):
        import base64

        env = {
            "MEMETICS_GITHUB_APP_ID": "123",
            "MEMETICS_GITHUB_APP_KEY_FILE": "/private/key.pem",
            "MEMETICS_GITHUB_INSTALLATION_ID": "456",
        }
        client = GitHub()
        client.opener = Mock()
        response = io.BytesIO(b'{"token":"installation-secret"}')
        client.opener.open.return_value = response
        with (
            patch.dict(os.environ, env),
            patch(
                "memetics.github.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, b"signature"),
            ) as run,
        ):
            self.assertEqual(client.token, "installation-secret")
            self.assertEqual(client.token, "installation-secret")
            self.assertEqual(run.call_count, 1)
            request = client.opener.open.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://api.github.com/app/installations/456/access_tokens",
            )
            jwt = request.get_header("Authorization").split()[1]
            payload = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "==="))
            self.assertEqual(payload["iss"], "123")
            self.assertLessEqual(payload["exp"] - payload["iat"], 600)
            self.assertNotIn("installation-secret", jwt)

    def test_rate_limit_reset_controls_retry(self):
        client = GitHub("test-token")
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            "https://api.github.com",
            403,
            "limited",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1500"},
            io.BytesIO(b"limited"),
        )
        with patch("memetics.github.time.time", return_value=1000):
            with self.assertRaises(GitHubError) as error:
                client.head("owner/repo", "main")
        self.assertEqual(error.exception.retry_after, 501)

    def test_model_rejects_incomplete_output(self):
        with patch.dict(
            os.environ, {"MEMETICS_MODEL": "test", "MEMETICS_MODEL_API_KEY": "sentinel-model-key-12345"}
        ):
            model = Model()
        opener = Mock()
        opener.open.return_value = io.BytesIO(
            json.dumps({"choices": [{"finish_reason": "length"}]}).encode()
        )
        with patch("memetics.model.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(Blocked, "did not complete"):
                model.adapt({"listener": "fixture"})
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("sentinel-model-key-12345", json.dumps(payload))
        self.assertNotIn("tools", payload)
