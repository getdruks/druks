import importlib
import pkgutil
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any

from druks.exceptions import AppsNotRegisteredError

if TYPE_CHECKING:
    from druks.apps import App
    from druks.browser.sessions import BrowserSession
    from druks.instructions import Bot, Instruction
    from druks.workflows import Workflow

# Leaf-module names that carry self-registering capabilities. ``autodiscover``
# imports exactly these (``routes`` defines routers the loader mounts, ``pages``
# defines the app's screens; the rest fire registration as an import side
# effect). The set is the single source of truth for what "a capability module"
# is named.
_ROLES = frozenset(
    {
        "webhooks",
        "subscribers",
        "workflows",
        "tasks",
        "routes",
        "pages",
        "services",
        "channel",
        "hooks",
    }
)


class Registry:
    """A named collection of self-registering capabilities, keyed by a string
    derived from each item. Re-registering the *same* item is idempotent (a
    re-imported module); a *different* item on an existing key is a collision and
    raises — two capabilities can't share a durable identity. ``all()`` returns
    the items in a stable order; ``get()`` resolves one by key."""

    def __init__(self, name: str, *, key: Callable[[Any], str]) -> None:
        self.name = name
        self._key = key
        self._items: dict[str, Any] = {}

    def register(self, item: Any) -> Any:
        key = self._key(item)
        existing = self._items.get(key)
        if existing and existing is not item:
            raise ValueError(
                f"{self.name} registry already has key {key!r} — two capabilities "
                "can't share a durable identity; namespace or rename one"
            )
        self._items[key] = item
        return item

    def clear(self) -> None:
        self._items.clear()

    def get(self, key: str) -> Any:
        return self._items.get(key)

    def all(self) -> list[Any]:
        return [self._items[key] for key in sorted(self._items)]

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


class ServiceRegistry(Registry):
    """The services that druks.toml leaves in the installation. Druks reads the
    file when a service is looked up, not when its class is defined, so importing
    a service needs no configuration."""

    def get(self, key: str) -> Any:
        service = super().get(key)
        return service if service and service.is_enabled() else None

    def all(self) -> list[Any]:
        return [service for service in super().all() if service.is_enabled()]

    def __contains__(self, key: str) -> bool:
        return bool(self.get(key))


def autodiscover(package: str) -> list[ModuleType]:
    """Import every role-named leaf module under ``package`` so the capability
    each one defines self-registers. Returns the imported modules so the loader
    can read routers off the ``routes`` ones; everything else works purely
    through the import side effect."""
    pkg = importlib.import_module(package)
    modules: list[ModuleType] = []
    for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{package}."):
        if info.ispkg or info.name.split(".")[-1] not in _ROLES:
            continue
        modules.append(importlib.import_module(info.name))
    return modules


# What the installation holds under a name of its own. Each registers when Python
# defines its class, and needs no app.
service_registry = ServiceRegistry("services", key=lambda cls: cls.slug)
webhook_registry = Registry("webhooks", key=lambda cls: f"{cls.__module__}.{cls.__qualname__}")
# The ways people reach a Bot. A channel's name is the source of its conversations.
channel_registry = Registry("channels", key=lambda cls: cls.name)

app_registry = Registry("apps", key=lambda app: app.name)


def iter_apps() -> "list[type[App]]":
    """Every app ``load_installed_apps`` registered when the process started."""
    if not app_registry:
        raise AppsNotRegisteredError
    return app_registry.all()


def get_app(name: str) -> "type[App]":
    for app in iter_apps():
        if app.name == name:
            return app
    raise KeyError(f"no installed app named {name!r}")


# Every App class that Python has defined, by its name. The loader registers the
# installed ones in app_registry.
_app_classes: "dict[str, type[App]]" = {}
# The App class that owns each Python package. A model, a workflow or a page finds
# its app from the module that defines it.
_app_packages: "dict[str, type[App]]" = {}


def register_app(app: "type[App]") -> None:
    """File the App class that Python just defined. Its name finds it, and its
    package belongs to it."""
    package = app.package
    if package:
        # Conflicting or overlapping claims are a packaging mistake — two installs
        # can't share a package.
        owner = _app_packages.get(package)
        if owner and owner.name != app.name:
            raise ValueError(
                f"package {package!r} already belongs to {owner.name!r} — "
                f"{app.name!r} can't claim it"
            )
        for registered, owner in _app_packages.items():
            nested = registered.startswith(f"{package}.") or package.startswith(f"{registered}.")
            if nested and owner.name != app.name:
                raise ValueError(
                    f"package {package!r} overlaps {registered!r} (owned by {owner.name!r}) — "
                    "package ownership must be unambiguous"
                )
        _app_packages[package] = app
    _app_classes[app.name] = app


def get_app_by_module(module: str) -> "type[App]":
    """The app that owns ``module``'s nearest claimed ancestor package. Raises
    ``LookupError`` when no app's package contains the module."""
    prefix = module
    while prefix:
        if prefix in _app_packages:
            return _app_packages[prefix]
        prefix = prefix.rpartition(".")[0]
    raise LookupError(module)


def get_app_name(module: str) -> str:
    """The name of the app that owns ``module``. Raises ``LookupError`` when no
    app's package contains the module."""
    return get_app_by_module(module).name


def _find(registry: str, name: str) -> Any:
    # A durable name starts with the name of the app that declares it.
    if app := _app_classes.get(name.rpartition(".")[0]):
        return getattr(app, registry).get(name)


def _list(registry: str) -> list[Any]:
    # One order for the whole installation, whatever order the apps loaded in.
    items: dict[str, Any] = {}
    for app in _app_classes.values():
        items.update(getattr(app, registry)._items)
    return [items[name] for name in sorted(items)]


def get_workflow(kind: str) -> "type[Workflow] | None":
    """The workflow that runs as ``kind``, or None when no loaded app declares it."""
    return _find("workflows", kind)


def iter_workflows() -> "list[type[Workflow]]":
    return _list("workflows")


def get_instruction(id: str) -> "Instruction | None":
    return _find("instructions", id)


def iter_instructions() -> "list[Instruction]":
    return _list("instructions")


def get_bot(id: str) -> "Bot | None":
    app = _app_classes.get(id.rpartition(".")[0])
    if app and app.bot and app.bot.id == id:
        return app.bot


def iter_bots() -> "list[Bot]":
    bots = {app.bot.id: app.bot for app in _app_classes.values() if app.bot}
    return [bots[id] for id in sorted(bots)]


def get_browser_session(name: str) -> "BrowserSession | None":
    return _find("browser_sessions", name)


def iter_browser_sessions() -> "list[BrowserSession]":
    return _list("browser_sessions")
