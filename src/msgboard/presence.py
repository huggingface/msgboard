"""Live agent observations. Records are replaced, never mutated, so snapshots stay stable."""

from __future__ import annotations

import threading

PRESENCE_FLUSH_SECONDS = 30.0
ACTIVE_SECONDS = 60.0
FIELDS = ("last_seen_at", "last_poll_at", "listens", "cursor", "cursor_since", "waiting", "waiting_since")


def unknown() -> dict:
    return dict.fromkeys(FIELDS)


class Presence:
    def __init__(self, records: dict[int, dict] | None = None):
        self._records = records or {}
        self._dirty: dict[int, int] = {}
        self._version = 0
        self._poll_sequence = 0
        self._latest_poll: dict[int, int] = {}
        self._lock = threading.Lock()

    def _put(self, pid: int, record: dict) -> None:
        self._version += 1
        self._records[pid] = record
        self._dirty[pid] = self._version

    def seen(self, principal, now: float) -> None:
        if principal.is_human:
            return
        with self._lock:
            record = dict(self._records.get(principal.id, unknown()))
            record["last_seen_at"] = max(now, record["last_seen_at"] or now)
            self._put(principal.id, record)

    def next_poll(self) -> int:
        with self._lock:
            self._poll_sequence += 1
            return self._poll_sequence

    def polled(self, principal, token: int, now: float, cursor: int | None,
               mode: str, channel_ids: list[int], waiting: bool) -> None:
        if principal.is_human:
            return
        listens = {"mode": mode, "channel_ids": sorted(set(channel_ids))}
        with self._lock:
            if token < self._latest_poll.get(principal.id, 0):
                return
            self._latest_poll[principal.id] = token
            record = dict(self._records.get(principal.id, unknown()))
            changed = record["cursor"] != cursor or record["listens"] != listens
            gap = record["last_poll_at"] is None or now - record["last_poll_at"] > ACTIVE_SECONDS
            if changed:
                record["cursor_since"] = now if cursor is not None else None
            if changed or gap:
                record["waiting_since"] = None
            record.update(last_poll_at=now, listens=listens, cursor=cursor)
            self._set_waiting(record, waiting if cursor is not None else None, now)
            self._put(principal.id, record)

    @staticmethod
    def _set_waiting(record: dict, waiting: bool | None, now: float) -> None:
        record["waiting"] = waiting
        if waiting:
            if record["waiting_since"] is None:
                record["waiting_since"] = now
        else:
            record["waiting_since"] = None

    def observed(self, principal, token: int, waiting: bool, now: float) -> None:
        """A held poll can discover new work; older concurrent polls cannot overwrite newer ones."""
        if principal.is_human:
            return
        with self._lock:
            if token != self._latest_poll.get(principal.id):
                return
            record = dict(self._records[principal.id])
            self._set_waiting(record, waiting if record["cursor"] is not None else None, now)
            if record != self._records[principal.id]:
                self._put(principal.id, record)

    def snapshot(self) -> dict[int, dict]:
        with self._lock:
            return dict(self._records)

    def pending(self) -> tuple[dict[int, int], dict[int, dict]]:
        with self._lock:
            versions = dict(self._dirty)
            return versions, {pid: self._records[pid] for pid in versions}

    def saved(self, versions: dict[int, int]) -> None:
        with self._lock:
            for pid, version in versions.items():
                if self._dirty.get(pid) == version:
                    del self._dirty[pid]
