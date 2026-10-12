from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suites", nargs="+", choices=["matrix", "rate", "fanout", "volume", "limits", "smoke", "scenario", "attachments", "unicode"])
    parser.add_argument("--url", default="https://lvwerra-flock-board.hf.space")
    parser.add_argument("--token-file", default="/home/node/local/flock-space-secrets/operator.token")
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--config")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results")
    parser.add_argument("--suffix", default="")
    arguments = parser.parse_args()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": "Bearer " + os.environ["HF_TOKEN"],
               "X-Flock-Token": Path(arguments.token_file).read_text().strip()}
    with httpx.Client(base_url=arguments.url, headers=headers, timeout=90) as client:
        for suite in arguments.suites:
            configuration = {"suite": suite, "duration": arguments.duration}
            if arguments.config:
                configuration.update(json.loads(Path(arguments.config).read_text()))
            response = client.post("/_bench/jobs", json=configuration)
            response.raise_for_status()
            job_id = response.json()["job_id"]
            destination = arguments.output_dir / (suite + arguments.suffix + ".json")
            destination.write_text(json.dumps({"job_id": job_id, "configuration": configuration}))
            print(json.dumps({"suite": suite, "job_id": job_id}), flush=True)
            seen = 0
            deadline = time.monotonic() + 1800
            failures = 0
            while time.monotonic() < deadline:
                time.sleep(5)
                try:
                    response = client.get("/_bench/jobs/" + job_id)
                    response.raise_for_status()
                    job = response.json()
                    failures = 0
                except (httpx.HTTPError, ValueError) as error:
                    failures += 1
                    print(json.dumps({"poll_error": type(error).__name__, "consecutive": failures}), flush=True)
                    if failures >= 5:
                        raise
                    continue
                temporary = destination.with_suffix(".tmp")
                temporary.write_text(json.dumps(job, indent=2))
                temporary.replace(destination)
                scenarios = job.get("result", {}).get("scenarios", [])
                for entry in scenarios[seen:]:
                    if "name" not in entry:
                        print(json.dumps({key: value for key, value in entry.items() if key != "outcomes"}), flush=True)
                        continue
                    print(json.dumps({"name": entry["name"], "posts_s": entry["posts_per_second"],
                        "requests_s": entry["successful_requests_per_second"],
                        "p95_ms": entry["success_latency_ms"].get("p95"),
                        "statuses": entry["statuses"], "generator_dropped": entry["offered_dropped_by_generator"],
                        "server_cpu": entry["server_cpu_cores"], "generator_cpu": entry["generator_cpu_cores"]}), flush=True)
                seen = len(scenarios)
                if job["status"] != "running":
                    print(json.dumps({"suite": suite, "status": job["status"], "stderr": job.get("stderr")}), flush=True)
                    if job["status"] != "complete":
                        raise RuntimeError("Benchmark failed; inspect " + str(destination))
                    break
            else:
                raise TimeoutError("Job status deadline exceeded: " + job_id)


if __name__ == "__main__":
    main()
