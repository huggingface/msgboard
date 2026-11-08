"""msgboard on a Hugging Face Space.

- `deploy()` creates or updates a board Space and its private bucket (`hub.py`, needs the `hf` extra).
- `app.py` is what the Space runs; `accounts.py` adds invite links and "Sign in with Hugging Face".
- `Dockerfile` and `README.md` (the Space card) are uploaded with the package.
"""

from .hub import Deployment, DeployError, deploy, resolve_space_id

__all__ = ["Deployment", "DeployError", "deploy", "resolve_space_id"]
