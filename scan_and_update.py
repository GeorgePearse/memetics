#!/usr/bin/env python3
"""Memetics: detect upstream pattern drift and ask Claude to reconcile the local implementation.

Scans a glob of "meme" files (YAML frontmatter + Markdown body). Each meme file
describes one bit of functionality and points at one or more upstream
repo/path/ref locations plus the local file(s) that implement the pattern.
For each upstream location whose latest commit (touching the tracked paths)
differs from the recorded `last_seen` SHA, this script fetches the before/after
file content, asks Claude to update the local implementation, writes the
result to disk, and advances `last_seen` in the meme file.

Intended to run inside the composite GitHub Action defined by action.yml.
"""
import base64
import glob
import json
import os
import re

import requests
import yaml
from anthropic import Anthropic

GITHUB_API = "https://api.github.com"


def gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def parse_meme_file(path):
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError(f"{path}: missing YAML frontmatter (expected --- ... ---)")
    frontmatter = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    return frontmatter, body


def dump_meme_file(path, frontmatter, body):
    fm = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm}\n---\n{body}")


def latest_commit_for_paths(token, repo, ref, paths):
    """Most recent commit SHA among all given paths on `ref`, or None if none found."""
    latest_sha, latest_date = None, None
    for path in paths:
        resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/commits",
            headers=gh_headers(token),
            params={"sha": ref, "path": path, "per_page": 1},
            timeout=30,
        )
        resp.raise_for_status()
        commits = resp.json()
        if not commits:
            continue
        sha = commits[0]["sha"]
        date = commits[0]["commit"]["committer"]["date"]
        if latest_date is None or date > latest_date:
            latest_sha, latest_date = sha, date
    return latest_sha


def file_content_at_ref(token, repo, path, ref):
    if not ref:
        return None
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=gh_headers(token),
        params={"ref": ref},
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        raise ValueError(f"{path}@{ref} in {repo} is a directory, not a file")
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


def build_prompt(meme_name, body, upstream_diffs, local_files):
    parts = [
        "You are updating a local implementation to follow an upstream pattern "
        f"change, for a tracked pattern named '{meme_name}'.",
        "",
        "## Pattern description (human-authored)",
        body.strip(),
        "",
        "## What changed upstream",
    ]
    for d in upstream_diffs:
        parts.append(f"\n### {d['repo']} — {d['path']}")
        parts.append(
            f"Old ({d['old_sha'][:10] if d['old_sha'] else 'none'}):\n```\n"
            f"{d['old_content'] if d['old_content'] is not None else '(file did not exist)'}\n```"
        )
        parts.append(
            f"New ({d['new_sha'][:10]}):\n```\n"
            f"{d['new_content'] if d['new_content'] is not None else '(file removed upstream)'}\n```"
        )
    parts.append("\n## Current local implementation")
    for f in local_files:
        parts.append(f"\n### {f['path']}\n```\n{f['content']}\n```")
    parts.append(
        "\nUpdate the local implementation(s) to incorporate the upstream change, "
        "preserving local coding style, the local API surface, and any adaptation "
        "notes in the pattern description above. Only change what the upstream "
        "diff actually motivates — do not refactor unrelated code or rename "
        "things that didn't change upstream. If a local file needs no change, "
        "omit it from your output entirely."
    )
    return "\n".join(parts)


FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One paragraph: what changed upstream and what was updated locally, for a PR description.",
        },
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "files"],
    "additionalProperties": False,
}


def ask_claude(client, model, prompt):
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"format": {"type": "json_schema", "schema": FILES_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def process_meme_file(meme_path, github_token, client, model):
    """Returns a summary string if the local implementation was updated, else None."""
    frontmatter, body = parse_meme_file(meme_path)
    upstreams = frontmatter.get("upstream") or []
    local = frontmatter.get("local") or {}
    local_paths = local.get("paths") or []

    upstream_diffs = []
    any_advanced = False
    for up in upstreams:
        repo = up["repo"]
        ref = up.get("ref", "main")
        paths = up.get("paths") or []
        last_seen = up.get("last_seen")

        latest = latest_commit_for_paths(github_token, repo, ref, paths)
        if latest is None or latest == last_seen:
            continue

        for path in paths:
            old_content = file_content_at_ref(github_token, repo, path, last_seen)
            new_content = file_content_at_ref(github_token, repo, path, latest)
            if old_content == new_content:
                continue
            upstream_diffs.append(
                {
                    "repo": repo,
                    "path": path,
                    "old_sha": last_seen,
                    "new_sha": latest,
                    "old_content": old_content,
                    "new_content": new_content,
                }
            )
        up["last_seen"] = latest
        any_advanced = True

    if not upstream_diffs:
        if any_advanced:
            # Upstream touched the path but produced no textual diff on tracked
            # files (e.g. a path-filtered commit to something else nearby) —
            # still persist the advanced last_seen so we don't re-check it forever.
            dump_meme_file(meme_path, frontmatter, body)
        return None

    local_files = []
    for lp in local_paths:
        if os.path.exists(lp):
            local_files.append({"path": lp, "content": open(lp, encoding="utf-8").read()})

    meme_name = os.path.splitext(os.path.basename(meme_path))[0]
    prompt = build_prompt(meme_name, body, upstream_diffs, local_files)
    result = ask_claude(client, model, prompt)

    for f in result["files"]:
        parent = os.path.dirname(f["path"])
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(f["path"], "w", encoding="utf-8") as out:
            out.write(f["content"])

    dump_meme_file(meme_path, frontmatter, body)
    return f"**{meme_name}**: {result['summary']}"


def main():
    github_token = os.environ["GITHUB_TOKEN"]
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("MEMETICS_MODEL", "claude-opus-5")
    scan_glob = os.environ.get("MEMETICS_PATHS", ".memetics/**/*.md")

    client = Anthropic(api_key=anthropic_key)

    meme_paths = sorted(glob.glob(scan_glob, recursive=True))
    if not meme_paths:
        print(f"Memetics: no meme files matched '{scan_glob}'.")

    summaries = []
    for meme_path in meme_paths:
        print(f"Memetics: checking {meme_path}")
        summary = process_meme_file(meme_path, github_token, client, model)
        if summary:
            summaries.append(summary)
            print(f"Memetics: updated local implementation for {meme_path}")

    changed_any = bool(summaries)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"changed={'true' if changed_any else 'false'}\n")
            if summaries:
                f.write("summary<<EOF\n")
                f.write("\n\n".join(summaries))
                f.write("\nEOF\n")

    print("Memetics sync complete." if changed_any else "Memetics: no upstream changes detected.")


if __name__ == "__main__":
    main()
