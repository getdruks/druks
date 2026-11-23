from druks.exceptions import DruksError


class PageRouteError(DruksError):
    """An app's pages cannot make a route table. Druks raises it at declaration for a
    nested child. It raises it at boot for one of these:

    - Two landing pages
    - Two pages with the same name
    - Two routes that match the same request
    - A signature that does not match its route
    - A navigation entry that is not a static top-level page.
    """


class PageReadError(DruksError):
    """Druks failed to read a page. The message names the app and the page. What the
    app's own code said stays in the process log, because it can carry a query, a URL, or
    a credential."""

    def __init__(self, app: str, page: str, detail: str) -> None:
        super().__init__(
            f"Druks failed to read page {page!r} of app {app!r}: {detail}. Fix the page in "
            "the app's code."
        )
        self.app = app
        self.page = page


class PageContractError(PageReadError):
    """The page that a function returned does not obey the contract. Druks wrote this message.
    The dashboard thus shows it whole."""


class NoTextFormError(DruksError):
    """A block that the operator acts through has no text form. Its ``to_markdown()``
    raises this error. A review context or a status then cannot hold the block. An action or
    a form in a review context is the exception. It holds the address of its page. It thus
    has a text form."""

    def __init__(self, block: str) -> None:
        super().__init__(
            f"{block} has no text form here. Remove the {block}. Show it on a page instead."
        )
        self.block = block
