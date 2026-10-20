# Meme file format

A **meme file** is one Markdown file per tracked pattern, living under
`.memetics/` (any nesting depth — the default scan glob is
`.memetics/**/*.md`). It has YAML frontmatter for the machine-readable
tracking state, and a Markdown body for the human- (and Claude-) readable
description.

```markdown
---
upstream:
  - repo: owner/upstream-repo
    paths:
      - src/the/tracked/file.ts
    ref: main
    last_seen: a1b2c3d4e5f6...
local:
  paths:
    - src/your/local-implementation.ts
---

# Pattern name

Free-form description. Include images, rationale, adaptation notes — anything
that helps a future reader (or Claude, when it drafts the update) understand
*why* the local version looks the way it does.
```

## Frontmatter fields

### `upstream` (required, list)

One entry per upstream source. A pattern can track **one upstream** (a 1:1
port of a specific implementation) or **several** (the same idea, watched
across multiple frameworks — Memetics reconciles against whichever one
actually changed).

| Field | Required | Description |
|---|---|---|
| `repo` | yes | `owner/name` on GitHub. |
| `paths` | yes | List of file paths within that repo to watch. The most recent commit touching *any* of them is used as the upstream "version." |
| `ref` | no (default `main`) | Branch, tag, or ref to track. |
| `last_seen` | yes (may be `null`) | The last commit SHA Memetics diffed against. Set to `null` on first write — the first run records the current HEAD without generating a diff, since there's nothing to compare yet. Memetics updates this field in place after every successful sync; commit the updated file. |

### `local` (required, object)

| Field | Required | Description |
|---|---|---|
| `paths` | yes | List of local files that implement this pattern. These are the files Claude is given as context and allowed to rewrite. |

## Body (Markdown)

Everything after the closing `---` is free-form Markdown, passed to Claude
verbatim as the "pattern description." Use it for:

- **What the pattern is** and why you reimplemented it instead of depending
  on it directly (license, framework mismatch, simplicity).
- **Diagrams or screenshots** — reference images the same way you would in
  any Markdown file (`![caption](./diagram.png)`). Keep images alongside the
  meme file.
- **Adaptation notes** — anything that should survive an upstream change:
  "keep this API stable even if upstream's internals change," "we only
  support the fixed-size variant," etc. This is the single highest-leverage
  thing you can write, since it's what keeps Claude's generated update from
  drifting away from decisions you made on purpose.

## A note on `last_seen: null`

Setting `last_seen: null` means "start tracking from here" rather than
"diff against empty." On the first run, Memetics records the current
upstream commit as the baseline and does **not** generate a PR — there is no
"before" to diff against yet. Every run after that compares real history.
