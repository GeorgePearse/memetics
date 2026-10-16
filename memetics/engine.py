"""A serial, restartable coordinator. SQLite persists work; flock excludes competing workers."""

import fcntl
import json
import time
from contextlib import contextmanager
from .config import Blocked, canonical, digest
from .git import Git
from .validation import DockerValidator

START = "<!-- memetics:start -->"
END = "<!-- memetics:end -->"


class Engine:
    def __init__(
        self, store, github, model, validator=None, poll_seconds=60, daily_calls=20
    ):
        self.store, self.github, self.model = store, github, model
        self.git = Git(store, github)
        self.validator = validator or DockerValidator()
        self.poll_seconds = max(10, poll_seconds)
        self.daily_calls = daily_calls
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS model_calls(job_id INTEGER, started REAL NOT NULL)"
        )

    @contextmanager
    def lock(self):
        with (self.store.root / "worker.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "another Memetics worker is already running"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def authorized(self, job):
        listener = self.store.one(
            "SELECT * FROM listeners WHERE id=?", (job["listener_id"],)
        )
        if (
            not listener
            or not listener["enabled"]
            or listener["config_hash"] != job["config_hash"]
        ):
            raise Blocked(
                "listener paused or changed during adaptation; publication stopped"
            )
        return listener

    def poll(self, force=False):
        sources = self.store.rows("""SELECT * FROM sources s WHERE EXISTS
            (SELECT 1 FROM listeners l WHERE l.source_id=s.id AND l.enabled=1)""")
        for source in sources:
            head = source["head"]
            if force or source["next_poll"] <= time.time():
                try:
                    new_head, etag = self.github.head(
                        source["repo"], source["ref"], source["etag"]
                    )
                    head = new_head or head
                    if not head:
                        raise RuntimeError(
                            "conditional response without a saved upstream revision"
                        )
                    self.store.execute(
                        "UPDATE sources SET head=?,etag=?,next_poll=?,error=NULL,polls=polls+1 WHERE id=?",
                        (head, etag, time.time() + self.poll_seconds, source["id"]),
                    )
                except Exception as exc:
                    delay = max(self.poll_seconds, getattr(exc, "retry_after", 60))
                    self.store.execute(
                        "UPDATE sources SET error=?,next_poll=? WHERE id=?",
                        (str(exc), time.time() + delay, source["id"]),
                    )
                    continue
            if head:
                # New registrations catch up even when the shared source returned 304.
                self.store.enqueue(source["id"], head)

    def reconcile(self):
        for proposal in self.store.rows("SELECT * FROM proposals WHERE state='open'"):
            try:
                pr = self.github.pull(proposal["repo"], proposal["number"])
                if pr["state"] != "closed":
                    continue
                if pr.get("merged_at"):
                    state = (
                        "adopted"
                        if pr["head"]["sha"] == proposal["head_sha"]
                        else "modified"
                    )
                else:
                    state = "declined"
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE proposals SET state=? WHERE listener_id=?",
                        (state, proposal["listener_id"]),
                    )
                    db.execute(
                        "UPDATE jobs SET status=?,updated=? WHERE listener_id=? AND status='proposed'",
                        (state, time.time(), proposal["listener_id"]),
                    )
                    if state == "adopted":
                        db.execute(
                            "UPDATE listeners SET adopted=? WHERE id=?",
                            (proposal["upstream_sha"], proposal["listener_id"]),
                        )
            except Exception as exc:
                # A failed read must not be treated as a closed/missing PR.
                self.store.execute(
                    "UPDATE sources SET error=? WHERE id=(SELECT source_id FROM listeners WHERE id=?)",
                    ("PR reconciliation: " + str(exc), proposal["listener_id"]),
                )

    def tick(self, max_jobs=1, force=False):
        with self.lock():
            # No other worker owns these jobs after acquiring the process lock.
            self.store.execute(
                "UPDATE jobs SET status='retry',reason='worker interrupted before publication' WHERE status='running'"
            )
            self.reconcile()
            self.poll(force)
            jobs = self.store.rows(
                """SELECT j.* FROM jobs j JOIN listeners l ON l.id=j.listener_id
                WHERE l.enabled=1 AND j.status IN ('queued','retry','delivering') AND j.not_before<=?
                ORDER BY j.id LIMIT ?""",
                (time.time(), max_jobs),
            )
            for job in jobs:
                self.work(job)
        return self.store.status()

    def work(self, job):
        attempts = job["attempts"] + 1
        if attempts > 3:
            self.store.set_job(
                job["id"],
                "blocked",
                reason="retry budget exhausted; inspect the job before retrying",
            )
            return
        self.store.set_job(
            job["id"], "delivering" if job["delivery"] else "running", attempts=attempts
        )
        try:
            self.authorized(job)
            if job["delivery"]:
                self.deliver(job, json.loads(job["delivery"]))
                return
            config = json.loads(job["config"])
            source, destination = config["upstream"], config["destination"]
            upstream_meta = self.github.metadata(source["repository"])
            destination_meta = self.github.metadata(destination["repository"])
            if upstream_meta["private"] and not destination_meta["private"]:
                raise Blocked(
                    "private upstream evidence cannot be published to a public destination"
                )
            proposal = self.store.one(
                "SELECT * FROM proposals WHERE listener_id=? AND state='open'",
                (job["listener_id"],),
            )
            if proposal:
                pr = self.github.pull(proposal["repo"], proposal["number"])
                if (
                    pr["state"] != "open"
                    or pr["head"]["sha"] != proposal["head_sha"]
                    or pr["base"]["ref"] != destination["base_ref"]
                ):
                    raise Blocked(
                        "open proposal changed outside Memetics; reconcile before updating"
                    )
                if (
                    self.git.remote_head(destination["repository"], proposal["branch"])
                    != proposal["head_sha"]
                ):
                    raise Blocked(
                        "proposal branch has human edits; refusing to overwrite"
                    )
            change = self.git.change(
                source["repository"], job["before_sha"], job["after_sha"]
            )
            if not change["files"]:
                self.store.finish(
                    job, "irrelevant", "upstream tree did not change", change
                )
                return
            base, _ = self.github.head(
                destination["repository"], destination["base_ref"]
            )
            path = self.git.checkout(
                job["id"], destination["repository"], base, proposal
            )
            files = self.git.context(path, config)
            decisions = self.store.rows(
                """SELECT after_sha,status,reason FROM jobs WHERE listener_id=?
                AND status IN ('declined','irrelevant','already_present','adopted') ORDER BY id DESC LIMIT 20""",
                (job["listener_id"],),
            )
            count = self.store.one(
                "SELECT count(*) AS n FROM model_calls WHERE started>?",
                (time.time() - 86400,),
            )["n"]
            if count >= self.daily_calls:
                # Budget waiting is not a model attempt or a blocked listener.
                self.store.set_job(
                    job["id"],
                    "retry",
                    attempts=job["attempts"],
                    not_before=time.time() + 3600,
                    reason="daily model call budget reached",
                )
                return
            self.store.execute(
                "INSERT INTO model_calls VALUES(?,?)", (job["id"], time.time())
            )
            decision = self.model.adapt(
                {
                    "listener": config,
                    "upstream_change": change,
                    "destination_base": base,
                    "destination_files": files,
                    "prior_decisions": decisions,
                }
            )
            evidence = {
                "upstream_before": job["before_sha"],
                "upstream_after": job["after_sha"],
                "destination_base": base,
                "model_decision": decision,
            }
            self.authorized(job)
            if decision["decision"] == "blocked":
                self.store.set_job(
                    job["id"],
                    "blocked",
                    reason=decision["reason"],
                    evidence=canonical(evidence),
                )
                return
            if decision["decision"] != "adapt":
                self.store.finish(
                    job, decision["decision"], decision["reason"], evidence
                )
                return
            self.git.apply(path, decision["changes"], config)
            checks = self.validator.run(path, config)
            evidence["validation"] = checks
            self.authorized(job)
            commit = self.git.commit(path, job["listener_id"])
            branch = (
                proposal["branch"]
                if proposal
                else "memetics/"
                + digest([job["listener_id"], job["config_hash"]])[:12]
                + "-"
                + job["after_sha"][:12]
            )
            delivery = {
                "repo": destination["repository"],
                "base_ref": destination["base_ref"],
                "base_sha": base,
                "branch": branch,
                "commit": commit,
                "expected": proposal["head_sha"] if proposal else None,
                "workdir": str(path),
                "evidence": evidence,
                "prior_number": proposal["number"] if proposal else None,
            }
            # Persist the entire intent before the first external write.
            self.store.set_job(
                job["id"],
                "delivering",
                delivery=canonical(delivery),
                evidence=canonical(evidence),
            )
            self.deliver(job, delivery)
        except Blocked as exc:
            self.store.set_job(job["id"], "blocked", reason=str(exc))
        except Exception as exc:
            saved = self.store.one("SELECT delivery FROM jobs WHERE id=?", (job["id"],))
            self.store.set_job(
                job["id"],
                "delivering" if saved["delivery"] else "retry",
                reason=str(exc),
                not_before=time.time()
                + max(30 * attempts, getattr(exc, "retry_after", 0)),
            )

    def body(self, job, delivery):
        evidence = delivery["evidence"]
        config = json.loads(job["config"])
        repo = config["upstream"]["repository"]
        checks = evidence["validation"]
        lines = [
            START,
            f"Listener: `{job['listener_id']}`",
            "",
            config["concern"],
            "",
            f"Upstream: [{repo}](https://github.com/{repo})",
            f"[Implementation comparison](https://github.com/{repo}/compare/{job['before_sha']}...{job['after_sha']})",
            f"Destination base: `{delivery['base_sha']}`",
            "",
            evidence["model_decision"]["reason"][:5000],
            "",
            "Validation: **"
            + (
                "passed"
                if checks["passed"]
                else "BLOCKED — checks failed or unavailable"
            )
            + "**",
            f"Runner: Docker, image `{checks['image']}`; network disabled.",
        ]
        for result in checks["results"]:
            lines.extend(
                [
                    "",
                    f"- `{result['command']}` → exit {result['exit_code']}",
                    "<details><summary>Output (tail)</summary>",
                    "",
                    "```text",
                    result["output"][-3000:].replace("```", "` ` `"),
                    "```",
                    "</details>",
                ]
            )
        lines.extend(
            [
                "",
                "Generated from pinned source evidence. Review the adaptation and any intentional deviations before merging.",
                f"Durable run: `{job['id']}`. Requested by the owner who registered this listener.",
                END,
            ]
        )
        return "\n".join(lines)

    def deliver(self, job, delivery):
        from pathlib import Path

        self.authorized(job)
        config = json.loads(job["config"])
        # Recheck visibility immediately before publishing retained source evidence.
        if (
            self.github.metadata(config["upstream"]["repository"])["private"]
            and not self.github.metadata(delivery["repo"])["private"]
        ):
            raise Blocked("destination visibility would expose private source evidence")
        path = Path(delivery["workdir"])
        if not path.exists():
            raise Blocked(
                "retained checkout is missing; inspect remote publication before recovery"
            )
        existing = self.github.find_pull(delivery["repo"], delivery["branch"])
        if existing and existing["state"] == "closed":
            # A lost create response followed by a human close must not reopen the PR.
            self.record_proposal(job, delivery, existing)
            self.reconcile()
            return
        if existing and existing["base"]["ref"] != delivery["base_ref"]:
            raise Blocked("proposal base branch changed outside Memetics")
        if existing and (
            START not in (existing.get("body") or "")
            or END not in (existing.get("body") or "")
        ):
            raise Blocked(
                "generated PR section was removed; preserve the human-edited body"
            )
        latest, _ = self.github.head(delivery["repo"], delivery["base_ref"])
        if latest != delivery["base_sha"]:
            remote = self.git.remote_head(delivery["repo"], delivery["branch"])
            if remote not in {delivery["expected"], delivery["commit"]}:
                raise Blocked("proposal branch changed while destination base advanced")
            self.git.run(["fetch", "origin", latest], path)
            if self.git.run(
                ["merge", "--no-edit", latest], path, check=False
            ).returncode:
                raise Blocked("new destination base conflicts with adaptation")
            delivery["base_sha"] = latest
            delivery["evidence"]["destination_base"] = latest
            delivery["evidence"]["validation"] = self.validator.run(path, config)
            delivery["expected"] = remote
            delivery["commit"] = self.git.run(
                ["rev-parse", "HEAD"], path
            ).stdout.strip()
            self.store.set_job(
                job["id"],
                "delivering",
                delivery=canonical(delivery),
                evidence=canonical(delivery["evidence"]),
            )
        self.authorized(job)
        self.git.publish(
            path,
            delivery["repo"],
            delivery["branch"],
            delivery["commit"],
            delivery["expected"],
        )
        body = self.body(job, delivery)
        existing = self.github.find_pull(delivery["repo"], delivery["branch"])
        if existing:
            if existing["state"] != "open":
                self.record_proposal(job, delivery, existing)
                self.reconcile()
                return
            old = existing.get("body") or ""
            if START not in old or END not in old:
                raise Blocked(
                    "generated PR section was removed; preserve the human-edited body"
                )
            body = old.split(START, 1)[0] + body + old.split(END, 1)[1]
            pr = self.github.update_pull(delivery["repo"], existing["number"], body)
        else:
            title = ("feat: follow upstream " + job["listener_id"])[:120]
            pr = self.github.create_pull(
                delivery["repo"], delivery["branch"], delivery["base_ref"], title, body
            )
        self.record_proposal(job, delivery, pr)

    def record_proposal(self, job, delivery, pr):
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO proposals(listener_id,repo,number,url,branch,head_sha,upstream_sha,state)
                VALUES(?,?,?,?,?,?,?,'open') ON CONFLICT(listener_id) DO UPDATE SET repo=excluded.repo,
                number=excluded.number,url=excluded.url,branch=excluded.branch,head_sha=excluded.head_sha,
                upstream_sha=excluded.upstream_sha,state='open' """,
                (
                    job["listener_id"],
                    delivery["repo"],
                    pr["number"],
                    pr["html_url"],
                    delivery["branch"],
                    delivery["commit"],
                    job["after_sha"],
                ),
            )
            db.execute(
                "UPDATE jobs SET status='proposed',reason=?,updated=? WHERE id=?",
                (pr["html_url"], time.time(), job["id"]),
            )
            db.execute(
                "UPDATE listeners SET decided=? WHERE id=?",
                (job["after_sha"], job["listener_id"]),
            )
