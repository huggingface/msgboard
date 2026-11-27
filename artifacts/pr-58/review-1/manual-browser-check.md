# Native Safari verification — PR #58

Status: **unverified**. This Linux workspace has Chromium but no native macOS or iOS Safari.

Record device, OS, Safari version and source commit. On a disposable board with image previews above a long conversation:

- Enter the channel while images load: the bottom should remain visible.
- Scroll up before a delayed image finishes: the reading line should stay within a few pixels.
- Open and close a thread while a preview or a long code block is visible: no chip flash or reading jump.
- Flick-scroll while previews load: momentum should continue without abrupt stops.
- Delete a sole reply from its open thread and edit out a selected body block: the channel should retain its offset.

Do not mark the review finding verified using Chromium or emulated mobile evidence alone.
