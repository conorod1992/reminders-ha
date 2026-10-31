# Real Home Assistant browser checks (opt-in)

These Playwright checks operate against a **running, authenticated Home Assistant instance with Reminders installed**. They are deliberately not part of PR CI, because CI does not have an HA installation, a Companion App, real Assist devices or user credentials. They are not a substitute for verifying mobile notification delivery on a physical device.

1. On a disposable/test HA installation, install this branch of Reminders and open its management panel once in a browser.
2. From `frontend/`, install the optional test tooling: `npm install --no-save @playwright/test`, then `npx playwright install chromium`.
3. Create a Playwright auth-state file for an authorized test account. Store it outside the repository and **never commit it**. For example, use `npx playwright codegen --save-storage=/private/path/ha-auth.json https://YOUR-HA-HOST/`, log in through the opened browser, then close it.
4. Set `HA_URL` to your HA origin (such as `https://ha.example.test`) and `HA_STORAGE_STATE` to the auth-state file path. Run `npx playwright test -c e2e/playwright.config.mjs` from `frontend/`.
5. Optionally set `HA_E2E_MUTATE=1` to enable the disposable create/delete journey, **only** against an HA installation where test mutations are acceptable.

The suite tests live panel loading, view navigation, search, unsaved form cancellation, keyboard focus, authorized deep-link fallback, and optional create/delete. Failure traces/screenshots are saved in `frontend/e2e/artifacts/`; they may contain private HA state, so treat them as sensitive and do not upload them to public PRs.

Also manually check a *real* mobile Companion App notification link, each selected Assist target, a non-admin user's restricted options, an HA restart during a waiting reminder, and multiple outstanding occurrences. Those workflows cannot be certified by mocked JavaScript tests or browser tests without corresponding devices.

## Validation boundary

The ordinary Node and Python suites cover pure logic and mocked WebSocket/DOM scenarios. This Playwright suite is **opt-in**; it does not claim real-HA coverage until someone runs it against a configured instance. No live HA server or authentication state is available to GitHub CI by default.

CI now provisions a fresh, disposable Home Assistant 2026.9.4 container and runs the authenticated Playwright smoke and create/delete journeys automatically. Its account and auth state exist only on the runner; private traces are not uploaded. `bootstrap.mjs` refuses existing installations and is intended only for that localhost container. Physical phone and Assist checks remain manual.
