## Preview UI URLs and storage

The normal UI uses `<base href="/">`. Build a PR UI with:

```sh
cd apps/showcase_ui
npm ci
npm run build:preview -- 70 <full-source-commit-sha>
```

The command validates the PR number and full SHA, passes Angular's native
`--base-href /preview/pr/70/`, and writes source metadata into the built index.
The visible banner identifies the PR and seven-character SHA. The SHA is a build
label, not admission evidence: the later trusted builder must bind it to the
checked-out candidate. A prefixed build without valid metadata says `unavailable`.

`appUrl` resolves from `document.baseURI`. The HttpClient interceptor covers all
services, including version and What's New. Native SSE (including `scope=all`),
relay WebSocket URLs, live video, copied run links, enrollment commands, screenshots,
recording segments and summary links use the same resolver. It rejects external
API/media origins, credentialed URLs, traversal and another PR's prefix. Approved
external links (such as font stylesheets and ordinary Markdown links) are unchanged.
Image uploads and same-origin blob downloads remain supported.

All browser storage goes through `BrowserStorageService`. Root keys remain
compatible. Nested keys include the application prefix and server-returned identity;
an unknown/failed identity cannot read, write or delete preview preferences. Open
mode has an explicit `open` namespace. Session caches retain their owner checks.
An identity change clears cached and displayed sessions, closes the old stream and
invalidates stale list/status responses. Selected device/profile preferences and
What's New/log settings are isolated too. On the audited base
`d5095edbc0c82e8e67249ddb9a7f7d8f57b3c44e`, OwnerScope's All users switch is an
in-memory signal: there is no owner-scope storage key to migrate or persist.

Checks:

```sh
npx ng test --watch=false --browsers ChromeHeadless
npm run build -- --configuration development
npm run test:preview-path
```

The browser check serves disposable synthetic HTTP fixtures at both `/` and
`/preview/pr/70/`. It exercises real Angular consumers and checks for zero root-site
API/media requests from the nested UI. It opens only a fixture WebSocket; no device
is connected. Fixture identities and SHA labels are not live authentication or
exact-head admission evidence.

This layer does not deploy, register a service worker, modify ingress/Access,
configure ASGI `root_path`, disable legacy `/admin`/`/debug`, or call providers.
Those backend/runtime controls belong to later layers. Path storage namespacing
prevents accidental leakage; it is not a same-origin security boundary. This PR
does not authorize any live preview.
