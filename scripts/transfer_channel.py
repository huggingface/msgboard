"""Copy one public channel bundle; default to a read-only target dry-run.

Tokens are read from files and are never printed or passed on the command line.
Use --apply only after reviewing the dry-run report and obtaining approval.
"""

import argparse
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source board URL")
    parser.add_argument("--source-channel", type=int, required=True)
    parser.add_argument("--source-token-file", type=Path, required=True)
    parser.add_argument("--target", required=True, help="target board URL")
    parser.add_argument("--target-channel", type=int, required=True)
    parser.add_argument("--target-token-file", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True, help="ZIP: create for dry-run; reuse the reviewed file with --apply")
    parser.add_argument("--apply", action="store_true", help="perform the import instead of a dry-run")
    args = parser.parse_args()
    if args.apply and not args.bundle.is_file():
        parser.error("--apply requires an existing reviewed --bundle file")
    # Avoid forwarding credentials through redirects or inheriting proxy settings.
    with httpx.Client(timeout=300, trust_env=False, follow_redirects=False) as client:
        if not args.apply:
            with client.stream("GET", args.source.rstrip("/") + f"/v1/channels/{args.source_channel}/export",
                               headers={"X-Msgboard-Token": args.source_token_file.read_text().strip()}) as response:
                response.raise_for_status()
                with args.bundle.open("xb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
        with args.bundle.open("rb") as bundle:
            response = client.post(args.target.rstrip("/") + f"/v1/channels/{args.target_channel}/import",
                                   params={"dry_run": str(not args.apply).lower()}, content=bundle,
                                   headers={"X-Msgboard-Token": args.target_token_file.read_text().strip(),
                                            "Content-Type": "application/zip"})
        response.raise_for_status()
        report = response.json()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["can_import"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
