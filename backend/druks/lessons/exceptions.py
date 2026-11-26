from druks.exceptions import ValidationError


class LessonIndexFull(ValidationError):
    """The index holds as many accepted lessons as it may."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"The lessons index is full: it holds {limit} accepted lessons. Retire a lesson "
            "or merge two, then accept again."
        )
