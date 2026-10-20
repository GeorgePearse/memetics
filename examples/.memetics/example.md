---
upstream:
  - repo: tanstack/virtual
    paths:
      - packages/virtual-core/src/index.ts
    ref: main
    last_seen: null
local:
  paths:
    - src/components/virtual-list.ts
---

# Virtual scrolling

We reimplement windowed/virtual scrolling for large lists, following the
overscan + item-size-cache approach used by `tanstack/virtual`.

![overscan diagram](./virtual-scrolling-overscan.png)

## Notes

- We only care about the fixed-size-item variant, not variable-size.
- Keep our API surface (`createVirtualList(items, itemHeight)`) stable even
  if upstream changes internals.
