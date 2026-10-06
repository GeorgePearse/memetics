# Implementation verification

Verified on 2026-10-06 using a private, controlled repository owned by George Pearse. No production implementation was changed or merged.

## Real upstream → real model → real draft PRs

Two enabled listeners share `GeorgePearse/memetics-demo:upstream` and independently adapt two destination modules on `demo-base`:

- Baseline: `295d1299e5f965a0025b6dccf22691d0e1d53cc0`.
- Upstream change: `b2daf586171b5c7041ec583cb36661975187c0aa` changes linear retry delays to capped exponential delays.
- [Primary adaptation PR #1](https://github.com/GeorgePearse/memetics-demo/pull/1): `src/backoff.py` and its tests; generated head `ad202c9f3a0e46eae6e83478beb6212643602d09`.
- [Secondary adaptation PR #2](https://github.com/GeorgePearse/memetics-demo/pull/2): `src/secondary_backoff.py` and its tests; generated head `d2ac677ec87e44c013977b18f8f8d9e0514a45bb`.

The worker used the real GitHub API, real Git fetch/push, and two xAI `grok-4.7` calls. Both PRs are drafts. Each changes the formula through the existing local function name and adds regression cases. Validation passed inside `python:3.13-slim` with network disabled. The image available during verification had digest `sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c`.

Independent negative-control checks restored the old implementation in disposable copies while retaining the generated tests. Both test suites failed on the old behavior. Thus the added tests distinguish the intended behavioral change, rather than merely passing on either version.

After publication, persisted state contained **two jobs, two model calls, one shared change record and two indexed blobs**. Service restart and subsequent polling retained the same two jobs and proposals without another model call. DeepSeek's disabled example performed zero upstream polls.

## Runtime and interface

A user systemd service runs on the development host at `http://127.0.0.1:8776`. Credentials are outside the checkout, and state is under `~/.local/share/memetics`. This is a local service, not a public deployment.

Browser verification covered dashboard loading, real listener/job/PR data, reload, a narrow viewport, and console errors. `/health` returned HTTP 200. The dashboard displays observed revisions and the two draft PR links; adopted revisions remain empty because neither PR has merged.

## Automated checks

`python -m unittest discover -s tests -v` passes 24 tests covering real local Git operations plus controlled external boundaries: shared indexing, conditional polling and late registration, pause behavior, interrupted work, lost PR-create responses, append-only updates, human edits, decline and merge reconciliation, destination-base movement, private/public separation, write scope, budgets, upstream history divergence, API auth, signed webhook deduplication, GitHub App JWT creation/token caching, rate-limit reset handling, and incomplete model output.

The source distribution and wheel build successfully with `uv build`. CI runs the suite and package/CLI installation on Python 3.11 and 3.13. This repository has no pre-commit configuration, so `prek run --from-ref origin/main --to-ref HEAD` is unavailable here.

The DeepSeek listener remains disabled until its illustrative destination is replaced by a real repository, authorized paths and runnable checks. GitHub App signing/token caching is unit-tested; this live run used the owner's existing GitHub CLI authentication.
