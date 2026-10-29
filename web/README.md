# Kura web

SvelteKit frontend for browsing images, pools and tags. Node 24 and npm are used by CI.

```sh
cd web
npm ci
npm run check
npm run build
```

`npm run check` checks TypeScript and Svelte. Existing warnings remain visible; errors fail the check. CI also builds the application and runs the isolated browser regressions.

## Local development

Run `npm run dev` with `INTERNAL_API_URL` set to the backend. Signed-in routes require the deployment's `KURA_SESSION_SECRET`. Keep credentials in the node-owned environment, outside Git; never expose the session secret through a `PUBLIC_` variable.

For isolated worktrees, install dependencies in that worktree. Sharing `node_modules` by symlink can cause Vite's filesystem boundary to refuse client modules even while SSR renders a convincing page.

## Browser regressions without production data

From the repository root, in a Python environment of your choice:

```sh
python -m pip install -r web/tests/requirements.txt
python -m playwright install chromium
python web/tests/ui-regressions.py
python web/tests/mobile-regressions.py
python web/tests/manga-regressions.py
```

The UI runner starts an isolated Vite server and a synthetic GET-only API on loopback ports 5179 and 5180. It supplies a test-only session, intercepts browser API/media requests, and fails if the browser attempts external requests or unapproved mutations. The two delayed-edit cases fulfill only their exact local mutation method/path in memory; those requests never reach an API. No database, production credential or live image fixture is needed. Both servers are stopped on exit. `KURA_UI_BROWSER_CHANNEL=chrome` selects an already-installed Chrome instead of Playwright Chromium.

UI cases cover empty-pool request storms, stale search and annotation responses, two-column persistence, mobile viewer navigation, Japanese composition, sorting, and Pool Back/Forward restoration. History cases include delayed rows, inserted items, repeated URLs and wheel/keyboard takeover. Evidence and synthetic screenshots are written under `/tmp/kura-ui-*`.

The mobile runner uses separate loopback ports 5183/5184. It checks tag-panel collapse, touch inclusion/exclusion, stale sidebar searches, keyboard suggestion activation, labelled sign-in inputs and scrolling on short screens.

The manga runner uses ports 5185/5186. It samples motion during wheel/drag input in both reading directions, including delayed images, missing dimensions, real append/prepend/trim, input takeover and teardown. Run suites sequentially in a worktree; do not build or run another Vite instance in that worktree concurrently. For a focused investigation, select exact case names from `tests/manga-regressions.py`, for example:

```sh
KURA_MANGA_CASES=queued_navigation_takeover_rtl,ctrl_wheel_cancels_pending_correction python web/tests/manga-regressions.py
```

The manga JSON report defaults to `/tmp/kura-manga-regressions-results.json` (`KURA_BROWSER_EVIDENCE` overrides it). Its `kura.browser-verification/v1` envelope records source commit/dirty state, browser, generated inputs, selected cases and failures. CI retains this report as `manga-browser-verification`; UI/mobile reports currently use their older format and are not included in that artifact. A filtered run is not the full suite. CI may test a generated merge commit: retain that identity rather than relabelling it as the reviewed branch commit. Automated input and mobile emulation do not establish physical-device or user confirmation; those remain separate evidence.

## Investigating position jumps and history loss

Single-image viewing has one renderer, `src/lib/components/ImageViewer.svelte`. The grid retains its list and opens it with SvelteKit shallow history at a `/post/{id}` URL; `ImagePage.svelte` supplies the same renderer for a direct URL or reload. Each adapter owns its data loading and close destination. Neighbor navigation replaces the viewing entry, so closing an overlay returns to its original grid once. Legacy `lightbox` preferences normalize to `single`; `?lb=` links still open. Details, related-pool images, tags, pair comparison and video thumbnail controls belong to the shared renderer. Delayed writes reconcile by captured post ID, never by the index that happens to be visible on completion.

| Symptom | Start here | Reproduction boundary |
| --- | --- | --- |
| Manga moves back or skips after new input | `src/lib/mangaScroll.ts` and its caller in `src/routes/post/[id]/+page.svelte` | Hold an image or page response, start a newer input, then release it. Inspect the trajectory and idle position, not only the final page. |
| Pool Back returns to the top or the wrong item | `src/routes/+page.svelte`: `snapshot`, `gridMetrics`, `cancelHistoryRestore` | Restore virtual extent before position; compare item ID, screen offset and scroll position after the Pool rows arrive. Repeat Back/Forward and the same URL as distinct history entries. |
| Test passes locally but fails with slow loading | `tests/ui-regressions.py`: `pool_history`; manga held-response fixtures | Wait for the expected view's visible rows with a bounded timeout before asserting exact position. URL completion or a fixed sleep alone does not mean virtual rows are ready. |

Manga deferred positioning belongs to the input controller's generation: newer operations cancel stale timers/frames. Keep geometry compensation in the viewer and rebase an active drag by the actual scroll movement after trim/clamping. Browser snapping can also move the viewport, so inspect snap state when an old correction appears to survive cancellation.

History restoration uses a descriptor per SvelteKit history entry, not one saved coordinate per query. Delayed restoration must yield to wheel, touch, pointer and scrolling keys without suppressing normal input behavior. Nearby item reconciliation is bounded; removed or far-moved items fall back to the nearest saved index. Adding `target="_blank"` hides a broken Back path and does not repair restoration. Regression examples and review history: PR #138 (manga lifecycle) and PR #139 (Pool history).

The older `view-smoke.py` and `p*-interactions.py` scripts use deployment credentials and frozen live fixtures. Their golden files must not be updated automatically; see `fixtures.py`.

## Shipping a frontend change

Follow the repository's `CLAUDE.md` review/version rules. The checkout timer does not build the frontend: after the reviewed revision reaches the service checkout, run `npm ci`, `npm run build`, then restart `kura-web`. Verify the frontend's **menu → Build** commit and inspect the actual page. `/api/build` identifies the backend build, not the frontend bundle.
