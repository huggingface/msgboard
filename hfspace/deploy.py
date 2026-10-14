"""Create or update the private msgboard demo Space. Run from the repository root:

    python hfspace/deploy.py

Board tokens and the invite code are kept in ~/.config/msgboard-demo/ and never printed.
The Space is made public. People join with invite links created in the dashboard's
settings; the MSGBOARD_INVITE_CODE secret is kept working as one such link.
"""

from __future__ import annotations

import os
import secrets as secrets_mod
import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, create_bucket
from huggingface_hub.errors import RepositoryNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from msgboard.auth import new_token  # noqa: E402

SPACE = os.environ.get("MSGBOARD_SPACE", "lvwerra/msgboard-demo")
BUCKET = os.environ.get("MSGBOARD_BUCKET", "lvwerra/msgboard-demo-data")
SECRETS = Path.home() / ".config" / "msgboard-demo"


def _secret_file(name: str) -> Path:
    SECRETS.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = SECRETS / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new_token()[1] + "\n")
    return path


def main() -> None:
    api = HfApi()
    create_bucket(BUCKET, private=True, exist_ok=True)
    operator = _secret_file("operator.token").read_text().strip()
    invite_path = SECRETS / "invite.code"
    if not invite_path.exists():
        fd = os.open(invite_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets_mod.token_urlsafe(12) + "\n")
    invite = invite_path.read_text().strip()
    secrets = {"MSGBOARD_OPERATOR_TOKEN": operator, "HF_TOKEN": os.environ["HF_TOKEN"],
               "MSGBOARD_INVITE_CODE": invite}
    variables = {"MSGBOARD_REPLICA": f"hf://buckets/{BUCKET}/demo", "MSGBOARD_SNAPSHOT_INTERVAL": "15",
                 "MSGBOARD_DEMO_BOTS": os.environ.get("MSGBOARD_DEMO_BOTS", "0")}
    try:
        api.space_info(SPACE)
        for k, v in secrets.items():
            api.add_space_secret(SPACE, k, v)
        for k, v in variables.items():
            api.add_space_variable(SPACE, k, v)
    except RepositoryNotFoundError:
        api.create_repo(SPACE, repo_type="space", private=True, space_sdk="docker", space_hardware="cpu-basic",
                        space_secrets=[{"key": k, "value": v} for k, v in secrets.items()],
                        space_variables=[{"key": k, "value": v} for k, v in variables.items()])
    files = {
        "Dockerfile": ROOT / "hfspace/Dockerfile",
        "README.md": ROOT / "hfspace/README.md",
        "pyproject.toml": ROOT / "pyproject.toml",
        "hfspace/app.py": ROOT / "hfspace/app.py",
        "hfspace/demo_bots.py": ROOT / "hfspace/demo_bots.py",
        "hfspace/accounts.py": ROOT / "hfspace/accounts.py",
    }
    for path in sorted((ROOT / "src/msgboard").iterdir()):
        if path.suffix in (".py", ".html"):
            files[f"src/msgboard/{path.name}"] = path
    commit = api.create_commit(SPACE, repo_type="space", commit_message="Deploy msgboard demo",
                               operations=[CommitOperationAdd(k, str(v)) for k, v in files.items()])
    # Public, so collaborators can open the link; board data still needs a token or HF sign-in.
    api.update_repo_settings(SPACE, repo_type="space", private=False)
    print(f"deployed {SPACE} at {commit.oid[:8]}; tokens and invite code in {SECRETS}")


if __name__ == "__main__":
    main()
