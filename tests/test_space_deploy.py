"""msgboard.space.deploy() against a fake HfApi, Client.wait_ready() and `deploy-space`."""

import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("huggingface_hub")
from huggingface_hub.errors import BucketNotFoundError, RepositoryNotFoundError  # noqa: E402

from msgboard.auth import new_token  # noqa: E402
from msgboard.cli import main  # noqa: E402
from msgboard.client import Client  # noqa: E402
from msgboard.space import DeployError, deploy, hub  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOT_FOUND = SimpleNamespace(headers={}, request=None, status_code=404)


class FakeHub:
    """The HfApi calls deploy() makes, in memory. `writes` records every call that changes something."""

    def __init__(self, user="alice"):
        self.user = user
        self.spaces: dict[str, dict] = {}
        self.buckets: dict[str, bool] = {}   # id -> private
        self.writes: list[tuple] = []

    def add_space(self, space_id, *, tags=(), private=False, variables=None, files=None):
        self.spaces[space_id] = {"tags": list(tags), "private": private, "variables": dict(variables or {}),
                                 "secrets": {}, "files": dict(files or {".gitattributes": b"", "README.md": b"x"})}

    def whoami(self):
        return {"name": self.user}

    def space_info(self, space_id):
        if space_id not in self.spaces:
            raise RepositoryNotFoundError(space_id, response=NOT_FOUND)
        s = self.spaces[space_id]
        return SimpleNamespace(id=space_id, tags=s["tags"], private=s["private"], subdomain=space_id.replace("/", "-"),
                               host=f"https://{space_id.replace('/', '-')}.hf.space")

    def get_space_variables(self, space_id):
        return {k: SimpleNamespace(key=k, value=v) for k, v in self.spaces[space_id]["variables"].items()}

    def bucket_info(self, bucket_id):
        if bucket_id not in self.buckets:
            raise BucketNotFoundError(bucket_id, response=NOT_FOUND)
        return SimpleNamespace(id=bucket_id, private=self.buckets[bucket_id])

    def list_repo_files(self, space_id, repo_type):
        return list(self.spaces[space_id]["files"])

    def create_repo(self, space_id, **kw):
        self.writes.append(("create_repo", space_id, kw))
        self.add_space(space_id, private=bool(kw["private"]),
                       variables={v["key"]: v["value"] for v in kw["space_variables"]})
        self.spaces[space_id]["secrets"] = {s["key"]: s["value"] for s in kw["space_secrets"]}

    def add_space_secret(self, space_id, key, value):
        self.writes.append(("secret", space_id, key))
        self.spaces[space_id]["secrets"][key] = value

    def add_space_variable(self, space_id, key, value):
        self.writes.append(("variable", space_id, key, value))
        self.spaces[space_id]["variables"][key] = value

    def create_bucket(self, bucket_id, *, private, exist_ok):
        self.writes.append(("create_bucket", bucket_id, private))
        self.buckets.setdefault(bucket_id, private)

    def create_commit(self, space_id, *, repo_type, operations, commit_message):
        self.writes.append(("commit", space_id, commit_message))
        files = self.spaces[space_id]["files"]
        for op in operations:
            if hasattr(op, "path_or_fileobj"):
                files[op.path_in_repo] = op.path_or_fileobj
            else:
                del files[op.path_in_repo]
        if b"\n  - msgboard\n" in files["README.md"]:
            self.spaces[space_id]["tags"] = ["msgboard", "docker"]
        return SimpleNamespace(oid="c0ffee" * 6 + "abcd")


@pytest.fixture
def fake(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(hub, "_hf_api", lambda token: fake)
    monkeypatch.setenv("HF_TOKEN", "hf_launcher")
    return fake


def test_new_space_gets_a_private_bucket_secrets_and_the_installed_package(fake):
    operator = new_token()[1]
    result = deploy("board", operator_token=operator, private=False)
    assert result.space_id == "alice/board" and result.created and result.private is False
    assert result.url == "https://alice-board.hf.space" and result.bucket_id == "alice/board-bucket"
    assert result.commit.startswith("c0ffee")
    assert ("commit", "alice/board", f"Deploy msgboard {hub._distribution().version}") in fake.writes
    (_, _, repo), *_ = [w for w in fake.writes if w[0] == "create_repo"]
    assert repo["private"] is False and repo["space_sdk"] == "docker" and repo["space_hardware"] == "cpu-basic"
    assert ("create_bucket", "alice/board-bucket", True) in fake.writes      # private, whatever `private` says
    space = fake.spaces["alice/board"]
    assert space["secrets"] == {"MSGBOARD_OPERATOR_TOKEN": operator, "HF_TOKEN": "hf_launcher"}
    assert space["variables"] == {"MSGBOARD_REPLICA": "hf://buckets/alice/board-bucket",
                                  "MSGBOARD_SNAPSHOT_INTERVAL": "15"}
    files = space["files"]
    card = files["README.md"].decode()
    assert 'title: "board"\n' in card and "hf_oauth: true\n" in card and "\n  - msgboard\n" in card
    assert files["LICENSE"] == (ROOT / "LICENSE").read_bytes()
    assert b'CMD ["python", "-m", "msgboard.space.app"]' in files["Dockerfile"]
    requirements = files["requirements.txt"].decode().split()
    assert "fastapi>=0.110" in requirements and "huggingface_hub>=1.31" in requirements
    assert not any(r.startswith("pytest") for r in requirements)
    for path in ("msgboard/__init__.py", "msgboard/dashboard.html", "msgboard/space/app.py",
                 "msgboard/space/accounts.py", ".gitattributes"):
        assert path in files
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in files)


def test_new_space_skips_buckets_left_behind(fake):
    fake.buckets.update({"alice/board-bucket": True, "alice/board-bucket-2": True})
    result = deploy("alice/board", operator_token=new_token()[1])
    assert result.bucket_id == "alice/board-bucket-3"
    assert fake.spaces["alice/board"]["variables"]["MSGBOARD_REPLICA"] == "hf://buckets/alice/board-bucket-3"
    assert fake.writes[0][2]["private"] is None          # the namespace's default


def test_redeploy_reuses_the_space_and_its_bucket(fake):
    first = deploy("alice/board", operator_token=new_token()[1], private=True)
    fake.spaces["alice/board"]["files"]["hfspace/app.py"] = b"old layout"
    fake.writes.clear()
    second_token = new_token()[1]
    second = deploy("alice/board", operator_token=second_token, private=False)
    assert not second.created and second.bucket_id == first.bucket_id
    assert second.private is True                        # an existing Space keeps its visibility
    assert not any(w[0] in ("create_repo", "variable") for w in fake.writes)
    assert fake.spaces["alice/board"]["secrets"]["MSGBOARD_OPERATOR_TOKEN"] == second_token
    files = fake.spaces["alice/board"]["files"]
    assert "hfspace/app.py" not in files and ".gitattributes" in files


def test_a_space_from_the_old_deploy_script_keeps_its_replica(fake):
    fake.add_space("lvwerra/demo", variables={"MSGBOARD_REPLICA": "hf://buckets/lvwerra/demo-data/demo",
                                              "MSGBOARD_SNAPSHOT_INTERVAL": "30"})
    fake.buckets["lvwerra/demo-data"] = True
    result = deploy("lvwerra/demo", operator_token=new_token()[1], bucket_id="lvwerra/demo-data")
    assert result.bucket_id == "lvwerra/demo-data"
    assert fake.spaces["lvwerra/demo"]["variables"] == {"MSGBOARD_REPLICA": "hf://buckets/lvwerra/demo-data/demo",
                                                         "MSGBOARD_SNAPSHOT_INTERVAL": "30"}


def test_an_interrupted_first_deploy_gets_its_bucket_before_any_secret_changes(fake):
    # create_repo() ran (default card, no tag yet, variables set), then deploy() stopped before the bucket
    fake.add_space("alice/board", variables={"MSGBOARD_REPLICA": "hf://buckets/alice/board-bucket",
                                             "MSGBOARD_SNAPSHOT_INTERVAL": "15"})
    result = deploy("alice/board", operator_token=new_token()[1])
    assert result.bucket_id == "alice/board-bucket" and not result.created
    assert fake.writes[0] == ("create_bucket", "alice/board-bucket", True)
    assert [w[0] for w in fake.writes[1:3]] == ["secret", "secret"]


def test_missing_explicit_recovery_bucket_is_refused(fake):
    fake.add_space(
        "alice/board", tags=["msgboard"], variables={},
        files={"Dockerfile": b"FROM existing"},
    )
    with pytest.raises(DeployError):
        deploy(
            "alice/board", operator_token=new_token()[1],
            bucket_id="alice/missing-backup",
        )
    assert fake.writes == []


def test_a_tagged_board_without_a_replica_recovers_from_an_existing_bucket(fake):
    fake.add_space("alice/board", tags=["msgboard"], files={".gitattributes": b"", "Dockerfile": b"FROM existing"})
    fake.buckets["alice/backup"] = True
    result = deploy("alice/board", operator_token=new_token()[1], bucket_id="alice/backup")
    assert result.bucket_id == "alice/backup" and not any(w[0] == "create_bucket" for w in fake.writes)
    assert fake.spaces["alice/board"]["variables"]["MSGBOARD_REPLICA"] == "hf://buckets/alice/backup"


def test_card_title_override(fake):
    deploy("alice/board", operator_token=new_token()[1], title="Team board: run 7")
    assert 'title: "Team board: run 7"\n' in fake.spaces["alice/board"]["files"]["README.md"].decode()
    deploy("alice/plain", operator_token=new_token()[1], title="msgboard demo")
    assert 'title: "msgboard demo"\n' in fake.spaces["alice/plain"]["files"]["README.md"].decode()


@pytest.mark.parametrize("space_id, title", [("alice/board", "true"), ("alice/board", "No"), ("alice/board", "null"),
                                             ("alice/board", "1.5"), ("alice/2024", None), ("alice/board", "a: b # c")])
def test_card_title_reads_back_as_written(fake, space_id, title):
    # YAML reads bare true/No as booleans, null as None and 2024 as a number
    yaml = pytest.importorskip("yaml")
    deploy(space_id, operator_token=new_token()[1], title=title)
    card = fake.spaces[space_id]["files"]["README.md"].decode()
    assert yaml.safe_load(card.split("---")[1])["title"] == (title or space_id.split("/")[1])


def test_symlinks_out_of_the_package_are_not_uploaded(monkeypatch, tmp_path):
    package = tmp_path / "msgboard"
    package.mkdir()
    (package / "board.py").write_text("x = 1\n")
    (tmp_path / "secret.py").write_text("TOKEN = 'do not upload'\n")
    (package / "link.py").symlink_to(tmp_path / "secret.py")
    monkeypatch.setattr(hub, "_PACKAGE", package)
    files = hub._space_files("alice/board", hub._distribution())
    assert "msgboard/board.py" in files and "msgboard/link.py" not in files


@pytest.mark.parametrize("case", ["foreign space", "other bucket", "public bucket", "bad operator token",
                                  "replica is a directory", "replica names no bucket", "tagged, bucket missing",
                                  "old layout, bucket missing", "tagged, no replica"])
def test_refusals_change_nothing(fake, case):
    token = new_token()[1]
    kwargs = {}
    gone = {"MSGBOARD_REPLICA": "hf://buckets/alice/gone/board"}
    if case == "foreign space":
        fake.add_space("alice/board", tags=["gradio"])
    elif case == "other bucket":
        fake.add_space("alice/board", tags=["msgboard"], variables={"MSGBOARD_REPLICA": "hf://buckets/alice/a"})
        kwargs = {"bucket_id": "alice/b"}
    elif case == "replica is a directory":    # store_from_url() accepts it, but it names no bucket
        fake.add_space("alice/board", tags=["msgboard"], variables={"MSGBOARD_REPLICA": "/data/board"})
    elif case == "replica names no bucket":
        fake.add_space("alice/board", variables={"MSGBOARD_REPLICA": "hf://buckets/alice"})
    elif case == "tagged, bucket missing":    # deleted, or not visible to this token
        fake.add_space("alice/board", tags=["msgboard"], variables=gone)
    elif case == "old layout, bucket missing":
        fake.add_space("alice/board", variables=gone, files={".gitattributes": b"", "Dockerfile": b"FROM x"})
    elif case == "tagged, no replica":
        fake.add_space("alice/board", tags=["msgboard"])
    elif case == "public bucket":
        fake.buckets["alice/board-bucket"] = False
        kwargs = {"bucket_id": "board-bucket"}
    else:
        token = "not-a-token"
    before = {k: dict(v, files=dict(v["files"])) for k, v in fake.spaces.items()}
    with pytest.raises(DeployError):
        deploy("alice/board", operator_token=token, **kwargs)
    assert fake.writes == [] and fake.spaces == before


def test_wait_ready_waits_through_starting_and_a_stale_operator_token():
    answers = iter([httpx.ConnectError("refused"),
                    httpx.Response(503, json={"error": {"code": "starting", "message": "board is starting"}}),
                    httpx.Response(200, json={"status": "starting", "error": None}),
                    httpx.Response(200, json={"status": "ok"}), httpx.Response(401, json={}),
                    httpx.Response(200, json={"status": "ok"}), httpx.Response(200, json={"name": "operator"})])
    seen = []

    def handler(request):
        seen.append(request.url.path)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with Client("https://board.example", "tok", transport=httpx.MockTransport(handler)) as client:
        assert client.wait_ready(5, interval=0) is None
    assert seen == ["/v1/health"] * 4 + ["/v1/me", "/v1/health", "/v1/me"]


def test_wait_ready_times_out_naming_the_last_answer():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "starting", "error": "boom"}))
    with Client("https://board.example", "", transport=transport) as client:
        with pytest.raises(TimeoutError, match=r"200 'starting' \(boom\)"):
            client.wait_ready(0.05, interval=0.01)


def test_deploy_space_cli_keeps_the_operator_token_in_a_private_file(fake, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["deploy-space", "board", "--public"]) == 0
    path = tmp_path / ".config/msgboard/spaces/alice/board/operator.token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    token = path.read_text().strip()
    assert fake.spaces["alice/board"]["secrets"]["MSGBOARD_OPERATOR_TOKEN"] == token
    assert main(["deploy-space", "alice/board"]) == 0       # reuses the file, so the board keeps its operator
    assert fake.spaces["alice/board"]["secrets"]["MSGBOARD_OPERATOR_TOKEN"] == token
    assert token not in capsys.readouterr().out
    fake.add_space("alice/other")
    assert main(["deploy-space", "other"]) == 1
    assert "not a msgboard board" in capsys.readouterr().err
    assert not (tmp_path / ".config/msgboard/spaces/alice/other").exists()
