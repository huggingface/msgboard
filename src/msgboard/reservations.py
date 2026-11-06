"""In-process reservations: slow object I/O must not take the SQLite writer."""

import asyncio
from contextlib import asynccontextmanager


class Reservations:
    def __init__(self, *, paths=False):
        self.condition = asyncio.Condition()
        self.active = set()
        self.paths = paths

    def conflicts(self, a, b):
        if not self.paths:
            return a == b
        return a[0] == b[0] and (a[1] == b[1] or a[1].startswith(b[1] + "/") or b[1].startswith(a[1] + "/"))

    @asynccontextmanager
    async def hold(self, keys):
        keys = set(keys)
        async with self.condition:
            await self.condition.wait_for(lambda: not any(self.conflicts(a, b) for a in keys for b in self.active))
            self.active.update(keys)
        try:
            yield
        finally:
            async with self.condition:
                self.active.difference_update(keys)
                self.condition.notify_all()
