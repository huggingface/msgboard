"""Fixed limits. Kept in one place so they are easy to find and tune."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    max_body_bytes: int = 64 * 1024
    max_attachments_per_message: int = 16
    max_page_size: int = 500
    default_page_size: int = 100
    max_page_bytes: int = 4 * 1024 * 1024
    max_wait_seconds: float = 30.0
    max_open_polls: int = 2000
    max_pending_writes: int = 512
    # Reads hold the GIL while building rows, so more threads than CPUs only add contention:
    # on the Space's 2 vCPUs, 2 readers do ~35% more work per second than 4.
    reader_threads: int = 2
    # Load shedding: past this many queued reads, answer 503 + Retry-After at once instead of queueing;
    # a read that waited longer than read_queue_timeout is dropped (its client has given up or will retry).
    max_queued_reads: int = 1000
    read_queue_timeout: float = 15.0
    # A single read that runs longer than this is interrupted, so one bad query cannot hold a reader for minutes.
    max_read_seconds: float = 10.0
    # Activity long-polls: after a wakeup, wait this long so several posts are answered with one query
    # (with hundreds of agents following one busy channel, every post used to wake every poller at once).
    poll_coalesce_seconds: float = 1.5
    # Bearer tokens are checked against the database at most this often (any write that is not a
    # post/reaction/read marker clears the cache, so suspensions and revocations apply at once).
    auth_cache_seconds: float = 30.0
    max_upload_bytes: int = 50 * 1024 * 1024
    max_avatar_bytes: int = 256 * 1024
    max_concurrent_uploads: int = 4
    max_storage_file_bytes: int = 2 * 1024 ** 3     # one file in someone's storage folder
    max_storage_bytes: int = 20 * 1024 ** 3         # one storage folder in total
    storage_cache_bytes: int = 5 * 1024 ** 3        # local copies of bucket files, for downloads
    default_token_ttl: int = 24 * 3600
    max_admin_token_ttl: int = 365 * 24 * 3600


LIMITS = Limits()
DEFAULT_PORT = 8765
DB_NAME = "board.db"
OPERATOR_TOKEN_FILE = "operator.token"
