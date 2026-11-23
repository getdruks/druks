from druks.db import StoredSubject, db_session
from druks.workflows import SubjectSummary
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column

from druks_field_notes.app import FieldNotes


class Note(StoredSubject, ordering=("-created_at", "-id")):
    # What the note is about: the raw observation that an operator wrote. A run's agent
    # reads it and writes its gist.
    body: Mapped[str]
    # ``body`` in one line. A Summarize run writes it when it finishes. None until then.
    gist: Mapped[str | None]
    # The username of the account that archived the note. None until someone archives it.
    archived_by: Mapped[str | None]
    # The username of the account that wrote the note. None for a note that no account wrote.
    written_by: Mapped[str | None]

    @classmethod
    async def list_recent(cls, *, limit: int = 100) -> list["Note"]:
        statement = select(cls).order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return list(await db_session().scalars(statement))

    async def save_gist(self, gist: str) -> None:
        self.gist = gist
        await self.save()

    async def archive(self, username: str) -> None:
        self.archived_by = username
        await self.save()

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[SubjectSummary]:
        # How many notes the board shows is a setting of the operator. It is thus in the
        # settings of the app.
        notes = await cls.list_recent(limit=(await FieldNotes.settings()).board_size)
        return [note.get_summary() for note in notes]


class Repository(StoredSubject):
    # ``owner/name``. A Survey run's RepoWorkspace clones its default branch.
    repo: Mapped[str] = mapped_column(unique=True)
    ref = None
    gist: Mapped[str | None]

    async def save_gist(self, gist: str) -> None:
        self.gist = gist
        await self.save()

    def __str__(self) -> str:
        return self.repo
