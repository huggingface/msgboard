"""Deploy a board to a Hugging Face Space: `msgboard.space.deploy()`. Needs the `hf` extra.

The Space runs `msgboard.space.app` from the files of the msgboard install that calls deploy(),
keeps its board in SQLite on its own disk and pushes snapshots to a private bucket through the
Hub API (DEPLOY_SPACES.md explains why it is not a bucket mount).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from .. import auth

log = logging.getLogger("msgboard.space")

TAG = "msgboard"          # deploy() puts it in the Space card and refuses Spaces that lack it
SNAPSHOT_INTERVAL = "15"  # MSGBOARD_SNAPSHOT_INTERVAL for a new Space, in seconds
_HERE = Path(__file__).resolve().parent
_PACKAGE = _HERE.parent   # the installed msgboard package, uploaded as-is


class DeployError(Exception):
    """deploy() refused, before changing anything on the Hub."""


@dataclass(frozen=True)
class Deployment:
    """What deploy() set up."""

    space_id: str                  # "namespace/name"; a bare name gets the logged-in account
    url: str                       # the board's base URL, https://<namespace>-<name>.hf.space
    bucket_id: str                 # "namespace/name" of the private bucket that holds the snapshots
    created: bool                  # True if this call created the Space, False if it reused one
    private: bool                  # the Space's visibility; a reused Space keeps its own
    commit: str                    # the Space repo commit that holds this deployment


def resolve_space_id(space_id: str, *, hf_token: str | None = None) -> str:
    """Return `space_id` as "namespace/name". A bare name goes in the logged-in account, as in Trackio."""
    return _Names(_hf_api(_hf_token(hf_token))).full(space_id)


def deploy(space_id: str, *, operator_token: str, bucket_id: str | None = None,
           private: bool | None = None, hf_token: str | None = None) -> Deployment:
    """Create or update the board Space `space_id` and return a `Deployment`.

    `space_id` is "namespace/name" or a bare name for the logged-in account. A missing Space is
    created (Docker SDK, CPU Basic) with `private` as its visibility (None: the namespace's default).
    An existing Space is reused, and keeps its visibility, only if it is a msgboard board: its card
    has the `msgboard` tag that deploy() puts there, or it has a MSGBOARD_REPLICA variable (a Space
    from before this function, or a deploy() that stopped before its first upload). Anything else
    raises DeployError without changing the Space.

    The bucket is `bucket_id` if given, else the one the Space's MSGBOARD_REPLICA already names, else
    "<space_id>-bucket", or -2, -3 ... if that exists, so a new board never restores the snapshots a
    deleted Space left behind. The bucket is always private, whatever `private` says: it holds every
    message, token hash and invite. An existing public bucket is refused. So is anything that would
    restart a reused board against an empty bucket: a different `bucket_id`, a MSGBOARD_REPLICA that
    names no bucket, a deployed board whose bucket is missing (deleted or not visible to this token),
    and a tagged board without MSGBOARD_REPLICA unless `bucket_id` says where its snapshots are. All
    checks run before the first write; a reused Space's bucket is in place before its secrets change.

    The Space gets the secrets MSGBOARD_OPERATOR_TOKEN (`operator_token`, from
    `msgboard.auth.new_token()`; the app makes it the admin credential on every boot) and HF_TOKEN
    (`hf_token`, default: the logged-in token, which also makes the Hub calls; the Space needs it to
    write to the bucket), and the variables MSGBOARD_REPLICA and MSGBOARD_SNAPSHOT_INTERVAL. Its files
    become the installed msgboard package, LICENSE, requirements.txt, the Dockerfile and the card;
    anything else in the Space repo is deleted.

    The Space then builds or restarts, which takes a few minutes: wait with `Client.wait_ready()`.
    A reused Space keeps answering with its previous container for a while; wait_ready() with the
    operator token waits until the board accepts that token.
    """
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete
    from huggingface_hub.errors import RepositoryNotFoundError

    if auth.parse_token(operator_token) is None:
        raise DeployError("operator_token is not a board token; make one with msgboard.auth.new_token()")
    token = _hf_token(hf_token)
    api = _hf_api(token)
    names = _Names(api)
    space_id = names.full(space_id)
    bucket_id = names.full(bucket_id) if bucket_id else None
    try:
        info = api.space_info(space_id)
    except RepositoryNotFoundError:
        info = None
    variables: dict[str, str] = {}
    replica = current = None
    tagged = False
    if info is not None:
        variables = {k: v.value for k, v in api.get_space_variables(space_id).items()}
        replica = variables.get("MSGBOARD_REPLICA")
        tagged = TAG in (info.tags or [])
        if not tagged and replica is None:
            raise DeployError(f"{space_id} exists but is not a msgboard board (no {TAG!r} tag in its card and "
                              "no MSGBOARD_REPLICA variable), so it was left alone; choose another Space ID")
        # A restart boots a reused board from its bucket, so never let it come back against an empty one.
        if replica is not None:
            current = _bucket_of(replica)
            if current is None:
                raise DeployError(f"{space_id} has MSGBOARD_REPLICA={replica!r}, which names no HF bucket; set it "
                                  "to hf://buckets/<namespace>/<bucket>[/<prefix>] where the board's snapshots are")
            if bucket_id and bucket_id != current:
                raise DeployError(f"{space_id} keeps its board in bucket {current}, not {bucket_id}; "
                                  "leave bucket_id out to keep using it")
        elif not bucket_id:
            raise DeployError(f"{space_id} is a msgboard board without a MSGBOARD_REPLICA variable; "
                              "pass bucket_id to say which bucket holds its snapshots")
    bucket_id = bucket_id or current or _free_bucket_id(api, f"{space_id}-bucket")
    if bucket_id != current:
        replica = None  # the Space had no bucket yet: snapshots go to the new one's root
    bucket = _bucket_info(api, bucket_id)
    # A missing bucket is fine for a Space that was never deployed (a deploy() stopped after create_repo),
    # but a deployed board (tagged, or an old-layout Space with a Dockerfile) would restart empty.
    if bucket is None and current is not None and (
            tagged or "Dockerfile" in api.list_repo_files(space_id, repo_type="space")):
        raise DeployError(f"{space_id} keeps its board in bucket {current}, which was not found (deleted, or not "
                          "visible to this token); refusing to restart the board against an empty bucket")
    if bucket is not None and not bucket.private:
        raise DeployError(f"bucket {bucket_id} is public; a board's bucket must be private, because its "
                          "snapshots hold every message, token hash and invite")
    dist = _distribution()
    files = _space_files(space_id, dist)

    secrets = {"MSGBOARD_OPERATOR_TOKEN": operator_token, "HF_TOKEN": token}
    wanted = {"MSGBOARD_REPLICA": replica or f"hf://buckets/{bucket_id}",
              "MSGBOARD_SNAPSHOT_INTERVAL": variables.get("MSGBOARD_SNAPSHOT_INTERVAL", SNAPSHOT_INTERVAL)}
    created = info is None
    if created:  # secrets and variables come with the Space; nothing runs before the first commit
        api.create_repo(space_id, repo_type="space", private=private, space_sdk="docker",
                        space_hardware="cpu-basic",
                        space_secrets=[{"key": k, "value": v} for k, v in secrets.items()],
                        space_variables=[{"key": k, "value": v} for k, v in wanted.items()])
    if bucket is None:  # after create_repo, so a retry finds the Space and reuses this bucket name
        api.create_bucket(bucket_id, private=True, exist_ok=True)
    if not created:  # each of these restarts a reused Space, so only once its bucket is in place
        for key, value in secrets.items():
            api.add_space_secret(space_id, key, value)
        for key, value in wanted.items():
            if variables.get(key) != value:
                api.add_space_variable(space_id, key, value)
    stale = set(api.list_repo_files(space_id, repo_type="space")) - set(files) - {".gitattributes"}
    operations = [CommitOperationAdd(path, data) for path, data in files.items()]
    operations += [CommitOperationDelete(path) for path in sorted(stale)]
    commit = api.create_commit(space_id, repo_type="space", operations=operations,
                               commit_message=f"Deploy msgboard {dist.version}")
    info = api.space_info(space_id)
    return Deployment(space_id=space_id, url=(info.host or f"https://{info.subdomain}.hf.space").rstrip("/"),
                      bucket_id=bucket_id, created=created, private=bool(info.private), commit=commit.oid)


def _hf_token(hf_token: str | None) -> str:
    from huggingface_hub import get_token

    token = hf_token or get_token()
    if not token:
        raise DeployError("no Hugging Face token: log in with `hf auth login` or set HF_TOKEN")
    return token


def _hf_api(token: str):  # tests replace this with a fake
    from huggingface_hub import HfApi

    return HfApi(token=token)


class _Names:
    """Fills in the logged-in account for bare repo names, asking the Hub at most once."""

    def __init__(self, api):
        self.api = api
        self._namespace = None

    def full(self, repo_id: str) -> str:
        from huggingface_hub.utils import validate_repo_id

        if "/" not in repo_id:
            self._namespace = self._namespace or self.api.whoami()["name"]
            repo_id = f"{self._namespace}/{repo_id}"
        validate_repo_id(repo_id)
        return repo_id


def _bucket_of(replica: str | None) -> str | None:
    """The bucket ID in an `hf://buckets/<namespace>/<bucket>[/<prefix>]` replica URL."""
    if not replica or not replica.startswith("hf://buckets/"):
        return None
    parts = replica[len("hf://buckets/"):].strip("/").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def _bucket_info(api, bucket_id: str):
    from huggingface_hub.errors import BucketNotFoundError

    try:
        return api.bucket_info(bucket_id)
    except BucketNotFoundError:
        return None


def _free_bucket_id(api, preferred: str) -> str:
    """`preferred`, or the first of preferred-2, -3 ... that doesn't exist, as Trackio's resolve_auto_bucket_id."""
    candidate, n = preferred, 1
    while _bucket_info(api, candidate) is not None:
        n += 1
        candidate = f"{preferred}-{n}"
    if candidate != preferred:
        log.info("bucket %s already exists; using %s", preferred, candidate)
    return candidate


def _distribution():
    try:
        return metadata.distribution("msgboard")
    except metadata.PackageNotFoundError:
        raise DeployError("msgboard is not installed; deploy() uploads the installed package "
                          "(pip install 'msgboard[hf]', or pip install -e '.[hf]' in a checkout)") from None


def _space_files(space_id: str, dist) -> dict[str, bytes]:
    """The Space repo's files by path: card, Dockerfile, requirements.txt, LICENSE and the package."""
    card = (_HERE / "README.md").read_text().replace("title: msgboard\n", f"title: {space_id.split('/')[1]}\n", 1)
    files = {"README.md": card.encode(), "Dockerfile": (_HERE / "Dockerfile").read_bytes(),
             "requirements.txt": _requirements(dist).encode(), "LICENSE": _license(dist)}
    # An allowlist, so stray files in an editable install's src/msgboard (a test board's token, say)
    # never reach a public Space. Keep it in step with [tool.setuptools.package-data] in pyproject.toml.
    for path in sorted(_PACKAGE.rglob("*")):
        if (path.suffix in (".py", ".html") and "__pycache__" not in path.parts and path.is_file()
                and path.resolve().is_relative_to(_PACKAGE)):  # no symlinks out of the package
            files["msgboard/" + path.relative_to(_PACKAGE).as_posix()] = path.read_bytes()
    return files


def _requirements(dist) -> str:
    """The install's own dependencies plus the hf extra (bucket snapshots); other extras are left out."""
    lines = []
    for req in dist.requires or []:
        spec, _, marker = req.partition(";")
        marker = marker.strip()
        if not marker or re.fullmatch(r"""extra\s*==\s*["']hf["']""", marker):
            lines.append(spec.strip())
        elif "extra" not in marker:
            lines.append(req.strip())
    return "\n".join(lines) + "\n"


def _license(dist) -> bytes:
    """LICENSE from the install's dist-info: licenses/ since PEP 639 (setuptools 77+), the top level before."""
    for name in dist.metadata.get_all("License-File") or ["LICENSE"]:
        for path in (f"licenses/{name}", name):
            text = dist.read_text(path)
            if text:
                return text.encode()
    raise DeployError("the installed msgboard has no LICENSE in its metadata; reinstall it with setuptools>=77")
