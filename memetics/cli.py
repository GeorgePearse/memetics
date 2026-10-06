import argparse
import json
import os
import time
from pathlib import Path
from .db import Store
from .engine import Engine
from .github import GitHub
from .model import Model
from .server import serve


def main():
    parser = argparse.ArgumentParser(
        description="Standing upstream listeners and automatic adaptation PRs"
    )
    parser.add_argument(
        "--state",
        default=os.environ.get(
            "MEMETICS_STATE", str(Path.home() / ".local/share/memetics")
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("manifest")
    for name in ("pause", "resume"):
        sub.add_parser(name).add_argument("listener")
    sub.add_parser("status")
    sub.add_parser("show-job").add_argument("id", type=int)
    sub.add_parser("retry").add_argument("id", type=int)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--repository", required=True)
    for name in ("run", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--poll-seconds", type=int, default=60)
        p.add_argument("--daily-model-calls", type=int, default=20)
        if name == "run":
            p.add_argument("--once", action="store_true")
            p.add_argument("--force-poll", action="store_true")
            p.add_argument("--max-jobs", type=int, default=1)
        else:
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8776)
    args = parser.parse_args()
    store = Store(args.state)
    if args.command == "register":
        manifest = json.loads(Path(args.manifest).read_text())
        if manifest.get("version") != 1 or not manifest.get("listeners"):
            parser.error("a version 1 manifest with listeners is required")
        out = {"registered": store.register(manifest["listeners"])}
    elif args.command in {"pause", "resume"}:
        store.enable(args.listener, args.command == "resume")
        out = {"listener": args.listener, "enabled": args.command == "resume"}
    elif args.command == "status":
        out = store.status()
    elif args.command == "show-job":
        out = store.one("SELECT * FROM jobs WHERE id=?", (args.id,))
        if out:
            for field in ("config", "evidence", "delivery"):
                if out[field]:
                    out[field] = json.loads(out[field])
    elif args.command == "retry":
        job = store.one("SELECT * FROM jobs WHERE id=?", (args.id,))
        if not job or job["status"] not in {"blocked", "retry", "delivering"}:
            parser.error("only blocked or waiting jobs can be retried")
        store.set_job(
            args.id,
            "delivering" if job["delivery"] else "retry",
            attempts=0,
            not_before=0,
            reason="explicit retry requested",
        )
        out = {"retry": args.id}
    elif args.command == "search":
        out = store.rows(
            """SELECT repo,sha,snippet(blob_search,2,'[',']','…',24) AS excerpt
            FROM blob_search WHERE blob_search MATCH ? AND repo=? LIMIT 20""",
            (args.query, args.repository),
        )
    else:
        engine = Engine(
            store,
            GitHub(),
            Model(),
            poll_seconds=args.poll_seconds,
            daily_calls=args.daily_model_calls,
        )
        if args.command == "serve":
            serve(
                store, engine, args.host, args.port, min(5, max(1, args.poll_seconds))
            )
            return
        while True:
            out = engine.tick(max_jobs=args.max_jobs, force=args.force_poll)
            print(json.dumps(out, indent=2), flush=True)
            if args.once:
                return
            time.sleep(min(5, max(1, args.poll_seconds)))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
