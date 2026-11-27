# Native macOS / Safari check — not yet performed

Use the PR build in a normal browser window, with enough channels and messages for the sidebar, channel and thread to overflow. This check is for an actual Mac/Safari browser, not Linux WebKit or mobile emulation.

1. Scroll each pane, then leave the pointer over its text. The thumb should fade after one second.
2. Move toward the scrollbar and drag its native thumb. Content should move, and its column width should stay steady.
3. Leave the pointer near the main feed's scrollbar, then press Escape to close the thread. Once scrolling stops, the widened feed's thumb should hide without a pointer move.
4. Repeat by widening the browser window.
5. Click a sidebar control: mouse focus should not keep the thumb visible. Tab through sidebar controls: keyboard focus should reveal it.

Record macOS version, Safari version, system scrollbar setting (Automatically / When scrolling / Always), and the result. If possible, check a current Safari version and a version using the legacy WebKit fallback. Chromium's native-enabled regression does not establish these results.
