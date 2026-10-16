"""Immutable upstream cache and isolated destination checkouts."""

import os
import shutil
import subprocess
from .config import Blocked, canonical, digest, in_scope, safe_path


def clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }


class Git:
    def __init__(self, store, github):
        self.store, self.github = store, github
        self.askpass = store.root / "askpass.sh"
        self.askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" x-access-token;; *) printf "%s\\n" "$MEMETICS_GIT_TOKEN";; esac\n'
        )
        self.askpass.chmod(0o700)

    def run(self, args, cwd=None, check=True, binary=False):
        env = clean_env()
        env.update(
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS=str(self.askpass),
            MEMETICS_GIT_TOKEN=self.github.token,
            GIT_AUTHOR_NAME="Memetics",
            GIT_AUTHOR_EMAIL="memetics@users.noreply.github.com",
            GIT_COMMITTER_NAME="Memetics",
            GIT_COMMITTER_EMAIL="memetics@users.noreply.github.com",
        )
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=not binary,
            timeout=180,
        )
        if check and result.returncode:
            error = result.stderr.decode(errors="replace") if binary else result.stderr
            raise RuntimeError("git " + args[0] + " failed: " + error[-1500:])
        return result

    def mirror(self, repo, sha):
        path = self.store.root / "mirrors" / digest(repo.lower())
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.run(["init", "--bare", str(path)])
            self.run(["remote", "add", "origin", self.github.remote(repo)], path)
        if self.run(
            ["cat-file", "-e", sha + "^{commit}"], path, check=False
        ).returncode:
            self.run(["fetch", "--filter=blob:none", "--no-tags", "origin", sha], path)
        return path

    def blob(self, repo, mirror, sha):
        found = self.store.one(
            "SELECT content FROM blobs WHERE repo=? AND sha=?", (repo, sha)
        )
        if found:
            return found["content"]
        size = int(self.run(["cat-file", "-s", sha], mirror).stdout)
        if size > 100_000:
            raise Blocked("changed file exceeds the 100 KB context limit")
        data = self.run(["cat-file", "blob", sha], mirror, binary=True).stdout
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Blocked("binary change needs manual assessment") from None
        if "\x00" in text:
            raise Blocked("binary change needs manual assessment")
        self.store.cache_blob(repo, sha, text)
        return text

    def file_at(self, repo, mirror, commit, path):
        tree = self.run(["ls-tree", "-z", commit, "--", path], mirror).stdout
        if not tree:
            return None
        mode, kind, sha = tree.split("\t")[0].split()
        if kind != "blob" or mode == "120000":
            raise Blocked("changed symlink or submodule requires manual assessment")
        return self.blob(repo, mirror, sha)

    def change(self, repo, before, after):
        key = digest([repo.lower(), before, after])
        cached = self.store.one("SELECT data FROM changes WHERE id=?", (key,))
        if cached:
            import json

            return json.loads(cached["data"])
        mirror = self.mirror(repo, before)
        self.mirror(repo, after)
        if self.run(
            ["merge-base", "--is-ancestor", before, after], mirror, check=False
        ).returncode:
            raise Blocked("upstream history diverged; register a reconciled baseline")
        parts = (
            self.run(
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--name-status",
                    "-z",
                    "-M",
                    before,
                    after,
                    "--",
                ],
                mirror,
            )
            .stdout.rstrip("\0")
            .split("\0")
        )
        files = []
        i = 0
        while i < len(parts) and parts[i]:
            status, path = parts[i : i + 2]
            i += 2
            old = path
            if status.startswith(("R", "C")):
                path = parts[i]
                i += 1
            files.append(
                {
                    "status": status,
                    "path": path,
                    "previous_path": old,
                    "before": self.file_at(repo, mirror, before, old),
                    "after": self.file_at(repo, mirror, after, path),
                }
            )
            if len(files) > 100 or len(canonical(files)) > 300_000:
                raise Blocked(
                    "upstream change exceeds bounded context; narrow the baseline after review"
                )
        result = {"repository": repo, "before": before, "after": after, "files": files}
        import time

        self.store.execute(
            "INSERT OR IGNORE INTO changes VALUES(?,?,?,?,?,?)",
            (key, repo, before, after, canonical(result), time.time()),
        )
        return result

    def remote_head(self, repo, branch):
        output = self.run(
            ["ls-remote", "--heads", self.github.remote(repo), "refs/heads/" + branch]
        ).stdout.strip()
        return output.split()[0] if output else None

    def checkout(self, job_id, repo, base, proposal=None):
        path = self.store.root / "work" / str(job_id)
        if path.exists():
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                self.github.remote(repo),
                str(path),
            ]
        )
        self.run(
            ["checkout", "--detach", proposal["head_sha"] if proposal else base], path
        )
        if proposal:
            result = self.run(["merge", "--no-edit", base], path, check=False)
            if result.returncode:
                raise Blocked(
                    "destination base conflicts with the open proposal; reconcile human changes"
                )
        return path

    def context(self, path, config):
        scopes = config["destination"]["paths"] + config["destination"].get(
            "test_paths", []
        )
        tracked = self.run(["ls-files", "-z"], path).stdout.rstrip("\0").split("\0")
        selected = [
            name
            for name in tracked
            if in_scope(name, scopes)
            or name.endswith("AGENTS.md")
            or name in {"README.md", "pyproject.toml", "package.json", "go.mod"}
        ]
        files = {}
        for name in selected:
            safe_path(name)
            file = path / name
            if (
                file.is_symlink()
                or not file.is_file()
                or file.resolve().is_relative_to(path.resolve()) is False
            ):
                raise Blocked(
                    "destination context contains a symlink or unsupported file"
                )
            try:
                files[name] = file.read_text()
            except UnicodeDecodeError:
                raise Blocked("destination context contains binary data") from None
            if len(files) > 80 or len(canonical(files)) > 200_000:
                raise Blocked(
                    "destination context exceeds bounds; narrow configured paths"
                )
        return files

    def apply(self, path, changes, config):
        scopes = config["destination"]["paths"] + config["destination"].get(
            "test_paths", []
        )
        seen = set()
        if not isinstance(changes, list) or not 1 <= len(changes) <= 20:
            raise Blocked("adaptation must provide 1-20 file edits")
        if len(canonical(changes)) > 200_000:
            raise Blocked("adaptation exceeds output size limit")
        for edit in changes:
            name = safe_path(edit["path"])
            if name in seen or not in_scope(name, scopes):
                raise Blocked(
                    "model edit is duplicated or outside authorized paths: " + name
                )
            seen.add(name)
            dest = path / name
            if (
                not dest.resolve().is_relative_to(path.resolve())
                or dest.is_symlink()
                or any(
                    parent.is_symlink()
                    for parent in dest.parents
                    if parent != path.parent
                )
            ):
                raise Blocked("model edit traverses a symlink")
            content = edit.get("content")
            if content is None:
                if dest.is_file():
                    dest.unlink()
            elif isinstance(content, str):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
            else:
                raise Blocked("file content must be a string or null")
        # Only explicit, validated paths can be staged.
        self.run(["add", "--", *sorted(seen)], path)
        if not self.run(["diff", "--cached", "--name-only"], path).stdout.strip():
            raise Blocked(
                "model reported adaptation but produced no implementation changes"
            )
        implementation = config["destination"]["paths"]
        staged = self.run(["diff", "--cached", "--name-only"], path).stdout.splitlines()
        if not any(in_scope(name, implementation) for name in staged):
            raise Blocked(
                "adaptation contains no change to the configured implementation"
            )
        self.run(["diff", "--cached", "--check"], path)

    def commit(self, path, listener_id):
        self.run(
            [
                "commit",
                "-m",
                f"feat: adapt upstream implementation for {listener_id} (AI)",
            ],
            path,
        )
        return self.run(["rev-parse", "HEAD"], path).stdout.strip()

    def publish(self, path, repo, branch, commit, expected):
        remote = self.remote_head(repo, branch)
        if remote == commit:
            return  # A previous attempt already pushed this exact commit.
        if remote != expected:
            raise Blocked(
                "proposal branch changed outside Memetics; refusing to overwrite it"
            )
        if (
            expected
            and self.run(
                ["merge-base", "--is-ancestor", expected, commit], path, check=False
            ).returncode
        ):
            raise Blocked("proposal update would rewrite history")
        # Compare-and-swap the ref after verifying that this is an append-only update.
        self.run(
            [
                "push",
                f"--force-with-lease=refs/heads/{branch}:{expected or ''}",
                "origin",
                f"{commit}:refs/heads/{branch}",
            ],
            path,
        )
