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
    reader_threads: int = 4
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
