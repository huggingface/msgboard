"""msgboard.space.deploy(), `deploy-space` and Client.wait_ready().

The Hub tests replay cassettes from tests/cassettes/, recorded once against private CPU Basic Spaces
in the namespaces below, which the tests delete again. To record them again, with a logged-in token that can
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
NAMESPACE = "lvwerra"  # deployment-success cassettes
FAILURE_NAMESPACE = "lewtun"  # CLI-failure and invalid-name cassettes
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
    space = f"{NAMESPACE}/msgboard-tasks-rec-1"
    delete_after += [("space", space), ("bucket", f"{space}-bucket")]
    title = "Team 😀: run #1\x85 "     # YAML reads ': ' and ' #' as syntax, and U+0085/U+2028 as line breaks
    d = deploy(space, token=hf_token, private=True, title=title)
    assert (d.space_id, d.bucket_id, d.created, d.private) == (space, f"{space}-bucket", True, True)
    assert d.url == "https://lvwerra-msgboard-tasks-rec-1.hf.space" and d.commit
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
    space, bucket = f"{NAMESPACE}/msgboard-tasks-redeploy-v31", f"{NAMESPACE}/msgboard-tasks-redeploy-v31-bucket"
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
    space, public = f"{NAMESPACE}/msgboard-tasks-rec-3", f"{NAMESPACE}/msgboard-tasks-rec-3-public"
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
    space = f"{NAMESPACE}/msgboard-tasks-rec-4"
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


@pytest.mark.vcr
def test_deploy_space_cli_keeps_a_new_token_when_the_upload_fails(hf_token, sent, delete_after, monkeypatch,
                                                                     tmp_path, capsys):
    # #29 (review #5 #11): the CLI wrote a new token's file only after deploy() had made it the Space's
    # secret, so a deploy() that failed after that left no copy of it. #30 (review #5 #12): a network error
    # from huggingface_hub escaped as a traceback.
    space = f"{FAILURE_NAMESPACE}/msgboard-27-rec-1"
    delete_after += [("space", space), ("bucket", f"{space}-bucket")]
    monkeypatch.setenv("HF_TOKEN", hf_token)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config/msgboard/spaces" / space / "operator.token"

    def connection_reset(request):  # fail as upload starts, after the Space got its secrets
        if request.url.path.endswith("/preupload/main"):
            # Fail before the recorded package inventory, which changes when files are added.
            # Unlike ConnectError, TransportError does not retry with a fresh session that loses this hook.
            raise _http().TransportError("[Errno 104] Connection reset by peer", request=request)

    hooks = get_session().event_hooks["request"]
    hooks.append(connection_reset)
    try:
        assert main(["deploy-space", space, "--private"]) == 1
    finally:
        hooks.remove(connection_reset)
    (repo,) = _bodies(sent, "/api/repos/create")
    installed = {s["key"]: s["value"] for s in repo["secrets"]}["MSGBOARD_OPERATOR_TOKEN"]
    kept = path.exists() and path.read_text().strip() == installed
    assert kept, "the file keeps the token the Space now has"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    err = capsys.readouterr().err
    assert "error: [Errno 104] Connection reset by peer" in err and "Traceback" not in err


@pytest.mark.vcr
def test_deploy_space_cli_drops_a_new_token_when_deploy_refuses(hf_token, delete_after, monkeypatch, tmp_path):
    # #29: the new token's file is written before deploy() asks the Hub anything, and removed when deploy()
    # refuses, which changes nothing.
    space = f"{FAILURE_NAMESPACE}/msgboard-27-rec-2"
    delete_after += [("space", space)]
    monkeypatch.setenv("HF_TOKEN", hf_token)
    monkeypatch.setenv("HOME", str(tmp_path))
    HfApi(token=hf_token).create_repo(space, repo_type="space", space_sdk="static", private=True)  # not a board
    path = tmp_path / ".config/msgboard/spaces" / space / "operator.token"
    on_disk = []
    hooks = get_session().event_hooks["request"]
    hooks.append(lambda request: on_disk.append(path.exists()))
    try:
        assert main(["deploy-space", space]) == 1
    finally:
        hooks.pop()
    assert on_disk and on_disk[0], "saved before deploy() asked the Hub about the Space"
    assert not path.exists(), "removed once deploy() refused"


@pytest.mark.vcr
def test_deploy_refuses_a_space_name_too_long_for_its_bucket(hf_token, sent):
    # #30 (review #5 #12): a valid Space name of 90+ characters makes "<space>-bucket" too long for a repo
    # name, and the first bucket call failed with a raw HTTP 400.
    space = f"{FAILURE_NAMESPACE}/msgboard-27-rec-3-{'x' * 75}"
    with pytest.raises(DeployError, match="--bucket"):
        deploy(space, token=hf_token, private=True)
    asked = [r.url.path for r in sent if r.url.path.startswith("/api/buckets/")]
    assert asked == [], "refused before asking the Hub about the bucket"


def _http():
    """The HTTP library huggingface_hub uses: httpx2 since huggingface_hub 2, httpx before."""
    try:
        import httpx2
        return httpx2
    except ImportError:
        import httpx
        return httpx


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
