import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.errors import RepositoryNotFoundError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from msgboard.auth import new_token


def deploy(repo_id):
    secret_dir = Path(os.environ.get("AM_LOCAL", "/tmp")) / "msgboard-space-secrets"
    secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_file = secret_dir / "operator.token"
    if not token_file.exists():
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as destination:
            destination.write(new_token()[1] + "\n")
    token = token_file.read_text().strip()
    api = HfApi()
    try:
        existing = api.space_info(repo_id)
        if not existing.private or existing.sdk != "docker":
            raise RuntimeError("Refusing to replace an existing incompatible or public Space")
        api.add_space_secret(repo_id, "MSGBOARD_OPERATOR_TOKEN", token)
        api.add_space_variable(repo_id, "MSGBOARD_BENCH_ENABLE", "1")
    except RepositoryNotFoundError:
        api.create_repo(repo_id, repo_type="space", private=True, space_sdk="docker", space_hardware="cpu-basic",
                        space_secrets=[{"key": "MSGBOARD_OPERATOR_TOKEN", "value": token}],
                        space_variables=[{"key": "MSGBOARD_BENCH_ENABLE", "value": "1"}])
    files = {"Dockerfile": ROOT / "space/Dockerfile", "README.md": ROOT / "space/README.md",
             "space_app.py": ROOT / "space/space_app.py", "loadtest.py": ROOT / "loadtests/loadtest.py",
             "pyproject.toml": ROOT / "pyproject.toml"}
    for name in ("aio_loadtest.py", "attachments.py", "unicode_boundary.py"):
        files[name] = ROOT / "loadtests" / name
    for source in sorted((ROOT / "src/msgboard").glob("*.py")):
        files[str(source.relative_to(ROOT))] = source
    source_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    manifest = json.dumps({"files": source_hashes}, indent=2).encode()
    operations = [CommitOperationAdd(path_in_repo=name, path_or_fileobj=str(path)) for name, path in files.items()]
    operations.append(CommitOperationAdd(path_in_repo="source-manifest.json", path_or_fileobj=manifest))
    commit = api.create_commit(repo_id, repo_type="space", commit_message="Deploy the msgboard prototype with bounded load-test diagnostics",
                               operations=operations)
    output = ROOT / "loadtests/results/deployment.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"repo_id": repo_id, "commit": commit.oid, "files": source_hashes,
                                  "hardware_requested": "cpu-basic", "private": True}, indent=2))
    print(json.dumps({"repo_id": repo_id, "commit": commit.oid, "operator_token_file": str(token_file)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy the prototype to an existing private Docker Space or create one.")
    parser.add_argument("repo_id", help="Space ID, namespace/name")
    deploy(parser.parse_args().repo_id)
