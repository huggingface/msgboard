# Channel transfer PR #17

Source: ae88ee41eea1339a40d46e2395b9358049618c09 (channel-transfer). Companion dashboard: PR #16, 35d59b485150adc456e174fc17e67e4126859892.

The A4 sheet combines SQLite and file backends into drums and shows the export/validation/publication path and digest replay mechanism. Files come from the actual diff in transfer.py, server.py, store.py and db.py; limits match transfer.py and config.py.

Validation: 206 tests pass on the source head; Ruff and whitespace checks pass. Real local export/import with the companion dashboard confirms full/grouped/thread source links, reactions, the history notification guard, and an image served from a copied ghost-owned storage folder. The screenshots use temporary local test data.

Diagram validation: one A4 page, black and white only, 8.6 mm fit above the footer, embedded Routed Gothic/Letter Gothic fonts. Rendered using Playwright Chromium after the kit's command-line Chrome renderer stalled in this workspace.

No real boards deployed and no real channel history imported. Team board's schema decision remains with Leandro.
