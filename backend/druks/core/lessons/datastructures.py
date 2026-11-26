from dataclasses import dataclass

from druks.sandbox.datastructures import HomeFile


@dataclass(frozen=True)
class Vault:
    """The lessons as a box holds them: the files that Druks writes into it, and the
    index that the prompt carries. An installation with no lesson has neither."""

    files: tuple[HomeFile, ...] = ()
    index: str = ""
