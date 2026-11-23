import importlib.util
import itertools
import json
import re
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import BaseModel, Field, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

# Every layer imports druks.apps. This module thus loads no models. A method that
# reads the database imports what it needs when it runs.
from druks.exceptions import ObjectNotFound
from druks.redis import get_client
from druks.registry import Registry, autodiscover, register_app
from druks.settings import get_settings
from druks.ui.exceptions import PageContractError, PageReadError, PageRouteError

from .exceptions import AppRouteConflict, AppSubjectContractError, SettingsDeclarationError
from .schemas import Operation
from .settings import (
    coerce_setting_value,
    field_kind,
    field_live_choices,
    redacted_validation_message,
    validate_setting_override,
    validate_settings_declaration,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from druks.chat.bots.base import Bot
    from druks.doctor import CheckResult
    from druks.durable.datastructures import Subject
    from druks.durable.models import Run
    from druks.durable.schemas import SubjectStatus, SubjectSummary
    from druks.models import StoredSubject
    from druks.ui import Page
    from druks.ui.page import PageRoute

    # A check that the app owns returns a verdict on one of its own preconditions. It
    # uses the same ``CheckResult`` shape as a core check. It can be sync or async.
    Check = Callable[[], "CheckResult | Coroutine"]


# An app name keys the ``/api/<name>`` namespace and the ``alembic_version_<name>``
# table. It also keys the ``<name>_`` table prefix and the ``app:<name>:`` settings.
# It must thus be a lowercase identifier that is safe in SQL and in a URL. The
# scaffolder validates a name against the same rule, so that ``druks create app``
# cannot emit a package that fails this check.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# The platform serves these segments under ``/api/<name>`` for every app: the
# agent-call reads, the page snapshots, and the upload route. Neither a subject type
# nor an app router can take one. An app thus never shadows a platform read.
RESERVED_SEGMENTS = frozenset({"transcripts", "pages", "uploads"})


# A settings field of the secret kind. An unset field is an empty ``SecretStr``, which
# is falsy. ``if self.token:`` thus tells whether the field is set.
# ``.get_secret_value()`` never needs a guard.
Secret = Annotated[SecretStr, Field(default=SecretStr(""))]


class AppSettings(BaseModel):
    def clean(self) -> dict[str, str]:
        """The problems of the resolved settings, keyed by the field that the operator
        must fix. The settings form refuses a save that creates a problem. Doctor reports
        the problems of the settings that Druks already stores."""
        return {}


class _OwnRegistry:
    """A registry that each App class has for itself."""

    def __init__(self, name: str, *, key: Callable[..., str]) -> None:
        self._name = name
        self._key = key

    def __set_name__(self, owner: type, attribute: str) -> None:
        self._attribute = f"_{attribute}"

    def __get__(self, instance: object, owner: type) -> Registry:
        # Python names the attributes of a class before it runs __init_subclass__. The
        # first reader thus makes the class its registry.
        if self._attribute not in owner.__dict__:
            setattr(owner, self._attribute, Registry(self._name, key=self._key))
        return owner.__dict__[self._attribute]


class App:
    """A pluggable application. To make one, subclass it and set ``name``. Then
    register the subclass under the ``druks.apps`` entry-point group. At boot, the
    loader calls ``discover``, which imports the package's modules that have the
    conventional names. That import is where the app's webhooks, workflows,
    instructions, and subscribers register themselves.

    Druks uses an app as a class and never makes an instance. An app is a stateless
    singleton of the installation. An instance has no use.
    """

    name: ClassVar[str]
    # The names of the tables that this app owns start with ``<name>_``, which comes
    # from ``name``. The prefix is the SQLAlchemy equivalent of a Django ``app_label``.
    # The platform scopes the app's autogenerate to this prefix. It tracks the app's
    # history in ``alembic_version_<name>``.
    table_prefix: ClassVar[str]
    # Whether this app's tables must carry the ``<name>_`` prefix. It is True for an
    # app that ships normally. The schema of such an app thus cannot collide with core
    # or with another app. A bundled app from before the prefix convention keeps its
    # tables in the platform's own migration history. Such an app sets this to False.
    # The boot-time check then skips its tables.
    prefix_tables: ClassVar[bool] = True
    # The rail glyph, by its name in the Lucide set that the frontend bundles, for
    # example "telescope" or "hammer". The UI's APP_ICONS lists the available names. An
    # app only names a glyph. A package that ships on its own thus gets a glyph with no
    # change to the frontend. An unknown name gets the default glyph.
    icon: ClassVar[str] = "box"
    # The one-line description that the settings pane shows when a person selects the app.
    description: ClassVar[str] = ""
    # The app's navigation tabs, as page names in the order that they show. Each one
    # names a static top-level page. The tab shows that page's label. An app thus never
    # writes a label twice. An app that ships its own frontend declares its tabs there
    # instead.
    navigation: ClassVar[list[str]] = []
    # The app's top-level package, which ``discover`` walks. It defaults to the package
    # that defines the subclass. By the ``<package>/app.py`` convention, that package is
    # always the app's root. An app thus sets it only when the class is outside its
    # package root.
    package: ClassVar[str]
    # A ``builtin`` app carries settings of the platform core, not of an app that people
    # use. The settings UI shows its instructions in the Druks tab. It gives the app no
    # tab of its own.
    builtin: ClassVar[bool] = False
    # The app's declared ``Settings`` inner class, if it has one. It holds the values
    # that the operator sets for the app itself, not for one of its workflows. It
    # has the same role as the ``Settings`` of a workflow.
    settings_model: ClassVar[type[AppSettings] | None] = None
    # What the app answers people with in chat. Declare it as ``bot = Bot(...)``.
    bot: "ClassVar[Bot | None]" = None
    # The checks that this app adds to ``druks doctor``, one for each precondition beyond
    # the coherence of the resolved settings. An example is whether its provider
    # answers. ``druks doctor`` runs each one through the same ``CheckResult`` report
    # as its core checks. It reports a check that raises under this app's name, so that
    # the check cannot hide a core failure. The default is none. Declare a list to add
    # them.
    checks: "ClassVar[list[Check]]" = []
    # What this app's code declares. Each thing registers itself here when Python
    # defines it.
    workflows = _OwnRegistry("workflows", key=lambda cls: cls.kind)
    instructions = _OwnRegistry("instructions", key=lambda instruction: instruction.id)
    page_routes = _OwnRegistry("pages", key=lambda page_route: page_route.key)
    browser_sessions = _OwnRegistry("browser_sessions", key=lambda session: session.name)

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "name", None)
        if not name:
            raise TypeError(
                f"{cls.__name__} must set a `name`. Set `name` to a lowercase identifier."
            )
        if not NAME_RE.match(name):
            raise TypeError(
                f"app name {name!r} must match {NAME_RE.pattern!r}. It keys the /api/<name> "
                "namespace, the version table, and the settings keys. Rename the app to "
                "match the pattern."
            )
        if "subject_type" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} declares subject_type. A workflow declares what it is "
                "about, with ``subject = YourSubject``. An app's subjects follow from its "
                "workflows. Remove subject_type from the app."
            )
        cls.table_prefix = f"{name}_"
        if "package" not in cls.__dict__:
            cls.package = cls.__module__.rpartition(".")[0]
        declared = cls.__dict__.get("Settings")
        if declared:
            if not isinstance(declared, type) or not issubclass(declared, AppSettings):
                raise SettingsDeclarationError(
                    f"{cls.__name__}.Settings must subclass AppSettings. Declare it as "
                    "``class Settings(AppSettings)``."
                )
            validate_settings_declaration(declared)
            cls.settings_model = declared
        # Druks registers the app here, so that the package's models, workflows and pages
        # find their app as Python defines them.
        register_app(cls)

    @classmethod
    async def settings(cls) -> AppSettings:
        """The app's settings. A field takes its secret file's value, else the operator's
        override, else its default. Raises ``TypeError`` when the app declares no
        ``Settings``. Raises ``ValueError`` without the refused value when the settings
        refuse a value."""
        from druks.db import db_session
        from druks.user_settings.models import SettingsOverride

        model = cls.settings_model
        if not model:
            raise TypeError(
                f"app {cls.name!r} declares no Settings. Declare a Settings class on the app."
            )
        values = {
            name: await SettingsOverride.app_setting(
                db_session(),
                cls.name,
                name,
                field.default,
                is_secret=field_kind(field) == "secret",
            )
            for name, field in model.model_fields.items()
        }
        values.update(cls.configured_settings())
        try:
            return model.model_validate(values)
        except ValidationError as error:
            # A validator can quote the value that it refuses. A value can be a secret.
            raise ValueError(
                f"The settings of {cls.name} are invalid: "
                f"{redacted_validation_message(model, error)}. Fix the settings that this "
                "message names."
            ) from None

    @classmethod
    def configured_settings(cls) -> dict[str, str]:
        """The secret settings that configuration sets. Each one is the file
        ``apps.<name>.<field>`` in DRUKS_SECRETS_DIR, which Druks reads at startup. The
        settings form cannot change them."""
        table = get_settings().apps.get(cls.name, {})
        return {field: value for field, value in table.items() if field != "enabled"}

    @classmethod
    async def override_setting(cls, field: str, value) -> None:
        """Save an operator's override of one declared setting. ``None`` removes the
        override. The declared default then applies. Raises ``ValueError``, which the API
        answers with 422."""
        from druks.db import db_session
        from druks.user_settings.models import SettingsOverride

        model = cls.settings_model
        if not model or field not in model.model_fields:
            raise ValueError(
                f"{cls.name} has no setting {field!r}. Set a field that its Settings declares."
            )
        if field in cls.configured_settings():
            raise ValueError(
                f"Configuration sets {field}. Change the file apps.{cls.name}.{field} in "
                "DRUKS_SECRETS_DIR."
            )
        if value is not None:
            value = coerce_setting_value(model, field, value)
            validate_setting_override(model, (await cls.settings()).model_dump(), field, value)
            live = field_live_choices(model.model_fields[field])
            if live and not await live.accepts(value):
                raise ValueError(
                    f"{field}: {value!r} is not one of the listed choices. Select a listed choice."
                )
        await SettingsOverride.set_app_setting(
            db_session(),
            cls.name,
            field,
            value,
            is_secret=field_kind(model.model_fields[field]) == "secret",
        )

    @classmethod
    async def get_cached(cls, key: str):
        """What this app cached under ``key``. It is ``None`` when the app never set it,
        when its TTL ended, or when the app cleared it."""
        if stored := await get_client().get(cls._cache_key(key)):
            return json.loads(stored)

    @classmethod
    async def set_cached(cls, key: str, value, *, ttl: int) -> None:
        """Cache a JSON value under ``key`` for ``ttl`` seconds."""
        await get_client().set(cls._cache_key(key), json.dumps(value), ex=ttl)

    @classmethod
    async def clear_cached(cls, key: str) -> None:
        """Clear the value under ``key``."""
        await get_client().delete(cls._cache_key(key))

    @classmethod
    def _cache_key(cls, key: str) -> str:
        return f"druks:app:{cls.name}:{key}"

    @classmethod
    async def get_statuses_for_subjects(
        cls, session: AsyncSession, identities: list[tuple[str, str]]
    ) -> "dict[tuple[str, str], SubjectStatus]":
        """Where the work on each ``(subject_type, subject_id)`` stands, from the runs
        of this app alone."""
        from druks.durable import reads

        return await reads.get_statuses_for_subjects(
            session, identities, workflows=cls.workflows.all()
        )

    @classmethod
    async def read_ask(cls, session: AsyncSession, run: "Run") -> dict:
        """The ask that ``run`` waits on, as Druks shows it. Each status in the blocks of
        its context holds where the work on its subject stands at this read, from the runs
        of this app."""
        from druks import ui

        ask = await run.get_ask()
        # Only an in-app ask holds blocks. A context of text holds none.
        if ask.get("presentation") == "in_app" and (
            stack := ui.Stack.from_context(ask.get("context", ""))
        ):
            statuses = await cls.get_statuses_for_subjects(session, stack.subjects())
            ask["context"] = [
                block.model_dump(mode="json", by_alias=True, context={"statuses": statuses})
                for block in stack.blocks
            ]
        return ask

    @classmethod
    def subjects(cls) -> "list[type[Subject] | type[StoredSubject]]":
        """The subjects that this app's workflows declare, ordered by subject type. An
        identity-only ``Subject`` must implement ``list_summaries()``. A ``StoredSubject``
        has a board by default. The check compares method identity and does not call the
        method."""
        from druks.durable.datastructures import Subject

        stub = Subject.list_summaries.__func__
        declared = {workflow.subject for workflow in cls.workflows.all() if workflow.subject}
        for subject_class in declared:
            if subject_class.subject_type in RESERVED_SEGMENTS:
                raise AppSubjectContractError(
                    f"{subject_class.__name__} is a {subject_class.subject_type!r} subject. "
                    "That segment serves every app's platform reads. Name it for what it is."
                )
            if subject_class.list_summaries.__func__ is stub:
                raise AppSubjectContractError(
                    f"app {cls.name!r} declares subject {subject_class.__name__} "
                    "without list_summaries(). The board calls it. Implement "
                    f"list_summaries() on {subject_class.__name__}."
                )
        return sorted(declared, key=lambda subject_class: subject_class.subject_type)

    @classmethod
    def pages(cls) -> "list[PageRoute]":
        """The pages that this app declares, in route-match order. Like ``routers()``, it
        imports the app first. A headless caller thus reads the whole surface. The list
        is empty for an app that ships no ``pages.py``. Raises ``PageRouteError`` on a
        table that a request cannot resolve."""
        from druks.ui.page import list_pages_for_app

        cls.discover()
        return list_pages_for_app(cls)

    @classmethod
    def navigation_pages(cls) -> "list[PageRoute]":
        """The pages that ``navigation`` names, in the order that it names them. The
        shell shows one tab for each, with the page's label."""
        declared = {page.name: page for page in cls.pages()}
        chosen = []
        for name in cls.navigation:
            page = declared.get(name)
            if page and page.is_static and not page.parent:
                chosen.append(page)
                continue
            raise PageRouteError(
                f"app {cls.name!r} navigation names {name!r}. A navigation entry must be a "
                "static top-level page that the shell can open with no parameters. This app "
                f"declares {sorted(declared)}. Name one of its static top-level pages."
            )
        return chosen

    @classmethod
    def discover(cls) -> list[ModuleType]:
        """Import the app's capability modules, so that its webhooks, workflows,
        instructions, and subscribers register themselves. The default walks
        ``package``. Override it to customize discovery. It has the role of Django's
        ``AppConfig.ready``. An unusual app stays unusual through the override,
        not through a special case in the platform. Returns the imported modules, so
        that ``get_routers`` can read the routers from the ``routes`` modules."""
        return autodiscover(cls.package)

    @classmethod
    def capability_modules(cls) -> list[ModuleType]:
        """The app's imported capability modules: the ``routes``, ``subscribers``,
        ``workflows``, and ``webhooks`` leaves that ``discover`` walks. It lists the
        app's routes, subscribers and webhooks with no web app. Each ``@subscribe`` and
        each ``Webhook`` registers itself on import. The modules are thus the surface.
        It is an alias for ``discover``, named for the surface instead of the side effect."""
        return cls.discover()

    @classmethod
    def routers(cls) -> "list[APIRouter]":
        """Every router that the app mounts, listed without the web app: its declared
        ``routes`` routers and the read-sides that Druks gives every app. It builds the
        ``APIRouter`` objects. It makes no FastAPI app. A CLI or an eval can thus read the
        route surface of the app when the platform does not run."""
        return cls.get_routers(cls.discover())

    @classmethod
    def migrations_dir(cls) -> Path | None:
        """The root of the app's own migration history, ``<package>/migrations``. It is
        None when the app ships no migrations. Examples are a builtin app whose tables
        are in core's schema, and an app with no migrations yet. ``druks init-db``
        upgrades the ``versions/`` directory in it under ``alembic_version_<name>``."""
        package_dir = cls.package_dir()
        if package_dir:
            migrations = package_dir / "migrations"
            return migrations if (migrations / "versions").is_dir() else None
        return

    @classmethod
    def package_dir(cls) -> Path | None:
        """The filesystem root of ``package``, where the app's shipped assets that are
        not modules are, such as ``migrations/`` and ``dist/``. It is None when the
        package has no location, as for a namespace-less or frozen import."""
        spec = importlib.util.find_spec(cls.package)
        if spec and spec.submodule_search_locations:
            return Path(spec.submodule_search_locations[0])
        return

    @classmethod
    def get_data_dir(cls) -> Path:
        """The one directory that the app writes outside the database:
        ``<data_dir>/apps/<name>``. Druks makes it on first use. A cached tool binary or
        a tool's state is here, never under ``~``. The process user has no home that
        it can write. Only the instance data directory survives a release."""
        directory = get_settings().data_dir / "apps" / cls.name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @classmethod
    def frontend_dist(cls) -> Path | None:
        """The app's built frontend, ``<package>/dist``, if it ships one. A dist is an ESM
        module that the shell mounts, not a document. ``entry.js`` marks it. It is inside
        the package, not at the project root. The same path thus resolves for a wheel and
        for an editable install."""
        package_dir = cls.package_dir()
        if package_dir:
            dist = package_dir / "dist"
            return dist if (dist / "entry.js").is_file() else None
        return

    @classmethod
    def get_routers(cls, modules: list[ModuleType]) -> "list[APIRouter]":
        """Every router under the app's namespace: the routers that its ``routes``
        modules declare, and the generic read-sides that Druks gives every app. These are
        always ``/transcripts``, ``/pages`` and ``/uploads``, and one subject read-side
        for each subject that its workflows declare. A subject read-side,
        ``/<subject_type>``, serves the status, the timeline and a live stream. The
        platform's routers come first, because those segments are its own. Override it to
        add a router that you build outside a ``routes`` module."""
        # Each platform router holds only its own segment. Druks matches them first. That
        # costs an app nothing anywhere else. It also leaves the app no way to take a read
        # that the platform serves, not even with a catch-all.
        return [
            cls._get_transcript_routes(),
            cls._get_page_routes(),
            cls._get_upload_routes(),
            *(cls._get_subject_routes(subject) for subject in cls.subjects()),
            *cls._declared_routers(modules),
        ]

    @classmethod
    def _declared_routers(cls, modules: "list[ModuleType]") -> "list[APIRouter]":
        """The routers that the app itself declares in its ``routes`` modules."""
        # This import is local, not at the module top. It keeps FastAPI off the import
        # graph, so that the loader stays importable headlessly. Only this list of
        # routers needs FastAPI.
        from fastapi import APIRouter

        seen: set[int] = set()
        declared: list[APIRouter] = []
        routes_modules = [
            module for module in modules if module.__name__.rpartition(".")[2] == "routes"
        ]
        for module in routes_modules:
            for value in vars(module).values():
                if isinstance(value, APIRouter) and id(value) not in seen:
                    seen.add(id(value))
                    # These are the paths that the router mounts, with or without its
                    # prefix. A reserved first segment in any of them takes a
                    # platform read. Starlette route kinds differ. Some have no path.
                    taken = {
                        f"{value.prefix}{getattr(route, 'path', '')}".strip("/").partition("/")[0]
                        for route in value.routes
                    }
                    reserved = sorted(taken & RESERVED_SEGMENTS)
                    if reserved:
                        raise AppRouteConflict(
                            f"app {cls.name!r} mounts a router on {reserved}. Those segments "
                            "serve every app's platform reads. Name the router for its own "
                            "resource."
                        )
                    declared.append(value)
        return declared

    @classmethod
    def operations(cls) -> "dict[str, Operation]":
        """Every route that this app declares with an ``operation_id``, keyed by that
        id. An ``Action`` names one. The shell resolves it here, so that an author does
        not write a URL twice."""
        found: dict[str, Operation] = {}
        for router in cls._declared_routers(cls.discover()):
            for route in router.routes:
                # Starlette route kinds differ. Only an API route carries these.
                name = getattr(route, "operation_id", None)
                if not name:
                    continue
                if name.startswith(f"{cls.name}_"):
                    raise AppRouteConflict(
                        f"app {cls.name!r} declares operation {name!r}. Druks adds the app "
                        "name to every operation id. Declare "
                        f"{name.removeprefix(f'{cls.name}_')!r}."
                    )
                # An APIRoute's own path already carries its router's prefix.
                path = f"/api/{cls.name}{getattr(route, 'path', '')}"
                if name in found:
                    raise AppRouteConflict(
                        f"app {cls.name!r} declares operation {name!r} twice, on "
                        f"{found[name].method} {found[name].path} and on {path}. An "
                        "action names one operation. Give each route its own."
                    )
                methods = sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"})
                if len(methods) > 1:
                    raise AppRouteConflict(
                        f"app {cls.name!r} declares operation {name!r} on {methods}. An "
                        "action calls one method. Give each method its own route."
                    )
                found[name] = Operation(id=name, method=methods[0] if methods else "GET", path=path)
        return {name: found[name] for name in sorted(found)}

    @classmethod
    def _get_page_routes(cls) -> "APIRouter":
        """The page function is the endpoint. FastAPI thus validates every route
        parameter against the declared signature."""
        from fastapi import APIRouter

        from druks.ui import Page
        from druks.ui.page import PageRoute

        operations = cls.operations()
        declarations = cls.pages()
        landing = {declaration.route: declaration for declaration in declarations}.get("/")
        router = APIRouter(prefix="/pages", tags=[f"{cls.name}:pages"])
        for declaration in declarations:
            router.add_api_route(
                # The landing page's route is "/". Its snapshot answers at the bare
                # /pages.
                declaration.route.rstrip("/"),
                cls._page_endpoint(declaration, operations, back=declaration.parent or landing),
                methods=["GET"],
                response_model=Page,
                response_model_by_alias=True,
                name=declaration.name,
            )
        if not landing:
            router.add_api_route(
                "",
                cls._page_endpoint(PageRoute("/", cls._get_home_page), operations, back=None),
                methods=["GET"],
                response_model=Page,
                response_model_by_alias=True,
                name="home",
            )
        return router

    @classmethod
    async def _get_home_page(cls) -> "Page":
        """The home that an app gets when it declares no landing page. It has one live
        table for each subject type. The columns are the summary's own fields and where
        the work on each subject stands."""
        from druks import ui
        from druks.accounts.context import current_account_id

        def cell(value):
            match value:
                case bool():
                    return ui.TextValue("yes" if value else "no")
                case int() | float():
                    return ui.NumberValue(value)
                case datetime():
                    return ui.TimeValue(value)
                case str():
                    return ui.TextValue(value)
            return ui.TextValue("")

        account_id = current_account_id.get()
        sections = []
        for subject_class in cls.subjects():
            subject_type = subject_class.subject_type
            label = subject_type.replace("_", " ")
            summaries = await cls._list_summaries(subject_class, account_id)
            facts = [summary.model_dump(exclude={"id", "key"}) for summary in summaries]
            # A nested value has no cell. The summary's scalars are thus the columns.
            fields = [
                name
                for name in dict.fromkeys(itertools.chain.from_iterable(facts))
                if all(
                    isinstance(fact.get(name), str | bool | int | float | datetime | None)
                    for fact in facts
                )
            ]
            rows = []
            for summary, fact in zip(summaries, facts, strict=True):
                subject = ui.Follows(subject_type=subject_type, subject_id=str(summary.id))
                rows.append(
                    ui.TableRow(
                        [
                            ui.TextValue(summary.key, link=ui.Link(subject=subject)),
                            *(cell(fact.get(name)) for name in fields),
                            ui.SubjectStatus(subject),
                        ]
                    )
                )
            sections.append(
                ui.Section(
                    name=subject_type,
                    title=label,
                    follows=subject_class,
                    blocks=[
                        ui.Table(
                            columns=[
                                ui.TableColumn(label),
                                *(ui.TableColumn(name.replace("_", " ")) for name in fields),
                                ui.TableColumn("status"),
                            ],
                            rows=rows,
                            empty=ui.EmptyState(f"No {label} yet."),
                        )
                    ],
                )
            )
        return ui.Page(cls.name.replace("_", " "), description=cls.description, blocks=sections)

    @classmethod
    def _page_endpoint(
        cls,
        declaration: "PageRoute",
        operations: "dict[str, Operation]",
        *,
        back: "PageRoute | None",
    ):
        """``wraps`` keeps the page function's signature. FastAPI thus still validates
        every route parameter. For a page whose subject does not exist, the
        route answers with an empty state that links ``back``. The page serializes with where the
        work on each of its subjects stands, from one read."""
        from fastapi.responses import JSONResponse

        from druks.db import db_session
        from druks.ui import Action, EmptyState, Link, Page

        controls = []
        if back and back is not declaration:
            controls.append(Link(back.label, page=back.name))

        @wraps(declaration.function)
        async def read_page(**parameters):
            try:
                page = await declaration.function(**parameters)
            except ObjectNotFound as error:
                return Page(str(error), blocks=[EmptyState(str(error), controls=controls)])
            except Exception as error:
                raise PageReadError(
                    cls.name, declaration.name, f"its own code raised {type(error).__name__}"
                ) from error
            if not isinstance(page, Page):
                raise PageContractError(
                    cls.name,
                    declaration.name,
                    f"it answered with {type(page).__name__}, not a Page",
                )
            try:
                for action in page.iter_parts(Action):
                    action.check_operation(cls.name, operations)
            except ValueError as error:
                raise PageContractError(cls.name, declaration.name, str(error)) from error
            statuses = await cls.get_statuses_for_subjects(db_session(), page.subjects())
            return JSONResponse(
                page.model_dump(mode="json", by_alias=True, context={"statuses": statuses})
            )

        return read_page

    @classmethod
    def _get_transcript_routes(cls) -> "APIRouter":
        """The agent-call read-side that Druks gives every app. It has a paginated read
        and a live tail of a call's stdout and stderr. It also lists the call's artifact
        files (prompt, response, transcript streams, metadata) for download. The
        platform's own ``AgentCall`` keys it, under ``/api/<name>/transcripts``. An app
        writes none of it."""
        # Keep FastAPI and the durable read-side off the import graph, so that the loader
        # stays importable headlessly.
        import mimetypes
        from typing import Literal

        from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
        from fastapi.responses import FileResponse, StreamingResponse

        from druks.api.dependencies import EngineDep, SessionDep
        from druks.durable import reads
        from druks.durable.enums import AgentCallStatus
        from druks.durable.exceptions import AgentCallNotFound
        from druks.durable.live import EventStream
        from druks.durable.models import AgentCall
        from druks.durable.schemas import AgentCallFiles, TranscriptChunk

        default_limit = 64 * 1024
        max_limit = 256 * 1024
        # A call that no longer runs never appends to its log again. The byte range
        # that this serves is thus permanent. The cache is private, because a shared cache
        # must not keep a transcript that only some accounts can read.
        settled_cache = "private, max-age=31536000, immutable"

        async def require_own_call(request: Request, session: SessionDep) -> None:
            # The loader puts this router behind the access check of the app. Druks thus
            # answers a call of another app, or of an app that is not installed, as unknown.
            call = await AgentCall.get(session, request.path_params["call_id"])
            workflow = call.run.workflow
            if not workflow or workflow.app != cls.name:
                raise AgentCallNotFound(call.id)

        router = APIRouter(
            prefix="/transcripts/{call_id}",
            tags=[f"{cls.name}:transcripts"],
            dependencies=[Depends(require_own_call)],
        )

        @router.get("", response_model=TranscriptChunk, response_model_by_alias=True)
        async def get_transcript(
            session: SessionDep,
            call_id: str,
            stream: Literal["stdout", "stderr"],
            response: Response,
            offset: int = 0,
            limit: int = default_limit,
        ) -> TranscriptChunk:
            if offset < 0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"offset {offset} is less than 0. Pass an offset of 0 or more.",
                )
            if not 0 < limit <= max_limit:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"limit {limit} is not in 1..{max_limit}. Pass a limit in that range.",
                )
            call = await AgentCall.get(session, call_id)
            if call.live_status == AgentCallStatus.RUNNING:
                response.headers["Cache-Control"] = "no-store"
            else:
                response.headers["Cache-Control"] = settled_cache
            return reads.read_transcript_chunk(call, stream, offset=offset, limit=limit)

        @router.get("/stream", response_class=StreamingResponse)
        async def stream_transcript(
            call_id: str,
            stream: Literal["stdout", "stderr"],
            engine: EngineDep,
            offset: int = 0,
        ) -> StreamingResponse:
            if offset < 0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"offset {offset} is less than 0. Pass an offset of 0 or more.",
                )
            return EventStream(reads.stream_transcript(engine, call_id, stream, offset=offset))

        @router.get("/files", response_model=AgentCallFiles, response_model_by_alias=True)
        async def list_files(session: SessionDep, call_id: str) -> AgentCallFiles:
            return await reads.get_agent_call_files(session, call_id)

        @router.get("/files/{file_name:path}")
        async def get_file(
            session: SessionDep,
            call_id: str,
            file_name: str,
            disposition: Literal["inline", "attachment"] = "inline",
        ) -> FileResponse:
            call = await AgentCall.get(session, call_id)
            resolved = call.get_file_path(file_name)
            if not resolved:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "The call has no file of this name. Name a file that its file list shows.",
                )
            media_type, _ = mimetypes.guess_type(resolved.name)
            return FileResponse(
                resolved,
                media_type=media_type or "application/octet-stream",
                filename=resolved.name if disposition == "attachment" else None,
                headers={"Cache-Control": "private"},
            )

        return router

    @classmethod
    def _get_upload_routes(cls) -> "APIRouter":
        """The route that an UploadField posts to. The app that owns the file is this
        route's own. A caller thus cannot store bytes under the name of another app."""
        import mimetypes
        from pathlib import PurePosixPath

        from fastapi import APIRouter, HTTPException, UploadFile, status

        from druks.accounts.context import current_account_id
        from druks.files import File
        from druks.files.constants import MAX_UPLOAD_BYTES
        from druks.ui import FileSummary

        router = APIRouter(prefix="/uploads", tags=[f"{cls.name}:uploads"])
        limit_in_megabytes = MAX_UPLOAD_BYTES // (1024 * 1024)

        @router.post("", response_model=FileSummary, response_model_by_alias=True)
        async def upload(file: UploadFile) -> FileSummary:
            content = await file.read()
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    f"That file is larger than {limit_in_megabytes} MB. Choose a smaller one.",
                )
            name = PurePosixPath(file.filename or "file").name
            stored = await File.create(
                name=name,
                # The name gives the file its type, as it does for an agent's own files.
                # Druks does not use the type that the browser claims.
                content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                content=content,
                app=cls.name,
                uploaded_by=current_account_id.get(),
            )
            return FileSummary.model_validate(stored)

        return router

    @classmethod
    async def _list_summaries(
        cls, subject_class: "type[Subject] | type[StoredSubject]", account_id: str | None
    ) -> "Sequence[SubjectSummary]":
        """The rows of this app's board for ``subject_class``. A subject that another
        app's package declares lists that app's work. The board thus lists the subjects
        that this app's own runs keep open."""
        module = subject_class.__module__
        if module == cls.package or module.startswith(cls.package + "."):
            return await subject_class.list_summaries(account_id)
        subjects = await subject_class.list_open(workflows=cls.workflows.all())
        return [subject.get_summary() for subject in subjects]

    @classmethod
    def _get_subject_routes(
        cls, subject_class: "type[Subject] | type[StoredSubject]"
    ) -> "APIRouter":
        """The board and one subject (header, status, timeline and phase). Each has a
        point-in-time read and a ``/stream`` that pushes the whole snapshot on a change.
        Druks mounts them at ``/api/<name>/<subject_type>`` for every subject that the
        app's workflows declare. Identity keys every read here. An app that keeps no row
        for its subject thus gets the same surface as one that does. The board reads pass
        the caller to ``list_summaries``."""
        from fastapi import APIRouter, Depends, HTTPException, status
        from fastapi.responses import StreamingResponse

        from druks.accounts.context import current_account_id
        from druks.accounts.dependencies import current_account
        from druks.accounts.models import Account
        from druks.api.dependencies import EngineDep, SessionDep
        from druks.database import session_scope
        from druks.durable import reads
        from druks.durable.live import EventStream, stream
        from druks.durable.schemas import SubjectList, SubjectResponse, SubjectRow

        subject_type = subject_class.subject_type
        router = APIRouter(prefix=f"/{subject_type}", tags=[f"{cls.name}:{subject_type}"])

        async def board(session: AsyncSession, account_id: str | None) -> SubjectList:
            summaries = await cls._list_summaries(subject_class, account_id)
            statuses = await reads.get_subject_statuses(
                session,
                subject_type,
                [summary.id for summary in summaries],
                workflows=cls.workflows.all(),
            )
            return SubjectList(
                rows=[
                    SubjectRow(summary=summary, status=statuses[summary.id])
                    for summary in summaries
                ]
            )

        async def subject_response(
            session: AsyncSession, subject_id: str, kinds: list[str]
        ) -> SubjectResponse | None:
            if subject := await subject_class.get_or_none(id=subject_id):
                return await reads.get_subject_response(
                    session,
                    subject_type,
                    subject_id,
                    summary=subject.get_summary(),
                    kinds=kinds,
                )

        @router.get("", response_model=SubjectList, response_model_by_alias=True)
        async def list_subjects(session: SessionDep) -> SubjectList:
            return await board(session, current_account_id.get())

        # ``/stream`` comes before ``/{subject_id}``, so that FastAPI matches the literal path
        # before the id matcher.
        @router.get("/stream", response_class=StreamingResponse)
        async def stream_board(engine: EngineDep) -> StreamingResponse:
            # The generator polls after the request context ends. Capture the caller here.
            account_id = current_account_id.get()

            async def snapshot() -> SubjectList:
                async with session_scope(engine) as session:
                    return await board(session, account_id)

            return EventStream(stream(snapshot))

        # A subject id is whatever identifies the subject, such as "7" for a row or
        # "owner/repo#7" for a pull request. The id matcher thus spans separators. The
        # route of the subject's own ``/stream`` comes before it, so that the greedy match
        # does not take that path.
        @router.get("/{subject_id:path}/stream", response_class=StreamingResponse)
        async def stream_subject(
            subject_id: str, engine: EngineDep, account: Account = Depends(current_account)
        ) -> StreamingResponse:
            # The generator polls after the request context ends. Read the caller's apps here.
            kinds = await account.list_usable_kinds()

            async def snapshot() -> SubjectResponse | None:
                async with session_scope(engine) as session:
                    return await subject_response(session, subject_id, kinds)

            return EventStream(stream(snapshot))

        @router.get(
            "/{subject_id:path}", response_model=SubjectResponse, response_model_by_alias=True
        )
        async def read_subject(
            session: SessionDep, subject_id: str, account: Account = Depends(current_account)
        ) -> SubjectResponse:
            response = await subject_response(
                session, subject_id, await account.list_usable_kinds()
            )
            if not response:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"Druks has no {subject_type} {subject_id!r}. Open a {subject_type} that the "
                    "board lists.",
                )
            return response

        return router

    @classmethod
    async def on_startup(cls) -> None:
        """Converge the runtime state when the API process boots, after the apps load and
        DBOS launches. It does nothing by default. An app overrides it, for example to sync
        schedules. The caller logs a failure and continues. One app thus cannot stop the
        boot."""
