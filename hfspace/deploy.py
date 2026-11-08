"""Create or update the msgboard demo Space with msgboard.space.deploy(). From the repository root,
with msgboard installed with the hf extra (pip install -e '.[hf]'):

    python hfspace/deploy.py

The defaults target the live demo, lvwerra/msgboard-demo: set MSGBOARD_SPACE (and MSGBOARD_SECRETS)
for any other board. The bucket is the one the Space already uses (MSGBOARD_BUCKET overrides it for a
new Space). Board tokens and the invite code are kept in ~/.config/msgboard-demo/ and never printed.
A new Space is made public; an existing one keeps its visibility. People join with invite links
created in the dashboard's settings; the MSGBOARD_INVITE_CODE secret is kept working as one such link.
"""

from __future__ import annotations

import os
import secrets as secrets_mod
from pathlib import Path

from huggingface_hub import HfApi

from msgboard.auth import new_token
from msgboard.space import deploy

HERE = Path(__file__).resolve().parent
SPACE = os.environ.get("MSGBOARD_SPACE", "lvwerra/msgboard-demo")
BUCKET = os.environ.get("MSGBOARD_BUCKET") or None
# Each board keeps its own tokens: set MSGBOARD_SECRETS for any board other than the demo.
SECRETS = Path(os.environ.get("MSGBOARD_SECRETS", Path.home() / ".config" / "msgboard-demo")).expanduser()
SHARED_INVITE = os.environ.get("MSGBOARD_SHARED_INVITE", "1") == "1"   # 0: no standing member invite link
DEMO_BOTS = os.environ.get("MSGBOARD_DEMO_BOTS", "0") == "1"


def _secret_file(name: str, make) -> str:
    SECRETS.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = SECRETS / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(make() + "\n")
    return path.read_text().strip()


def main() -> None:
    operator = _secret_file("operator.token", lambda: new_token()[1])
    result = deploy(SPACE, operator_token=operator, bucket_id=BUCKET, private=False)
    api = HfApi()
    if SHARED_INVITE:
        api.add_space_secret(result.space_id, "MSGBOARD_INVITE_CODE",
                             _secret_file("invite.code", lambda: secrets_mod.token_urlsafe(12)))
    if DEMO_BOTS:  # not part of the package, so deploy() leaves it out; the app imports it from /app
        api.upload_file(path_or_fileobj=str(HERE / "demo_bots.py"), path_in_repo="demo_bots.py",
                        repo_id=result.space_id, repo_type="space", commit_message="Add the demo bots")
    api.add_space_variable(result.space_id, "MSGBOARD_DEMO_BOTS", "1" if DEMO_BOTS else "0")
    print(f"deployed {result.space_id} at {result.commit[:8]} ({result.url}, bucket {result.bucket_id}); "
          f"tokens{' and invite code' if SHARED_INVITE else ''} in {SECRETS}")


if __name__ == "__main__":
    main()
