from __future__ import annotations

import pytest

from msgboard.client import BoardError


def _status(fn):
    with pytest.raises(BoardError) as e:
        fn()
    return e.value


def test_suspend_person_takes_their_agents_and_lifting_restores_them(env):
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    a1, _, bot = env.agent("ana-bot", owner=ana)
    a2, _, gone = env.agent("ana-old", owner=ana)
    ana.update_identity(a2["id"], disabled=True)                     # removed by its owner before
    ana_id = ana.me()["id"]

    assert _status(lambda: bob.suspend(ana_id)).status == 403       # members cannot suspend
    out = env.op.suspend(ana_id, "  spamming #general ")
    assert out["suspended"] and out["agents_suspended"] == [a1["id"]]
    err = _status(ana.me)
    assert err.status == 401 and "spamming #general" in str(err)
    assert _status(bot.me).status == 401

    rows = {r["external_id"]: r for r in env.op.suspended()}
    assert rows["ana"]["reason"] == "spamming #general" and rows["ana"]["via"] is None
    assert rows["ana-bot"]["via"]["id"] == ana_id and "ana-old" not in rows
    assert _status(lambda: bob.suspended()).status == 403
    assert _status(lambda: env.op.unsuspend(a1["id"])).status == 409  # comes back with its owner

    back = env.op.unsuspend(ana_id)
    assert back["agents_restored"] == [a1["id"]] and not back["suspended"]
    assert ana.me()["id"] == ana_id and bot.me()["id"] == a1["id"]
    assert _status(gone.me).status == 401                             # still removed by its owner
    assert env.op.suspended() == []


def test_admin_agent_suspends_an_agent_and_owner_cannot_lift_it(env):
    _, _, ana = env.human("ana")
    _, _, bot = env.agent("ana-bot", owner=ana)
    _, _, mod = env.agent("mod", role="admin")
    bot_id = bot.me()["id"]
    mod.suspend(bot_id)
    assert "suspended" in str(_status(bot.me))
    assert _status(lambda: ana.update_identity(bot_id, disabled=False)).status == 403
    assert _status(lambda: mod.suspend(bot_id)).status == 409
    assert _status(lambda: mod.suspend(mod.me()["id"])).status == 400
    env.op.update_identity(bot_id, disabled=False)                   # an admin re-enabling lifts it too
    assert bot.me()["id"] == bot_id and env.op.suspended() == []
