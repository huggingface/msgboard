# Notes for coding agents

- Don't add `from __future__ import annotations`. msgboard needs Python 3.11 or newer,
  where `X | None` and `list[str]` work at runtime; quote a forward reference instead
  (`-> "Board"`). Some older files still have the import; don't copy it from them.
- Don't mention downstream projects (the launchers and orchestrators built on msgboard) by name
  anywhere in this repo: code, docs, tests, commit-tracked data. Say "a launcher" or "an
  orchestrator" instead.

## Tests

Tests exercise real objects: a real board (`tests/conftest.py` starts one), real tokens, the real
Hub client. Don't use `SimpleNamespace`, `MagicMock`, `object.__new__`, hand-written fakes or
monkeypatched internals as stand-ins: they only have what the test author thought of, so the test
keeps passing when the real code changes. Mock only what can't run in the test environment, such
as a remote server. Test through the public API (`deploy()`, `Client`, the CLI), not private
functions. Keep the suite small: one test per behaviour, not one per branch.
Extend an existing behaviour test before adding another. For invalid inputs that must leave the
board unchanged, loop over the cases on one real board and check rejection after each request;
keep separate fixtures for distinct starting states, permissions, restart and concurrency cases.
Avoid fixed sleeps when completion can be observed or explicitly triggered. Run affected tests
while iterating, then the full suite once before review; cosmetic changes need only relevant checks.

### Hub and Space tests

Follow huggingface_hub's own tests (`TestSpaceAPIProduction` and `TestSpaceAPIMocked` in its
`tests/test_hf_api.py`):

- Make real calls where they are cheap.
- Record Space API calls once and replay them with pytest-vcr cassettes in `tests/cassettes/`
  (`--vcr-record=all` to record; the default `record_mode` is `none`, so a test never reaches the
  Hub by accident). Record against private CPU Basic Spaces, and delete everything the test
  creates in the test itself, so the deletions are recorded too.
- Mock expensive server-side actions (hardware, GPUs) at the HTTP-session level, and assert the
  request that was sent.
- At most one opt-in live test, marked `live` (`pytest -m live`), which deploys CPU Basic Spaces
  and deletes what it creates.

Cassettes must not hold tokens. `deploy()` sends the operator token and the HF token as Space
secrets in request bodies, so scrub request and response bodies as well as headers (see
`vcr_config` in `tests/test_space_deploy.py`), and grep every cassette for token-like strings
(`hf_...`, `fbt_...`) before committing. A failing assertion prints the values it compares, so
compare tokens into a bool first.
