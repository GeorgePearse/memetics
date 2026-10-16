import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from memetics.config import validate
from memetics.db import Store
from memetics.engine import Engine
from memetics.git import clean_env


def git(path, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=path,
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def commit(path, files):
    for name, text in files.items():
        dest = path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            dest.unlink()
        else:
            dest.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")


class FakeGitHub:
    token = "fixture-token-never-sent"

    def __init__(self, repos):
        self.repos = repos
        self.private = {repo: True for repo in repos}
        self.calls = []
        self.pulls = []
        self.lose_create = False

    def remote(self, repo):
        return str(self.repos[repo])

    def metadata(self, repo):
        return {"private": self.private[repo]}

    def head(self, repo, ref, etag=None):
        self.calls.append((repo, ref, etag))
        sha = git(self.repos[repo], "rev-parse", ref)
        return (None if etag == sha else sha), sha

    def fresh(self, pr):
        pr["head"]["sha"] = git(self.repos[pr["repo"]], "rev-parse", pr["branch"])
        return copy.deepcopy(pr)

    def pull(self, repo, number):
        return self.fresh(self.pulls[number - 1])

    def find_pull(self, repo, branch):
        for pr in reversed(self.pulls):
            if pr["repo"] == repo and pr["branch"] == branch:
                return self.fresh(pr)
        return None

    def create_pull(self, repo, branch, base, title, body):
        pr = {
            "number": len(self.pulls) + 1,
            "repo": repo,
            "branch": branch,
            "base": {"ref": base},
            "head": {},
            "title": title,
            "body": body,
            "draft": True,
            "state": "open",
            "merged_at": None,
            "html_url": f"https://github.com/{repo}/pull/{len(self.pulls) + 1}",
        }
        self.pulls.append(pr)
        result = self.fresh(pr)
        if self.lose_create:
            self.lose_create = False
            raise ConnectionError("response lost after GitHub created the PR")
        return result

    def update_pull(self, repo, number, body):
        self.pulls[number - 1]["body"] = body
        return self.fresh(self.pulls[number - 1])


class FixtureModel:
    def __init__(self):
        self.calls = []
        self.override = None

    def adapt(self, context):
        self.calls.append(context)
        if self.override:
            return self.override(context)
        source = next(
            f for f in context["upstream_change"]["files"] if f["after"] is not None
        )
        value = int(source["after"].strip().split("=")[1])
        return {
            "decision": "adapt",
            "reason": "Port the upstream value and regression coverage.",
            "changes": [
                {"path": "value.py", "content": f"VALUE = {value}\n"},
                {
                    "path": "test_value.py",
                    "content": f"from value import VALUE\nassert VALUE == {value}\n",
                },
            ],
        }


class FixtureValidator:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def run(self, checkout, config):
        self.calls += 1
        # Fixtures contain only these controlled tests; production always uses Docker.
        run = subprocess.run(
            ["python3", "test_value.py"],
            cwd=checkout,
            env=clean_env(),
            capture_output=True,
            text=True,
        )
        return {
            "passed": run.returncode == 0 and not self.fail,
            "runner": "fixture",
            "image": "fixture",
            "results": [
                {
                    "command": "python3 test_value.py",
                    "exit_code": 1 if self.fail else run.returncode,
                    "output": run.stderr,
                }
            ],
        }


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.up = root / "up"
        self.down = root / "down"
        for path in (self.up, self.down):
            path.mkdir()
            git(path, "init", "-q", "-b", "main")
        self.before = commit(self.up, {"value.py": "VALUE = 1\n"})
        commit(
            self.down,
            {
                "value.py": "VALUE = 1\n",
                "test_value.py": "from value import VALUE\nassert VALUE == 1\n",
            },
        )
        self.after = commit(self.up, {"value.py": "VALUE = 2\n"})
        self.store = Store(root / "state")
        self.github = FakeGitHub({"owner/up": self.up, "owner/down": self.down})
        self.model = FixtureModel()
        self.validator = FixtureValidator()
        self.engine = Engine(self.store, self.github, self.model, self.validator)
        self.config = {
            "id": "value",
            "enabled": True,
            "concern": "value handling",
            "upstream": {
                "repository": "owner/up",
                "ref": "main",
                "baseline_commit": self.before,
                "paths": ["value.py"],
            },
            "destination": {
                "repository": "owner/down",
                "base_ref": "main",
                "paths": ["value.py"],
                "test_paths": ["test_value.py"],
            },
            "adaptation": {
                "instructions": "Follow upstream",
                "validation_commands": ["python3 test_value.py"],
            },
        }

    def register(self):
        self.store.register([self.config])

    def job(self):
        return self.store.one("SELECT * FROM jobs ORDER BY id DESC LIMIT 1")

    def test_end_to_end_and_restart_dedup(self):
        self.register()
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "proposed")
        self.assertEqual(len(self.github.pulls), 1)
        pr = self.github.pulls[0]
        self.assertTrue(pr["draft"])
        self.assertIn(self.after, pr["body"])
        self.assertIn("Validation: **passed**", pr["body"])
        self.assertEqual(
            git(self.down, "show", pr["branch"] + ":value.py"), "VALUE = 2"
        )
        restarted = Engine(
            Store(self.store.root), self.github, self.model, self.validator
        )
        restarted.tick(force=True)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.github.pulls), 1)
        self.assertIsNone(self.store.one("SELECT adopted FROM listeners")["adopted"])

    def test_two_listeners_share_poll_and_index(self):
        second = copy.deepcopy(self.config)
        second["id"] = "second"
        self.store.register([self.config, second])
        self.engine.tick(max_jobs=2, force=True)
        upstream_calls = [c for c in self.github.calls if c[0] == "owner/up"]
        self.assertEqual(len(upstream_calls), 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM changes")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM blobs")["n"], 2)
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(len(self.github.pulls), 2)
        self.assertTrue(
            self.store.rows("SELECT * FROM blob_search WHERE blob_search MATCH 'VALUE'")
        )

    def test_registration_catches_up_on_304_and_pause_is_sticky(self):
        self.register()
        self.engine.tick(force=True)
        second = copy.deepcopy(self.config)
        second["id"] = "late"
        self.store.register([second])
        self.engine.tick(force=True)
        self.assertEqual(len(self.github.pulls), 2)
        self.store.enable("value", False)
        self.store.enable("late", False)
        self.store.register([self.config])
        count = len(self.github.calls)
        self.engine.poll(force=True)
        self.assertEqual(len(self.github.calls), count)
        self.assertEqual(
            self.store.one("SELECT enabled FROM listeners WHERE id=?", ("value",))[
                "enabled"
            ],
            0,
        )

    def test_update_open_pr_preserves_body_and_history(self):
        self.register()
        self.engine.tick(force=True)
        pr = self.github.pulls[0]
        old = git(self.down, "rev-parse", pr["branch"])
        pr["body"] = "Human introduction\n" + pr["body"] + "\nHuman notes"
        latest = commit(self.up, {"value.py": "VALUE = 3\n"})
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "proposed")
        self.assertEqual(len(self.github.pulls), 1)
        self.assertTrue(pr["body"].startswith("Human introduction"))
        self.assertTrue(pr["body"].endswith("Human notes"))
        self.assertIn(latest, pr["body"])
        git(self.down, "merge-base", "--is-ancestor", old, pr["branch"])
        self.assertEqual(
            git(self.down, "show", pr["branch"] + ":value.py"), "VALUE = 3"
        )

    def test_lost_create_response_recovers_without_second_pr_or_model_call(self):
        self.register()
        self.github.lose_create = True
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "delivering")
        self.store.execute("UPDATE jobs SET not_before=0")
        Engine(Store(self.store.root), self.github, self.model, self.validator).tick(
            force=True
        )
        self.assertEqual(self.job()["status"], "proposed")
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(len(self.model.calls), 1)

    def test_human_branch_changes_block_overwrite(self):
        self.register()
        self.engine.tick(force=True)
        branch = self.github.pulls[0]["branch"]
        git(self.down, "checkout", "-q", branch)
        human = commit(self.down, {"notes.txt": "human work\n"})
        git(self.down, "checkout", "-q", "main")
        commit(self.up, {"value.py": "VALUE = 3\n"})
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "blocked")
        self.assertEqual(git(self.down, "rev-parse", branch), human)
        self.assertEqual(len(self.model.calls), 1)

    def test_close_declines_without_reopening(self):
        self.register()
        self.engine.tick(force=True)
        self.github.pulls[0]["state"] = "closed"
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "declined")
        self.assertEqual(len(self.github.pulls), 1)
        self.assertIsNone(self.store.one("SELECT adopted FROM listeners")["adopted"])

    def test_merge_records_adopted_revision(self):
        self.register()
        self.engine.tick(force=True)
        self.github.pulls[0].update(state="closed", merged_at="now")
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "adopted")
        self.assertEqual(
            self.store.one("SELECT adopted FROM listeners")["adopted"], self.after
        )

    def test_failed_validation_is_visible_on_draft(self):
        self.register()
        self.validator.fail = True
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "proposed")
        self.assertIn("BLOCKED", self.github.pulls[0]["body"])
        self.assertFalse(json.loads(self.job()["evidence"])["validation"]["passed"])

    def test_private_source_cannot_leak_to_public_destination(self):
        self.register()
        self.github.private["owner/down"] = False
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "blocked")
        self.assertFalse(self.model.calls)
        self.assertFalse(self.github.pulls)

    def test_out_of_scope_model_edit_never_publishes(self):
        self.register()
        self.model.override = lambda _: {
            "decision": "adapt",
            "reason": "bad edit",
            "changes": [{"path": ".github/workflows/oops.yml", "content": "oops"}],
        }
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "blocked")
        self.assertFalse(self.github.pulls)

    def test_budget_waits_without_spinning_or_spending(self):
        self.register()
        self.engine.daily_calls = 0
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "retry")
        self.assertEqual(self.job()["attempts"], 0)
        self.assertFalse(self.model.calls)

    def test_model_can_classify_semantic_change_outside_original_path(self):
        git(self.up, "mv", "value.py", "renamed.py")
        git(self.up, "commit", "-qm", "rename")
        self.register()
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "proposed")
        paths = [f["path"] for f in self.model.calls[0]["upstream_change"]["files"]]
        self.assertIn("renamed.py", paths)

    def test_force_push_blocks_instead_of_assuming_linear_history(self):
        git(self.up, "checkout", "--orphan", "replacement")
        git(self.up, "rm", "-rf", ".")
        commit(self.up, {"value.py": "VALUE = 9\n"})
        git(self.up, "branch", "-M", "main")
        self.register()
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "blocked")
        self.assertFalse(self.model.calls)

    def test_destination_base_advance_revalidates(self):
        self.register()
        original = self.validator.run

        def validate_then_advance(checkout, config):
            result = original(checkout, config)
            if self.validator.calls == 1:
                commit(self.down, {"new.txt": "new base\n"})
            return result

        self.validator.run = validate_then_advance
        self.engine.tick(force=True)
        self.assertEqual(self.job()["status"], "proposed")
        self.assertEqual(self.validator.calls, 2)
        self.assertEqual(
            git(self.down, "show", self.github.pulls[0]["branch"] + ":new.txt"),
            "new base",
        )

    def test_registration_validates_atomically_and_rejects_traversal(self):
        invalid = copy.deepcopy(self.config)
        invalid["id"] = "bad"
        invalid["destination"]["paths"] = ["../outside"]
        with self.assertRaises(ValueError):
            self.store.register([self.config, invalid])
        self.assertFalse(self.store.rows("SELECT * FROM listeners"))
        self.config["delivery"] = {"auto_merge": True}
        with self.assertRaises(ValueError):
            validate(self.config)

    def test_running_job_recovers_after_restart(self):
        self.register()
        self.engine.poll(force=True)
        self.store.set_job(self.job()["id"], "running", attempts=1)
        self.engine.tick()
        self.assertEqual(self.job()["status"], "proposed")
        self.assertEqual(self.job()["attempts"], 2)

    def test_process_lock_excludes_another_worker(self):
        with self.engine.lock():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.engine.tick()


if __name__ == "__main__":
    unittest.main()
