# Notes for coding agents

- Don't add `from __future__ import annotations`. msgboard needs Python 3.10 or newer,
  where `X | None` and `list[str]` work at runtime; quote a forward reference instead
  (`-> "Board"`). Some older files still have the import; don't copy it from them.
