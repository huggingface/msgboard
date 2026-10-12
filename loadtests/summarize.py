from __future__ import annotations

import csv
import json
from pathlib import Path


def main():
    root = Path(__file__).parent / "results"
    rows = []
    for source in sorted(root.glob("*.json")):
        artifact = json.loads(source.read_text())
        result = artifact.get("result", artifact)
        for entry in result.get("scenarios", []):
            if "name" not in entry:
                continue
            observations = entry.get("observations", [])
            systems = [entry.get("system_before", {}), entry.get("system_after", {})]
            systems.extend(observation.get("system", {}) for observation in observations)
            rss = [int(snapshot["process"]["VmRSS"].split()[0]) / 1024
                   for snapshot in systems if snapshot.get("process", {}).get("VmRSS")]
            cgroup_memory = [int(snapshot["cgroup"]["memory.current"]) / 1048576
                             for snapshot in systems if snapshot.get("cgroup", {}).get("memory.current")]
            statuses = entry["statuses"]
            errors = sum(count for status, count in statuses.items() if not status.startswith("2"))
            parameters = entry["parameters"]
            latency = entry["success_latency_ms"]
            rows.append({"artifact": source.name, "name": entry["name"],
                "concurrency": parameters["concurrency"], "body_bytes": parameters["body_bytes"],
                "duration_s": entry["duration_seconds"], "requests": entry["requests"],
                "posts_s": entry["posts_per_second"], "requests_s": entry["successful_requests_per_second"],
                "p50_ms": latency.get("p50"), "p95_ms": latency.get("p95"), "p99_ms": latency.get("p99"),
                "failures": errors, "status_counts": json.dumps(statuses, sort_keys=True),
                "offered_drops": entry["offered_dropped_by_generator"],
                "server_cpu_cores": entry["server_cpu_cores"], "driver_cpu_cores": entry["generator_cpu_cores"],
                "peak_sampled_rss_mib": round(max(rss), 1) if rss else None,
                "peak_sampled_cgroup_mib": round(max(cgroup_memory), 1) if cgroup_memory else None,
                "response_mib_s": entry["response_mib_per_second"],
                "db_mib": round(entry["metrics_after"].get("db_bytes", 0) / 1048576, 1),
                "wal_mib": round(entry["metrics_after"].get("wal_bytes", 0) / 1048576, 1),
                "delivery_p95_ms": entry["delivery_lag_ms"].get("p95"),
                "sequence_gaps": entry["sequence_gaps"],
                "duplicate_sequences": entry["duplicate_acknowledged_sequences"]})
    if rows:
        with (root / "summary.csv").open("w", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    print(f"Summarized {len(rows)} scenarios")


if __name__ == "__main__":
    main()
