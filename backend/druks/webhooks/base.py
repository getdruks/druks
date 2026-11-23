import hashlib
import json
import logging

from fastapi import HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import compile_path

from druks.accounts.context import Actor, current_actor
from druks.accounts.exceptions import InvalidAssertionError
from druks.accounts.jwt import verify_claims
from druks.exceptions import PermissionDenied
from druks.registry import service_registry, webhook_registry
from druks.settings import Settings

from .deliveries import mark_delivery, release_delivery

logger = logging.getLogger(__name__)


class Webhook:
    """The base class of one webhook of a provider: one category of its events.

    A subclass sets ``provider`` and ``category``, or overrides ``path``. It then
    implements ``request_is_authentic``, ``get_action``, and one ``on_<action>`` method
    for each action that it handles. The base class reads the body, checks the
    authentication, ignores a duplicate delivery, and calls the right ``on_<action>``
    method.

    Set ``abstract = True`` on a class that Druks must not register. An example is an
    intermediate base that other subclasses inherit from.
    """

    abstract: bool = False
    provider: str | None = None
    category: str | None = None
    path: str | None = None
    # __init_subclass__ sets it on a concrete subclass.
    pattern = None

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)

        # A class that does not set ``abstract`` itself is not abstract. A parent thus
        # does not pass its abstractness to its subclasses. Only ``cls.__dict__``, not
        # the MRO, tells a declared value from an inherited one. Without this check, a
        # grandchild of an abstract base inherits ``abstract = True`` and never registers.
        if "abstract" not in cls.__dict__:
            cls.abstract = False

        if cls.abstract:
            return

        if not cls.path:
            if not cls.provider or not cls.category:
                raise TypeError(
                    f"Webhook {cls.__name__} has no path. Set `path`, or set both "
                    "`provider` and `category`."
                )
            cls.path = f"{cls.provider}/{cls.category}/"

        # Starlette's compile_path anchors to ``^/...$``. Druks stores the convention path
        # with no leading slash, such as ``"github/events/"``. The path then joins the
        # catch-all ``{hook_path:path}`` route of FastAPI. Druks compiles it with a leading
        # slash, so that the regex matches the shape that the router gives.
        cls.pattern, _, _ = compile_path("/" + cls.path)
        webhook_registry.register(cls)

    def __init__(self, request: Request, kwargs: dict, settings: Settings) -> None:
        self.request = request
        self.kwargs = kwargs
        self.settings = settings
        self.raw_body: bytes = b""
        self._data_cached = _UNSET

    @property
    def data(self):
        if self._data_cached is _UNSET:
            self._data_cached = self.get_data()
        return self._data_cached

    def get_data(self):
        """Parse the request body. The default decodes the raw bytes as JSON. Override it
        for a form-encoded payload or a signed envelope."""
        return json.loads(self.raw_body)

    def get_action(self) -> str:
        """Return the token that selects the ``on_<action>`` method that runs. The base
        class calls ``getattr(self, f"on_{action}")``. Return a string that is a valid
        Python identifier. Define the method that matches it."""
        raise NotImplementedError

    async def request_is_authentic(self) -> bool:
        """Return whether the request comes from the provider that it names. ``False``
        refuses the request with a generic 401. A subclass can also raise
        ``HTTPException`` itself for a more specific answer, for example 403
        "Repository not allowed"."""
        raise NotImplementedError

    def get_delivery_key(self) -> str | None:
        """A stable key that delivers each event at most once, or ``None``, which disables
        the check. The default is ``None``. A subclass returns a key such as the
        ``x-github-delivery`` header or a hash of the payload. The base class stores the
        key with ``deliveries.mark_delivery``."""
        return

    def get_actor_id(self) -> str | None:
        """The provider's id for the person whose request this delivery is, or None when
        the delivery is no person's request.

        A run that the delivery starts runs as that person's account. Druks finds the
        account under the authority that the provider's ``Authentication.get_authority()``
        names.
        """
        return

    async def on_duplicate(self) -> Response:
        """The answer to a delivery of a key that Druks already stored. The default is a
        200 with a small JSON body. The provider then gets a success response and does not retry.
        The operator can find the replays in the logs."""
        return Response(
            content=b'{"accepted":false,"duplicate":true}',
            media_type="application/json",
        )

    async def on_unhandled(self, action: str) -> Response:
        """The answer when no ``on_<action>`` method matches. The default is a 200, so
        that an unknown event type of the provider does not start its retry loop.
        Override it to log or to raise."""
        return Response(
            content=b'{"accepted":true,"handled":false}',
            media_type="application/json",
        )

    def log_ignored(self, *, event: str, reason: str, **extra) -> None:
        # Operators search for ``webhook_ignored_reason=`` to find the events that
        # arrived and fired no handler. Keep the shape of the message stable.
        fields = {"webhook_event": event, "webhook_ignored_reason": reason, **extra}
        logger.info("webhook ignored: %s (%s)", reason, event, extra=fields)

    async def request_is_manager_signed(self) -> bool:
        """Whether the manager signed this forwarded event for this Druks: a JWT in
        ``X-Druks-Signature`` over the delivery and the body's SHA-256."""
        manager = self.settings.manager
        try:
            claims = await verify_claims(
                self.request.headers.get("x-druks-signature", ""),
                jwks_url=manager.jwks_url,
                issuer=manager.issuer,
                audience=manager.audience,
            )
        except InvalidAssertionError:
            return False
        delivery = f"{self.provider}:{self.get_delivery_key()}"
        digest = hashlib.sha256(self.raw_body).hexdigest()
        return claims.get("sub") == delivery and claims.get("body_sha256") == digest

    async def respond(self, session: AsyncSession) -> Response:
        self.raw_body = await self.request.body()

        # The manager forwards a managed service's events with its own signature.
        service = service_registry.get(self.provider)
        if service and service.is_managed():
            authentic = await self.request_is_manager_signed()
        else:
            authentic = await self.request_is_authentic()
        if not authentic:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="The webhook signature is not correct. Make sure that Druks trusts the "
                "key or the secret that the sender signs with.",
            )

        provider = self.provider or self.path
        key = self.get_delivery_key()
        if not await mark_delivery(provider, key):
            return await self.on_duplicate()

        # The claim stays for the whole attempt, so that a concurrent duplicate still stops early.
        # If the handler or its commit fails, Druks releases the claim. The retry of the
        # provider then runs the handler again. Without the release, the retry counts as a
        # duplicate until the TTL of the claim ends.
        try:
            action = self.get_action()
            handler = getattr(self, f"on_{action}", None)
            actor = None
            if actor_id := self.get_actor_id():
                actor = Actor(authority=service.Authentication.get_authority(), user_id=actor_id)
            token = current_actor.set(actor)
            try:
                if handler:
                    response = await handler()
                else:
                    response = await self.on_unhandled(action)
            finally:
                current_actor.reset(token)
            await session.commit()
            return response
        except PermissionDenied as error:
            # Druks refused the account of the delivery. A second delivery gets the
            # same answer. The claim thus stays. The sender gets an acknowledgement.
            await session.rollback()
            logger.info("webhook refused: %s", error)
            return Response(
                content=b'{"accepted":false,"refused":true}', media_type="application/json"
            )
        except BaseException:
            await release_delivery(provider, key)
            raise


class _Unset:
    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET = _Unset()
