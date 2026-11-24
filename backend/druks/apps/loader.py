import importlib
import importlib.util
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from druks.apps import App
from druks.models import Base
from druks.registry import app_registry, iter_apps
from druks.settings import get_settings

from . import exceptions
from .settings import field_kind

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint
    from types import ModuleType

    from fastapi import FastAPI

_GROUP = "druks.apps"


def load_installed_apps() -> None:
    """Fill the apps registry with every installed app that druks.toml leaves
    enabled, and check that each secret file of an app names a Secret setting of it.
    A process calls this once where it starts."""
    # A model can name a model of another package: Account.teams names Team. The
    # name resolves only when that package is loaded, so a process loads it here.
    import druks.teams.models  # noqa: F401

    tables = get_settings().apps
    entries = entry_points(group=_GROUP)
    for name in tables:
        if name not in entries.names:
            raise exceptions.AppNotFound(
                f"druks.toml or a secret file configures the app {name!r}, and no installed "
                f"package provides it. Remove [apps.{name}] and each file apps.{name}.*."
            )
    disabled = {name for name, table in tables.items() if not table.get("enabled", True)}
    app_registry.clear()
    for entry in entries:
        if entry.name in disabled:
            continue
        app = entry.load()
        if not (isinstance(app, type) and issubclass(app, App)):
            raise TypeError(f"app entry point {entry.name!r} is not an App")
        if app.name != entry.name:
            raise exceptions.MalformedApp(
                f"app {entry.name!r} entry point resolves to an App named "
                f"{app.name!r} — the entry-point name must match App.name"
            )
        app_registry.register(app)
    for name, table in tables.items():
        app = app_registry.get(name)
        fields = app.settings_model.model_fields if app and app.settings_model else {}
        for field in set(table) - {"enabled"}:
            if field not in fields or field_kind(fields[field]) != "secret":
                raise exceptions.SecretFileError(
                    f"The file apps.{name}.{field} in DRUKS_SECRETS_DIR names no Secret "
                    "setting of an enabled app. Remove the file."
                )


def load_app(name: str) -> type[App]:
    """Load one installed app without the web app: resolve its entry point,
    register its tables, and import its capability modules so its workflows,
    routes, subscribers, and webhooks self-register. Returns the ``App``
    class, with every surface then enumerable off it (``workflows()``,
    ``routers()``, ``capability_modules()``, ``settings_model``,
    ``migrations_dir()``). The load path used by the CLI, tests, and evals —
    no FastAPI, nothing mounted.

    Fails loudly and by name: an uninstalled package raises ``AppNotFound``;
    an entry point that doesn't resolve to an ``App`` raises
    ``MalformedApp``; the app's own code raising on import raises
    ``AppImportError``; a declared subject that fails the read-side contract
    raises ``AppSubjectContractError``; a page table a request could not
    resolve raises ``PageRouteError``."""
    app = resolve_app(name)
    try:
        import_app_models(app)
        app.discover()
    except Exception as error:
        raise exceptions.AppImportError(f"app {name!r} failed to import: {error}") from error
    # Outside the try: a contract break is not an import error.
    app.subjects()
    app.navigation_pages()
    app.operations()
    return app


def resolve_app(name: str) -> type[App]:
    """The single installed app named ``name``, loaded to its ``App``
    class. Entry points are listed, not imported, so an unknown name fails before
    any app code runs."""
    matches = [e for e in entry_points(group=_GROUP) if e.name == name]
    if not matches:
        raise exceptions.AppNotFound(f"no installed app named {name!r} — install its package first")
    if len(matches) > 1:
        # Two installed distributions register the same entry-point key — the name
        # keys the /api, settings, and migration namespaces, so this is a broken
        # install. Caught from metadata alone, without importing anything.
        raise exceptions.MalformedApp(
            f"app {name!r} is declared by {len(matches)} installed packages "
            f"({', '.join(e.value for e in matches)}) — uninstall all but one"
        )
    app = _load_entry(matches[0])
    # The entry-point key must equal the class's ``name`` — the key scopes the
    # /api, settings, and migration namespaces, which is what lets the
    # duplicate-key check above stand in for a duplicate-name check without
    # importing sibling apps.
    if app.name != name:
        raise exceptions.MalformedApp(
            f"app {name!r} entry point resolves to an App named "
            f"{app.name!r} — the entry-point name must match App.name"
        )
    return app


def _load_entry(entry: "EntryPoint") -> type[App]:
    """Resolve one entry point to its ``App`` class, distinguishing a
    packaging mistake from the app's own code failing on import. A missing
    target module or attribute is ``MalformedApp`` (bad metadata); the
    target module existing but raising on import is ``AppImportError`` (the
    app's code) — the two the caller's taxonomy must tell apart."""
    try:
        module = importlib.import_module(entry.module)
    except ModuleNotFoundError as error:
        if error.name == entry.module or entry.module.startswith(f"{error.name}."):
            raise exceptions.MalformedApp(
                f"entry point {entry.value!r} points at a module that isn't installed: {error}"
            ) from error
        # A dependency the entry module imports is missing — its code ran and failed.
        raise exceptions.AppImportError(
            f"app entry module {entry.module!r} failed to import: {error}"
        ) from error
    except Exception as error:
        raise exceptions.AppImportError(
            f"app entry module {entry.module!r} failed to import: {error}"
        ) from error

    app = module
    for attribute in filter(None, (entry.attr or "").split(".")):
        try:
            app = getattr(app, attribute)
        except AttributeError as error:
            raise exceptions.MalformedApp(
                f"entry point {entry.value!r} names {attribute!r}, which its module doesn't define"
            ) from error
    if not (isinstance(app, type) and issubclass(app, App)):
        raise exceptions.MalformedApp(f"entry point {entry.value!r} is not an App")
    return app


def _tables_declared_in(package: str) -> set[str]:
    # A table belongs to whoever declared its model, not to whoever happened to import
    # it first: an app entry module pulls its own models in, and a sibling's.
    return {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if mapper.class_.__module__.startswith(f"{package}.") and mapper.local_table is not None
    }


def import_app_models(only: type[App] | None = None) -> None:
    """Import apps' ``<package>.models`` so their tables register on the shared
    metadata before ``create_all`` or autogenerate. Defaults to every installed
    app (boot, migrations); pass ``only`` to register a single app's tables
    for a headless load. A separately-shipped app's tables must carry its
    ``<name>_`` prefix — the platform scopes its migrations by that prefix, so an
    unprefixed table would be invisible to them and fails the load instead. Builtin
    apps are exempt: their schema is core's."""
    selected = [only] if only else iter_apps()
    for app in selected:
        name = f"{app.package}.models"
        if not importlib.util.find_spec(name):
            continue
        importlib.import_module(name)
        misnamed = sorted(
            table
            for table in _tables_declared_in(app.package)
            if not table.startswith(app.table_prefix)
        )
        if misnamed and app.prefix_tables and not app.builtin:
            raise ValueError(
                f"app {app.name!r} tables must start with {app.table_prefix!r}: {misnamed}"
            )


def mount(api: "FastAPI", app: type[App], modules: list["ModuleType"]) -> None:
    """Mount one discovered app under ``/api/<name>`` and ``/app/<name>``.
    The prefix and the identity gate are the loader's — no app hook can
    override them."""
    # Local, matching get_routers: the loader stays importable headlessly.
    from fastapi import Depends

    from druks.accounts.dependencies import current_account
    from druks.chat.dependencies import current_conversation
    from druks.teams.dependencies import require_app_access

    prefix = f"/api/{app.name}"
    for router in app.get_routers(modules):
        api.include_router(
            router,
            prefix=prefix,
            tags=[app.name],
            dependencies=[
                Depends(current_account),
                Depends(current_conversation),
                Depends(require_app_access(app)),
            ],
        )
    dist = app.frontend_dist()
    if dist:
        # /app, not /api: unknown /api/* paths stay JSON 404s.
        api.frontend(f"/app/{app.name}", directory=dist)


def load(api: "FastAPI") -> None:
    """API boot: for each app — discover, validate subjects and pages, mount."""
    # The table-prefix check runs here, not just in makemigrations — an author
    # who hand-writes migrations still can't boot with an unprefixed table.
    import_app_models()
    for app in iter_apps():
        modules = app.discover()
        app.subjects()
        app.navigation_pages()
        app.operations()
        mount(api, app, modules)
