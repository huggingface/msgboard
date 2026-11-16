# Independent review: Tasks rename and ordered blocks

Reviewed the current working-tree changes over `e3a3782d45eeeb7c460c3f49c8552e9977e287ad`, including the author's cold-bookmark fix at `dashboard.html:1708`. Read-only review of application source; no commits, external comments, board writes, or deployment actions.

**No remaining concrete blockers found in the reviewed paths.** The review identified that a cold `#queue` bookmark bypassed the legacy redirect; the author fixed this during review. Both bare `#queue` and `#queue/<id>` now pass in separate fresh browser contexts and normalize to their `#tasks` forms.

- **Migration:** independently ran `python -m pytest tests/test_tasks.py -q`: **24 passed in 18.39 seconds**, including constructing a real v30 database and verifying IDs, source/answer references, order, pending notices, receipt replay, unchanged stable-key retries, and foreign-key consistency after v31.
- **Nested controls:** real Chromium/public API setup confirmed a storage link, channel attachment chip, inline image, and internal message link inside a quoted message each open their intended viewer/target. Whole-quote navigation does not hijack them. Space on the quote itself navigates to the quoted message.
- **Block/source privacy:** independently alternated private/public text blocks and quote references through 80 public API upserts while a narrow guest sibling made 103 paired list/item reads. No private marker appeared in any response. `_snapshot` still spans visibility checks and row/source serialization; quote IDs automatically join the same permission-checked source list.
- **Browser errors:** none in the interaction probe. Text/quote ordering, deletion semantics, validation limits, source access checks, and preserved internal idempotency identities were also inspected.

Retained reproducible probe: `check_independent_interactions.py`; its latest output is `independent-interactions.json` in this directory. It uses a real `tests/conftest.py::Env`, real tokens/API calls, and locally pinned rendering libraries, without replacing application internals.

Limits: this review did not rerun the complete repository suite or inspect the deployed Space's private data. It does not claim exhaustive timing, browser, or permission coverage.

## Agent panel resizing — independent follow-up, 16 November

- Fixed: a long draft could push Send below the viewport. The panel now measures the shared composer and grows within the viewport limit.
- Fixed: scrolling the whole composer clipped emoji and mention menus. Only the inner message box scrolls; the menus stay clickable.
- Final independent browser check: no remaining findings for long drafts, short windows, emoji selection, mention selection and real touch dragging. Desktop drag, height persistence, collapse/restore, keyboard arrows and reset also passed the implementation probe.
