from druks.exceptions import DruksError


class ServiceNotConnectedError(DruksError):
    """Druks has no identity connected for this service. The installation thus has
    nothing to act as there. The message names the service, so that each refusal says
    what to do."""

    def __init__(self, service: str, message: str = "") -> None:
        self.service = service
        super().__init__(
            message
            or f"{service} is not connected. Connect it in Settings → Connections → Services."
        )


class ServiceDisabledError(ServiceNotConnectedError):
    """druks.toml disables the service. This installation thus has nothing to act as
    there. A caller that copes with no identity copes with this the same way."""

    def __init__(self, service: str) -> None:
        super().__init__(service, f"Configuration disables {service}. Remove [services.{service}].")


class ServiceConnectError(DruksError):
    """A refused connect. The service's ``verify`` writes the message. It is safe to
    show. It never quotes anything that the operator pasted."""


class ServiceManagedError(DruksError):
    """Configuration owns the service's credentials."""

    def __init__(self, service: str) -> None:
        super().__init__(
            f"Configuration sets the credentials of {service}. Change druks.toml and its secrets."
        )


class OauthPageError(DruksError):
    """A failure on an OAuth door that a browser opens: the connect route and the
    callback route. Their failures render a page for the operator, not the JSON
    envelope."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class OauthExchangeError(DruksError):
    """An OAuth connect flow did not complete: an unknown or expired state, or a
    refused code exchange. Druks stores nothing on a failure. A new connect flow is
    thus always safe. ``context`` is the stash of the begun flow when the state
    resolved. It is empty when the state did not resolve."""

    def __init__(self, provider: str, reason: str, *, context: dict) -> None:
        super().__init__(f"OAuth exchange for {provider!r} failed: {reason}. Connect it again.")
        self.provider = provider
        self.reason = reason
        self.context = context


class OauthRefreshError(DruksError):
    """Druks did not mint an access token from a stored grant. The provider refused
    the refresh token, Druks cannot get to the token endpoint, or a concurrent refresh
    never freed the lock. A new connect replaces the grant."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"OAuth refresh for {provider!r} failed: {reason}. Connect it again.")
        self.provider = provider
        self.reason = reason


class ResourceError(DruksError):
    """A step on a resource failed at the manager. ``status_code`` and ``reason``
    are the manager's answer. A step that got no answer has no status code. Its reason
    is then the transport error."""

    def __init__(
        self, step: str, resource: str, reason: str, *, status_code: int | None = None
    ) -> None:
        answer = f"{status_code} {reason}".strip() if status_code else reason
        super().__init__(
            f"{step} on {resource} failed: {answer}. Fix what the answer names, then try again."
        )
        self.status_code = status_code
        self.reason = reason
