from druks.durable.exceptions import FatalError


class ScheduleSkipped(FatalError):
    """Druks skipped a schedule's tool call. The raise makes DBOS record the due time as
    failed."""
