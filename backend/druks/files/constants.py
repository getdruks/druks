from datetime import timedelta

MAX_FILE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
# The limit for a file that a person picks in a browser, not for a workspace that an agent ships.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB
REAPER_GRACE_PERIOD = timedelta(days=1)
# The address of a file is this path, then its id.
FILE_URL_PREFIX = "/api/files/"

INLINE_CONTENT_TYPES = {
    "application/pdf",
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
}
