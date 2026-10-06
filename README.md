# Memetics

Trace the ideas behind your code, and keep up when their implementations evolve.

A GitHub repository depends on more than the packages in its lockfile. Its architecture,
algorithms, APIs, and small implementation details often come from other repositories,
papers, articles, and conversations. Those connections usually disappear into browser
history, chat transcripts, or a passing comment.

Memetics is a project for making those connections explicit and keeping them useful.

## The idea

Link a part of your repository to the sources that inspired it. Record what you borrowed,
why it mattered, which upstream revision you examined, and where the idea appears in your
own implementation. Watch the relevant upstream code and surface changes worth reviewing.

For example, if your cache eviction logic was inspired by another project's implementation,
a fix to that upstream implementation should lead you back to your own cache code, with the
original rationale and a link to the upstream diff.

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

## Intended workflow

1. Add a `memetics.json` manifest to a repository.
2. Record the sources behind important implementation decisions and pin code sources to
   the revisions you actually used.
3. Check the watched upstream paths for changes since the last acknowledged revision.
4. Receive an update containing the upstream comparison, the original rationale, and the
   local code that may need attention.
5. Review the change and record whether you adopted it, dismissed it, or deferred it.

The first implementation should support GitHub code sources and scoped commit checks.
Papers and articles can be recorded as inspirations from the start; detecting changes to
those sources needs a separate mechanism.

## Update behavior

An upstream change is a prompt to investigate, not a claim that your implementation is
broken. Updates should group changes by inspiration and avoid reporting the same revision
repeatedly. Checking for a change must not silently mark it as reviewed.

A future scheduled GitHub Action or GitHub App could deliver a review queue through GitHub
issues. Private sources must remain accessible only to authorized users, and notification
content must not expose private source details in a public destination.

## Status

Project brief and proposed manifest only. There is no watcher, scheduled job, or notification
service running yet. The manifest format is exploratory, not a stable API.
