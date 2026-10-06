# Memetics

Trace the ideas behind your code. Turn relevant upstream implementation changes into PRs.

A GitHub repository depends on more than the packages in its lockfile. Its architecture,
algorithms, APIs, and small implementation details often come from other repositories,
papers, articles, and conversations. Those connections usually disappear into browser
history, chat transcripts, or a passing comment.

Memetics is a project for making those connections explicit and keeping them useful.

## The idea

Link a part of your repository to the sources that inspired it. Record what you borrowed,
why it mattered, which upstream revision you examined, and where the idea appears in your
own implementation. Set upstream listeners that watch the relevant code and automatically
open PRs adapting meaningful changes to your repository.

For example, if your cache eviction logic was inspired by another project's implementation,
a fix to that upstream implementation should produce a PR applying the same approach
to your cache code, with regression tests, the original rationale, and the upstream diff.

## What a connection records

- **Inspiration:** a repository, file, pull request, paper, article, or other source.
- **Rationale:** the specific idea that influenced the implementation.
- **Local implementation:** the files or symbols that use the idea.
- **Baseline:** an immutable upstream revision, when available.
- **Watch scope:** the upstream branch and paths whose changes matter.
- **Provenance:** who recorded the connection and the evidence supporting it.

Connections should be authored or confirmed by a person. Automated discovery can suggest
connections, but similarity alone is not proof of influence. Attribution also does not
replace the source's license obligations.

See [the example manifest](examples/memetics.json) for an illustrative connection.
Its repository names, paths, and commit are placeholders.

## Upstream listeners

> If DeepSeek changes how its harness implements something my repo cares about,
> give me a PR implementing it their way.

A listener connects an upstream implementation to a local concern: context compaction,
retry behavior, plugin lifecycle, tool execution, or anything else that matters to the
repository. It tracks relevant changes and automatically implements the upstream approach
through the destination's own architecture and interfaces.

The listener's output is a draft implementation PR with code, tests, and upstream
provenance. Register it once; subsequent relevant changes should not need a fresh request
to investigate or write the patch. The repository owner reviews and merges it.

See the [upstream listener design](docs/upstream-listeners.md) and
[DeepSeek Harness listener example](examples/deepseek-harness-listener.json).
The example pins real upstream code; its destination is illustrative and it is disabled.

## Intended workflow

1. Record an inspiration or a prospective upstream interest in `memetics.json`.
2. Configure a listener with the upstream implementation, the local code that cares,
   the behavior to preserve, and the destination's validation commands.
3. Detect upstream changes through webhooks where available, with polling otherwise.
4. Assess the actual behavioral change and automatically adapt relevant changes in an
   isolated checkout of the destination repository.
5. Run checks and open or update a draft PR containing the implementation, regression
   tests, upstream comparison, and validation evidence.
6. Record adoption on merge, or retain the reason for declining the change.

Start adaptation as soon as a relevant change is detected. Detection, generation, and
checks determine when the PR is ready; polling cannot promise instantaneous delivery.

## Update behavior

An upstream change is evidence to assess against the local concern. Already-adopted or
irrelevant changes receive a recorded decision. Related changes update an existing
proposal; duplicate events do not create duplicate PRs. Observed, proposed, and adopted
revisions remain distinct, and human edits to generated branches must be preserved.

A blocked adaptation retains its reason and any partial patch. Failed checks remain
visible on the draft PR; opening it never implies that tests passed or the change merged.
Private source content must stay within authorized destinations.

Papers and articles can still be recorded as inspirations; the first listener workflow
focuses on GitHub implementations with inspectable revisions and tests.

## Status

Project brief, listener design, and proposed manifests only. No listener, adaptation
worker, or automatic PR service is running yet. The manifest format is exploratory,
not a stable API.
