"""Units, range validation and aggregation helpers for board analytics. SQL stays in store."""

from __future__ import annotations

import math
import re

from .errors import bad_request

DAY = 86400
MAX_TIMESTAMP = 253402300800  # exclusive end of UTC year 9999
WORD = re.compile(r"\b\w+(?:['’]\w+)*\b", re.UNICODE)
RESPONSE_BOUNDS = (0, 60, 300, 900, 3600, 14400, 86400)


def bounds(now: float, days: int = 7, start: float | None = None,
           end: float | None = None) -> tuple[float, float]:
    end = now if end is None else end
    if not math.isfinite(end) or not 0 < end <= MAX_TIMESTAMP:
        raise bad_request("to must be a finite positive Unix timestamp through UTC year 9999")
    if start is None:
        start = max(0, (math.ceil(end / DAY) - days) * DAY)
    if not math.isfinite(start) or start < 0 or start >= end:
        raise bad_request("from must be a finite nonnegative Unix timestamp before to")
    if end - start > 365 * DAY:
        raise bad_request("choose at most 365 days")
    return start, end


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low = math.floor(index)
    high = math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def response_summary(requests: int, latencies: list[float]) -> dict:
    return {"requests": requests, "answered": len(latencies), "unanswered": requests - len(latencies),
            "median_seconds": percentile(latencies, .5), "p90_seconds": percentile(latencies, .9)}


def response_histogram(latencies: list[float]) -> list[dict]:
    bins = [{"from_seconds": low, "to_seconds": high, "count": 0}
            for low, high in zip(RESPONSE_BOUNDS, (*RESPONSE_BOUNDS[1:], None))]
    for value in latencies:
        for bucket in bins:
            if bucket["to_seconds"] is None or value < bucket["to_seconds"]:
                bucket["count"] += 1
                break
    return bins


def chord_graph(edges: list[dict], identities: dict[int, dict], top_k: int) -> dict:
    weights: dict[int, int] = {}
    for edge in edges:
        for pid in (edge["source"], edge["target"]):
            weights[pid] = weights.get(pid, 0) + edge["total"]
    ranked = sorted(weights, key=lambda pid: (-weights[pid], pid))
    selected = set(ranked[:top_k])
    nodes = [dict(identities[pid], total=weights[pid], members=1) for pid in ranked[:top_k]]
    hidden = set(ranked) - selected
    if hidden:
        nodes.append({"id": "other", "display_name": "Other", "external_id": "other", "identity_type": "other",
                      "total": sum(weights[pid] for pid in hidden), "members": len(hidden)})
    links = {}
    for edge in edges:
        source = edge["source"] if edge["source"] in selected else "other"
        target = edge["target"] if edge["target"] in selected else "other"
        key = tuple(sorted((source, target), key=lambda pid: (pid == "other", str(pid))))
        link = links.setdefault(key, {"source": key[0], "target": key[1], "total": 0,
                                      "mentions": 0, "replies": 0, "dms": 0})
        for field in ("total", "mentions", "replies", "dms"):
            link[field] += edge[field]
    result = sorted(links.values(), key=lambda link: (-link["total"], str(link["source"]), str(link["target"])))
    return {"top_k": top_k, "total": sum(edge["total"] for edge in edges), "nodes": nodes, "links": result,
            "other_internal": next((dict(link) for link in result if link["source"] == link["target"] == "other"),
                                   {"source": "other", "target": "other", "total": 0, "mentions": 0, "replies": 0, "dms": 0})}


def listening_series(start: float, end: float, records: dict[int, tuple[float, int]],
                     started_at: float | None) -> dict:
    width = max(60, math.ceil((end - start) / 1500 / 60) * 60)
    first = math.floor(start / width) * width
    buckets = {at: [] for at in range(first, math.ceil(end / width) * width, width)}
    for minute, (observed_at, count) in records.items():
        at = (minute // width) * width
        if start <= observed_at < end and at in buckets:
            buckets[at].append(count)
    series = []
    for at, counts in buckets.items():
        lo, hi = max(at, start), min(at + width, end)
        expected = math.ceil(hi / 60) - math.floor(lo / 60)
        series.append({"at": at, "count": sum(counts) / len(counts) if counts else None,
                       "min": min(counts) if counts else None, "max": max(counts) if counts else None,
                       "samples": len(counts), "expected_samples": expected})
    return {"sample_seconds": 60, "bucket_seconds": width, "started_at": started_at, "series": series}
