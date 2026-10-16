# Material Symbols subset

`material-symbols-outlined.woff2` contains the 80 icon names in
`material-symbols-outlined.txt`, including icons returned by the status,
device and action helpers. The subset is 44,248 bytes. Its SHA-256 is
`2e43bbf3f2a40e91907a4257c3def5b1893a848a023e40a4b19516ff78b01d5d`.

Source: Google Fonts Material Symbols Outlined, version 377, retrieved
2026-10-11. Licensed under Apache-2.0; see `LICENSE.txt`.
The variable axes cover the app's existing styles: optical size 20–24,
weight 300–400, fill 0–1, grade 0. Text fonts remain on Google Fonts.
The local icon face uses `font-display: block` to avoid ligature-text flash.

To regenerate, update the sorted comma-separated icon list first, then run
from `apps/showcase_ui`:

```sh
icons=$(cat public/fonts/material-symbols-outlined.txt)
curl -fsSL -A 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36' \
  --get --data-urlencode 'family=Material Symbols Outlined:opsz,wght,FILL,GRAD@20..24,300..400,0..1,0' \
  --data-urlencode "icon_names=$icons" --data-urlencode 'display=block' \
  https://fonts.googleapis.com/css2 -o /tmp/material-symbols-subset.css
url=$(sed -n 's/.*url(\([^)]*\)).*/\1/p' /tmp/material-symbols-subset.css | head -1)
curl -fsSL "$url" -o public/fonts/material-symbols-outlined.woff2
npm run test:icon-font
sha256sum public/fonts/material-symbols-outlined.woff2
```

Update the version, size and digest here after regenerating. The Google Fonts
endpoint can change; the committed bytes, not that endpoint, pin the runtime
asset. Check every manifest glyph in the browser audit after `document.fonts.ready`.
