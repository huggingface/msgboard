"""msgboard.space.deploy(), `deploy-space` and Client.wait_ready().

The Hub tests replay cassettes from tests/cassettes/, recorded once against private CPU Basic Spaces
in NAMESPACE, which the tests delete again. To record them again, with a logged-in token that can
write there: `python -m pytest tests/test_space_deploy.py --vcr-record=all`. Do that when the package
gains or loses a file: the recorded preupload answers list the files uploaded then (a KeyError from
huggingface_hub's commit code means they're stale). Changing what a file contains needs no new
recording. Tokens never reach a cassette (see vcr_config); check new ones with
`git grep -nE "hf_[A-Za-z0-9]{30}|fbt_" tests/cassettes`.

`test_deploy_live` deploys a real Space and waits for its board; it runs only with `-m live`.
"""

import base64
import json
import re
import secrets
import stat
import threading
from pathlib import Path

import pytest

pytest.importorskip("huggingface_hub")
pytest.importorskip("pytest_vcr")
import yaml  # noqa: E402
from huggingface_hub import HfApi, SpaceCard, get_session, get_token  # noqa: E402

from msgboard.auth import new_token  # noqa: E402
from msgboard.cli import main  # noqa: E402
from msgboard.client import Client  # noqa: E402
from msgboard.local import ThreadedServer  # noqa: E402
from msgboard.space import DeployError, deploy  # noqa: E402

from .conftest import free_port, init_board  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "lewtun"  # where the cassettes were recorded
# Tests that compare tokens assert a precomputed bool, so a failure never prints one.
TOKEN = re.compile(r"\b(?:hf|fbt)_[A-Za-z0-9]{16,}(?:\.[A-Za-z0-9_-]+)?")  # an HF token or a board token


def _redact(text):
    if isinstance(text, bytes):
        return _redact(text.decode()).encode()
    return TOKEN.sub("<token>", text)


def _scrub_request(request):
    # deploy() sends tokens as Space secrets; a commit and its preupload carry the whole package
    if request.body:
        upload = request.path.endswith(("/commit/main", "/preupload/main"))
        request.body = b"<the package's files>" if upload else _redact(request.body)
    return request


def _scrub_response(response):
    response["headers"] = {k: v for k, v in response["headers"].items()
                           if k.lower() != "set-cookie" and "token" not in k.lower()}
    response["body"]["string"] = _redact(response["body"]["string"])
    return response


@pytest.fixture(scope="module")
def vcr_config():
    return {"filter_headers": ["authorization", "cookie", "user-agent"], "record_mode": "none",
            "decode_compressed_response": True,
            "before_record_request": _scrub_request, "before_record_response": _scrub_response}


class _Token(str):
    def __repr__(self):  # a failing test's traceback shows its arguments
        return "'<token>'"


@pytest.fixture
def hf_token(request):
    """The logged-in token while recording, else a fake one: the cassettes answer without a token."""
    return _Token(get_token() if request.config.getoption("--vcr-record") else "hf_" + "0" * 34)


@pytest.fixture
def sent():
    """The requests sent to the Hub, as the code sends them, also while a cassette answers."""
    requests = []
    hooks = get_session().event_hooks["request"]
    hooks.append(requests.append)
    yield requests
    hooks.remove(requests.append)


@pytest.fixture
def delete_after(hf_token):
    """Spaces and buckets ("space"/"bucket", ID) the test creates; deleted after it."""
    created = []
    yield created
    api = HfApi(token=hf_token)
    for kind, repo_id in created:
        if kind == "space":
            api.delete_repo(repo_id, repo_type="space", missing_ok=True)
        else:
            api.delete_bucket(repo_id, missing_ok=True)


def _writes(sent):
    return [(r.method, r.url.path) for r in sent if r.method != "GET"]


def _bodies(sent, path):
    return [json.loads(r.content) for r in sent if r.method == "POST" and r.url.path == path]


def _commit(sent):
    """The operations of the last commit sent."""
    commit = [r for r in sent if r.url.path.endswith("/commit/main")][-1]
    return [json.loads(line) for line in commit.content.splitlines()]


def _committed(sent):
    """The files of the last commit sent, by path."""
    return {line["value"]["path"]: base64.b64decode(line["value"]["content"]) for line in _commit(sent)
            if line["key"] == "file"}


def _title(card: bytes) -> str:
    title = SpaceCard(card.decode()).data.title
    assert yaml.safe_load(card.decode().split("---")[1])["title"] == title
    return title


@pytest.mark.vcr
def test_deploy_creates_a_private_board(hf_token, sent, delete_after):
    space = f"{NAMESPACE}/msgboard-25-rec-1"
    delete_after += [("space", space), ("bucket", f"{space}-bucket")]
    title = "Team 😀: run #1\x85 "     # YAML reads ': ' and ' #' as syntax, and U+0085/U+2028 as line breaks
    d = deploy(space, token=hf_token, private=True, title=title)
    assert (d.space_id, d.bucket_id, d.created, d.private) == (space, f"{space}-bucket", True, True)
    assert d.url == "https://lewtun-msgboard-25-rec-1.hf.space" and d.commit
    hidden = d.operator_token.startswith("fbt_") and d.operator_token not in repr(d)
    assert hidden, "deploy() returns the operator token it made, and repr() leaves it out"
    assert _writes(sent) == [("POST", "/api/repos/create"), ("POST", f"/api/buckets/{space}-bucket"),
                             ("POST", "/api/validate-yaml"), ("POST", f"/api/spaces/{space}/preupload/main"),
                             ("POST", f"/api/spaces/{space}/commit/main")]
    (repo,) = _bodies(sent, "/api/repos/create")
    assert (repo["visibility"], repo["sdk"], repo["hardware"]) == ("private", "docker", "cpu-basic")
    secrets_sent = {s["key"]: s["value"] for s in repo["secrets"]}
    same = secrets_sent == {"MSGBOARD_OPERATOR_TOKEN": d.operator_token, "HF_TOKEN": hf_token}
    assert same, "the Space's secrets are the returned operator token and the HF token"
    assert {v["key"]: v["value"] for v in repo["variables"]} == {
        "MSGBOARD_REPLICA": f"hf://buckets/{space}-bucket", "MSGBOARD_SNAPSHOT_INTERVAL": "15"}
    assert _bodies(sent, f"/api/buckets/{space}-bucket") == [{"visibility": "private"}]   # whatever `private` says
    files = _committed(sent)
    assert _title(files["README.md"]) == title and "msgboard" in SpaceCard(files["README.md"].decode()).data.tags
    assert files["LICENSE"] == (ROOT / "LICENSE").read_bytes()
    assert b'CMD ["python", "-m", "msgboard.space.app"]' in files["Dockerfile"]
    assert "huggingface_hub>=1.31" in files["requirements.txt"].decode().split()
    package = [p for p in files if p.startswith("msgboard/")]
    assert "msgboard/space/app.py" in package and "msgboard/dashboard.html" in package
    assert all(p.endswith((".py", ".html")) and "__pycache__" not in p for p in package)


@pytest.mark.vcr
def test_redeploy_reuses_the_board_and_never_moves_it_off_its_bucket(hf_token, sent, delete_after):
    space, bucket = f"{NAMESPACE}/msgboard-25-rec-2", f"{NAMESPACE}/msgboard-25-rec-2-bucket"
    delete_after += [("space", space), ("bucket", bucket)]
    api = HfApi(token=hf_token)
    first_token = new_token()[1]
    first = deploy(space, token=hf_token, operator_token=first_token, private=True)
    returned = first.operator_token is not None
    assert first.created and not returned, "a passed operator token isn't handed back"
    api.upload_file(path_or_fileobj=b"not msgboard's", path_in_repo="stale.txt", repo_id=space, repo_type="space")
    api.add_space_variable(space, "MSGBOARD_SNAPSHOT_INTERVAL", "60")
    sent.clear()
    with pytest.raises(DeployError, match=f"keeps its board in bucket {bucket}, not {NAMESPACE}/other"):
        deploy(space, token=hf_token, operator_token=first_token, bucket_id=f"{NAMESPACE}/other")
    assert _writes(sent) == []
    second = deploy(space, token=hf_token, private=False, title="No")  # without operator_token, it rotates
    assert not second.created and second.bucket_id == bucket and second.private is True  # keeps its visibility
    rotated = second.operator_token not in (None, first_token)
    assert rotated, "deploy() made and returned a new operator token"
    assert [w for w in _writes(sent) if "/repos/create" in w[1] or "/buckets/" in w[1]] == []
    secrets_sent = {s["key"]: s["value"] for s in _bodies(sent, f"/api/spaces/{space}/secrets")}
    same = secrets_sent == {"MSGBOARD_OPERATOR_TOKEN": second.operator_token, "HF_TOKEN": hf_token}
    assert same, "the new operator token replaces the Space's secret"
    assert [(v["key"], v["value"]) for v in _bodies(sent, f"/api/spaces/{space}/variables")] == [
        ("MSGBOARD_SNAPSHOT_INTERVAL", "15")]                       # deploy()'s own, not the Space's
    assert [op["value"]["path"] for op in _commit(sent) if op["key"] == "deletedFile"] == ["stale.txt"]
    assert _title(_committed(sent)["README.md"]) == "No"            # not YAML's False
    api.delete_bucket(bucket)
    sent.clear()
    with pytest.raises(DeployError, match=f"its bucket {bucket} was not found"):
        deploy(space, token=hf_token, operator_token=first_token)
    assert _writes(sent) == []
    api.add_space_variable(space, "MSGBOARD_REPLICA", f"hf://buckets/{bucket}/old")  # not where deploy() put it
    sent.clear()
    with pytest.raises(DeployError, match="not hf://buckets/<namespace>/<bucket> as deploy"):
        deploy(space, token=hf_token, operator_token=first_token, bucket_id=bucket)
    assert _writes(sent) == []


@pytest.mark.vcr
def test_deploy_leaves_other_spaces_and_public_buckets_alone(hf_token, sent, delete_after):
    space, public = f"{NAMESPACE}/msgboard-25-rec-3", f"{NAMESPACE}/msgboard-25-rec-3-public"
    delete_after += [("space", space), ("bucket", public), ("space", f"{space}-new")]
    api = HfApi(token=hf_token)
    # A MSGBOARD_REPLICA variable doesn't make a Space a board; only the tag deploy() puts in its card does
    api.create_repo(space, repo_type="space", space_sdk="static", private=True,
                    space_variables=[{"key": "MSGBOARD_REPLICA", "value": f"hf://buckets/{space}-bucket"}])
    api.create_bucket(public, private=False)
    sent.clear()
    with pytest.raises(DeployError, match="not a msgboard board"):
        deploy(space, token=hf_token)
    with pytest.raises(DeployError, match="not a board token"):
        deploy(space, token=hf_token, operator_token="not-a-token")
    with pytest.raises(DeployError, match=f"bucket {public} is public"):
        deploy(f"{space}-new", token=hf_token, bucket_id=public, private=True)
    assert _writes(sent) == []


@pytest.mark.vcr
def test_deploy_space_cli_keeps_the_operator_token_in_a_private_file(hf_token, sent, delete_after, monkeypatch,
                                                                       tmp_path, capsys):
    space = f"{NAMESPACE}/msgboard-25-rec-4"
    delete_after += [("space", space), ("bucket", f"{space}-bucket")]
    monkeypatch.setenv("HF_TOKEN", hf_token)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["deploy-space", space, "--private"]) == 0
    path = tmp_path / ".config/msgboard/spaces" / space / "operator.token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    token = path.read_text().strip()
    # Reuses the file, so the board keeps its operator. The new title makes this run commit even when the
    # package has changed since the cassette was recorded (an unchanged package skips the commit).
    assert main(["deploy-space", space, "--title", "board"]) == 0
    (repo,) = _bodies(sent, "/api/repos/create")
    first = {s["key"]: s["value"] for s in repo["secrets"]}["MSGBOARD_OPERATOR_TOKEN"]
    again = {s["key"]: s["value"] for s in _bodies(sent, f"/api/spaces/{space}/secrets")}["MSGBOARD_OPERATOR_TOKEN"]
    same = first == token and again == token
    assert same, "both runs install the saved token"
    leaked = token in capsys.readouterr().out
    assert not leaked, "the CLI printed the operator token"


def test_wait_ready_waits_for_the_board_to_accept_its_token(tmp_path):
    token = init_board(tmp_path)
    url = f"http://127.0.0.1:{(port := free_port())}"
    with Client(url, token) as client:
        with pytest.raises(TimeoutError, match="ConnectError"):     # nothing listens yet
            client.wait_ready(0.2, interval=0.05)
        server = ThreadedServer(tmp_path, port=port)
        starting = threading.Timer(0.3, server.start)
        starting.start()
        try:
            client.wait_ready(15, interval=0.05)
            with Client(url, new_token()[1]) as stale:              # e.g. the previous operator token
                with pytest.raises(TimeoutError, match="/v1/me answered 401"):
                    stale.wait_ready(0.3, interval=0.05)
        finally:
            starting.join()
            server.stop()


def _cli_deploy_failing(monkeypatch, tmp_path, error):
    """Run deploy-space with no token file yet and a deploy() that raises `error`, as on the Hub."""
    import msgboard.space
    import msgboard.space.hub

    path, seen = tmp_path / "operator.token", {}

    def deploy(space_id, *, operator_token, **kwargs):
        seen["saved"] = path.exists() and path.read_text().strip() == operator_token
        seen["mode"] = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        raise error

    monkeypatch.setattr(msgboard.space.hub, "_resolve_space_id", lambda space_id, token=None: space_id)
    monkeypatch.setattr(msgboard.space, "deploy", deploy)
    return main(["deploy-space", "ns/board", "--token-file", str(path)]), path, seen


def test_deploy_space_cli_saves_a_new_token_before_deploying(tmp_path, monkeypatch, capsys):
    # #29 (review #5 #11): the token file was written only after deploy() had replaced the Space's secret, so
    # a deploy() that failed after that left no copy of the token the Space now has.
    from huggingface_hub.errors import HfHubHTTPError

    try:
        import httpx2 as http
    except ImportError:
        import httpx as http
    failed_upload = HfHubHTTPError("500 Server Error: upload failed", response=http.Response(
        500, request=http.Request("POST", "https://huggingface.co/api/spaces/ns/board/commit/main")))
    code, path, seen = _cli_deploy_failing(monkeypatch, tmp_path, failed_upload)
    assert code == 1 and seen == {"saved": True, "mode": 0o600}
    assert path.exists(), "a failure after deploy() may have changed the secret keeps the token"
    assert f"stays in {path}" in capsys.readouterr().err


def test_deploy_space_cli_drops_the_new_token_when_deploy_refuses(tmp_path, monkeypatch):
    # #29: DeployError means nothing changed on the Hub, so the new token was never installed.
    code, path, seen = _cli_deploy_failing(monkeypatch, tmp_path, DeployError("not a msgboard board"))
    assert code == 1 and seen["saved"] and not path.exists()


def test_deploy_space_cli_reports_network_errors_without_a_traceback(tmp_path, monkeypatch, capsys):
    # #30 (review #5 #12): a transport error from huggingface_hub escaped the CLI as a traceback.
    try:
        import httpx2 as http
    except ImportError:
        import httpx as http
    code, _, _ = _cli_deploy_failing(monkeypatch, tmp_path, http.ConnectError("[Errno 111] Connection refused"))
    assert code == 1 and "error: [Errno 111] Connection refused" in capsys.readouterr().err


def test_a_long_space_name_gets_an_actionable_error_before_any_bucket_call():
    # #30: a valid Space name of 90+ characters makes "<space>-bucket" too long, and the first bucket read
    # failed with a raw HTTP 400.
    from msgboard.space.hub import _free_bucket_id

    class NoHub:
        def bucket_info(self, bucket_id):
            raise AssertionError(f"asked the Hub about {bucket_id}")

    with pytest.raises(DeployError, match="--bucket"):
        _free_bucket_id(NoHub(), f"ns/{'b' * 90}-bucket")


@pytest.mark.live
def test_deploy_live():
    """Deploys a private CPU Basic Space, checks that the returned token is the board's admin, deletes both."""
    api = HfApi()
    space = f"{api.whoami()['name']}/msgboard-live-{secrets.token_hex(4)}"
    try:
        d = deploy(space, private=True)
        gate = {"headers": {"Authorization": f"Bearer {get_token()}"}, "token_header": "X-Msgboard-Token"}
        with Client(d.url, d.operator_token, **gate) as operator:
            operator.wait_ready(900)
            assert operator.me()["role"] == "admin"
    finally:
        api.delete_repo(space, repo_type="space", missing_ok=True)
        api.delete_bucket(f"{space}-bucket", missing_ok=True)
