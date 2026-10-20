# memetics

Link your code to the inspirations behind it, and track changes to their upstream implementations.

Dependabot watches dependency *versions*. Memetics watches implementation
*patterns* — the ideas you reimplemented instead of importing, with no
automated way to notice when the original drifts.

Full docs: **https://georgepearse.github.io/memetics/**

*(`remix` would also be a pretty good name for this — it's all about taking other people's ideas and mixing them into your own.)*

## In short

1. Describe a bit of functionality in `.memetics/<slug>.md` — what it is,
   optionally an image, and which upstream repo(s)/path(s) it's based on.
2. Add the `GeorgePearse/memetics` action to a scheduled workflow.
3. When something changes at the tracked upstream location, the action asks
   Claude to draft an updated local implementation and opens a PR via
   `peter-evans/create-pull-request`.

```yaml
# .github/workflows/memetics.yml
on:
  schedule:
    - cron: '0 6 * * 1'
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
```

See [memetics-demo](https://github.com/GeorgePearse/memetics-demo) for a
self-contained, controllable end-to-end example, and the
[docs](https://georgepearse.github.io/memetics/) for the full meme file
format and how detection/drafting works.
