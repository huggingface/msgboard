All three findings from review #1 are addressed:

1. **Cleared draft / stale Tab:** Send closes the suggestion list before posting and again when it clears the composer. Accepting any suggestion checks that both the draft and caret still match the captured token.
2. **Caret navigation:** PageUp, PageDown and modified vertical arrows reconcile suggestions after moving the caret. A stale list cannot insert into an earlier position or let Enter accidentally send.
3. **URL safety:** Emoji suggestions reject the enclosing URL/path token, including schemes, protocol-relative URLs, relative paths and `www.` links. Parenthesized prose still completes normally.

The same three real-board browser tests now cover these regressions; no additional board fixtures were introduced. Native selection/undo/redo and editor positioning also pass in Chromium and Firefox, with no page errors. Before/after reproduction results are attached alongside the updated change sheet.
