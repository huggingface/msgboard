# PR 36 review evidence

Source: `8052b48927b0612b562e939c9125a2ea07e4fd1d` on `browser-downloads`, based on main `563911d`.

The diagram shows the actual download path and the boundary between preparation and native transfer. SQLite and the file cache are existing parts, simplified to drums; previews retain their existing Blob cache.

The full source suite passed: **253 tests**, one opt-in live test deselected. The final source relocation for clean integration with PR 35 was checked again with the download tests and the combined browser run.

`browser-baseline-proxy-results.json` and `browser-final-results.json`: real local msgboard, Chromium headless shell, actual HTTP proxy paced at 1 MiB/s for a 16 MiB file. The fixed run deliberately holds the real preparation response for 0.5 seconds. This measures time until the browser owns the download, not faster network throughput. Downloaded bytes are SHA-256 checked. A repeat click is dispatched while preparation is pending; one native GET and one browser download result.

`browser-combined-results.json`: temporary test merge with PR 35 at `1ae5008`, including a native image download in the dark viewer. This merge is not part of the source branch. No JavaScript errors. This run repeats the proxy check and verifies deleted-file error feedback and retry.

Screenshots use synthetic files on an isolated local board. Phone screenshots are a 375-pixel Chromium viewport, not a physical-device test. No live board was modified, deployed or restarted. No cold cache was forced on production.

The first suite attempt with a new package module failed three recorded Space deployment tests (the cassette lacked that new upload filename). Keeping the small capability store in the existing authentication module resolved those failures; the subsequent full suite passed without cassette/network changes.

Review follow-up at `ecd82585e1dcf10b3dc67e0da8814156b3208cb3`: documented reusable bearer capability semantics, returned a dashboard-relative URL, asserted that contract in the existing API test. The six download tests and Ruff pass again. `path-prefix-results.json` records a real board behind a strict `/board/` HTTP proxy: Chromium saved matching bytes from a native download URL that retained that prefix, with no page errors. The standalone reproduction uses the real-board fixture and `slow_proxy.py` from the durable investigation folder. Preview Blob retention remains a separate follow-up.
