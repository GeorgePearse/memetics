"""Small GitHub API boundary; credentials never enter model or test subprocesses."""

import base64
import json
import time
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class GitHubError(Exception):
    def __init__(self, status, message, retry_after=60):
        super().__init__(f"GitHub HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token=None):
        self._token = token
        self._app_token = None
        self._app_expires = 0
        self.opener = urllib.request.build_opener(NoRedirect)

    @property
    def token(self):
        if os.environ.get("MEMETICS_GITHUB_APP_ID"):
            if not self._app_token or time.time() >= self._app_expires:
                self._app_token = self.installation_token()
                self._app_expires = time.time() + 3000
            return self._app_token
        if self._token is None:
            self._token = os.environ.get("MEMETICS_GITHUB_TOKEN") or os.environ.get(
                "GH_TOKEN"
            )
            if not self._token:
                result = subprocess.run(
                    ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
                )
                if result.returncode:
                    raise RuntimeError("Set MEMETICS_GITHUB_TOKEN or authenticate gh")
                self._token = result.stdout.strip()
        return self._token

    def installation_token(self):
        def encode(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        now = int(time.time())
        header = encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        payload = encode(
            json.dumps(
                {
                    "iat": now - 60,
                    "exp": now + 540,
                    "iss": os.environ["MEMETICS_GITHUB_APP_ID"],
                }
            ).encode()
        )
        message = header + b"." + payload
        signature = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                os.environ["MEMETICS_GITHUB_APP_KEY_FILE"],
            ],
            input=message,
            capture_output=True,
            timeout=15,
            check=True,
        ).stdout
        jwt = (message + b"." + encode(signature)).decode()
        installation = os.environ["MEMETICS_GITHUB_INSTALLATION_ID"]
        if not installation.isdigit():
            raise ValueError("GitHub installation ID must be numeric")
        req = urllib.request.Request(
            "https://api.github.com/app/installations/"
            + installation
            + "/access_tokens",
            method="POST",
            data=b"{}",
            headers={
                "Authorization": "Bearer " + jwt,
                "Accept": "application/vnd.github+json",
            },
        )
        with self.opener.open(req, timeout=30) as response:
            return json.load(response)["token"]

    def request(self, method, path, data=None, etag=None):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "memetics/0.1",
        }
        if etag:
            headers["If-None-Match"] = etag
        payload = None if data is None else json.dumps(data).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            "https://api.github.com" + path,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(req, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None, response.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, etag
            # GitHub error responses contain no credentials, but do not echo request headers.
            message = e.read().decode(errors="replace")[:1000]
            delay = int(e.headers.get("Retry-After", "60"))
            if e.headers.get("X-RateLimit-Remaining") == "0":
                delay = max(
                    delay,
                    int(e.headers.get("X-RateLimit-Reset", "0")) - int(time.time()) + 1,
                )
            raise GitHubError(e.code, message, delay) from None

    def head(self, repo, ref, etag=None):
        data, tag = self.request(
            "GET",
            f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}",
            etag=etag,
        )
        return (data["sha"] if data else None), tag

    def metadata(self, repo):
        return self.request("GET", f"/repos/{repo}")[0]

    def remote(self, repo):
        return f"https://github.com/{repo}.git"

    def pull(self, repo, number):
        return self.request("GET", f"/repos/{repo}/pulls/{number}")[0]

    def find_pull(self, repo, branch):
        query = urllib.parse.urlencode(
            {"state": "all", "head": repo.split("/")[0] + ":" + branch, "per_page": 100}
        )
        values = self.request("GET", f"/repos/{repo}/pulls?{query}")[0]
        return values[0] if values else None

    def create_pull(self, repo, branch, base, title, body):
        return self.request(
            "POST",
            f"/repos/{repo}/pulls",
            {"head": branch, "base": base, "title": title, "body": body, "draft": True},
        )[0]

    def update_pull(self, repo, number, body):
        return self.request("PATCH", f"/repos/{repo}/pulls/{number}", {"body": body})[0]
