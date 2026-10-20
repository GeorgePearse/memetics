# Quickstart

## 1. Add an Anthropic API key

Memetics calls Claude to draft the update. Add an API key as a repo secret:
**Settings → Secrets and variables → Actions → New repository secret**, named
`ANTHROPIC_API_KEY`.

## 2. Describe a pattern

Create `.memetics/<slug>.md` in your repo:

```markdown
---
upstream:
  - repo: owner/upstream-repo
    paths:
      - path/to/tracked/file.ts
    ref: main
    last_seen: null
local:
  paths:
    - src/your/local-implementation.ts
---

# What this pattern is

A short description of the functionality and why it's implemented this way.
Include a screenshot or diagram if that helps a human (or Claude) understand
the shape of the thing.
```

`last_seen: null` means "I haven't checked yet" — the first run will record
the current upstream commit without generating a diff (there's nothing to
diff against yet), then every run after that compares against it.

## 3. Add the workflow

```yaml
# .github/workflows/memetics.yml
name: Memetics sync
on:
  schedule:
    - cron: '0 6 * * 1' # every Monday at 06:00 UTC
  workflow_dispatch: {}

permissions:
  contents: write
  pull-requests: write

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - id: memetics
        uses: GeorgePearse/memetics@main
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}

      - uses: peter-evans/create-pull-request@v6
        if: steps.memetics.outputs.changed == 'true'
        with:
          title: 'Memetics: sync with upstream changes'
          body: ${{ steps.memetics.outputs.summary }}
          branch: memetics/auto-sync
          commit-message: 'Sync with upstream pattern changes (memetics)'
          labels: memetics
```

## 4. Test it without waiting for Monday

Trigger it manually from the **Actions** tab (`workflow_dispatch`), or from
the CLI:

```sh
gh workflow run memetics.yml
```

If nothing in `.memetics/` has an upstream change since its recorded
`last_seen`, the action exits cleanly with `changed=false` and no PR step
runs.
