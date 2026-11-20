Owned-agent mentions

Source: 42c26e47b458c860947456d52c43c4eca94258f0. Native desktop and touch screenshots use a local board with placeholder people and agents. Full-suite validation is in validation.json and full-suite.log.

PR #47 diagram: sidebar ordering/owner labels, the author-owned channel-member filter, and existing mention storage/read flows. Unchanged activity/notification internals and candidate-directory internals are simplified. One A4 page, black/white, 4.8 mm above footer.

Regenerate with python make_diagram.py, render diagram/pr-47.html with Chromium, and run the shared NASA kit's check.py on the PDF. PR metadata is pinned in diagram/pr.json.

Review fixes: matching group shortcuts remain after seven owned rows; all suggestion kinds scroll keyboard selection into view. Integrated current main (channel groups #46) without conflicts.
