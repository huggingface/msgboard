Channel groups - PR #46

Source: f8a6245ff7ea6b21a216a14e0173f8c2eb22e4bd. Screenshots come from the real local desktop/touch test board with placeholder accounts and content.

The diagram shows the changed sidebar, existing preferences API/storage, and the visible-channel filter used for collapsed counts. Ordinary channel-list internals are simplified. One A4 page, pure black/white, 8.5 mm above footer.

Regenerate HTML: python make_diagram.py (uses the shared NASA kit). Render diagram/pr-46.html with Chromium/Playwright, then run the kit check.py on the PDF. Metadata is pinned in diagram/pr.json.
