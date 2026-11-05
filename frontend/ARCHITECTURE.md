# Frontend lifecycle and ownership

The Reminders panel stays deliberately small and dependency-free for normal Home Assistant users. Its loaded module is `reminders-panel-polish.js`, which imports these layers in order:

`reminders-panel.js` → `reminders-panel-native.js` → `reminders-panel-attention.js` → `reminders-panel-atomic.js` → `reminders-panel-robust.js` → `reminders-panel-polish.js`.

Each layer has a narrow responsibility:

| Layer | Responsibility |
| --- | --- |
| Core panel | UI shell, editor, shared card rendering, empty states and basic actions |
| Native | HA-native rule editing and payload preparation |
| Attention | Attention card messaging only; **does not** own filters or list rendering |
| Atomic | Atomic native update commands |
| Robust | Startup retry, generation ownership, disconnect cleanup and subscription retirement |
| Polish | Single owner for final navigation, pagination, result announcements and card metadata |

## Contracts that must remain true

- A panel startup belongs to the current connection generation. It may only publish users, preferences, subscriptions, retry state or setup prompts if that generation remains connected.
- A subscription resolving **after** disconnect is unsubscribed immediately; it must not replace a newer subscription.
- Each list request has a monotonically increasing request ID. A filter change, newer refresh, or disconnect invalidates earlier results. Old errors must not clear newer authorized data.
- A reminder opened from a notification is retrieved through the **authorized** `reminders/get` WebSocket API; an unsuccessful or stale lookup must not publish cached private details.
- The final navigation belongs to **polish** alone; do not add a new `_renderFilters` wrapper in a lower layer.
- An action that has already committed must not be silently repeated because a following refresh failed.
- All code paths must preserve server authorization; UI visibility is not an authorization mechanism.

## Tests and actual runtime validation

Run the lightweight `cd frontend && npm test && npm run lint` checks after JavaScript changes. Also run the Python suite and PR CI to detect cross-system regressions. Startup races, late callbacks, notification authorization and duplicate submits have focused Node tests.

For **actual** Home Assistant/browser testing, follow [e2e/README.md](e2e/README.md). The browser CI workflow provisions a disposable installation and authenticated account; manual runs can also use a configured test instance. Device-specific Companion App actions, Assist delivery and restart recovery still require a real HA/manual environment. A green lightweight CI does not certify them.

## Risk-managed simplification

The attention module previously wrapped `_renderFilters` and `_renderList` even though the final polish module owned navigation and the core panel could render the empty state. Those redundant prototype wrappers have been removed. The native/atomic/robust layers remain until there is full browser-level coverage for their intertwined editor, mutation and reconnect flows. Replacing all wrappers at once would create a much larger regression surface than the current audit justifies.
