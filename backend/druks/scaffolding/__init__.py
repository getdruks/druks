from importlib import metadata
from pathlib import Path

from druks.apps.base import NAME_RE

_TEMPLATE = Path(__file__).parent / "app_template"
# Django's ``startapp`` uses the same method. Template files carry a suffix, so that nothing in
# the template tree is importable or lintable as real code before Druks renders it.
_TEMPLATE_SUFFIX = "-tpl"
# The template's package directory. The copy renames it to ``druks_<name>``.
_PACKAGE_DIR = "package"


def create_app(name: str, parent: Path) -> Path:
    """Copy the app template to ``parent/druks-<slug>`` and render its placeholders.
    The result is a standalone package whose entry point registers itself with the
    platform on install. ``name`` can use hyphens. The app name is its snake_case form.
    Raises ``ValueError`` on a bad name, a collision with an installed app, or a
    target directory that exists."""
    app_name = name.replace("-", "_")
    if not NAME_RE.match(app_name):
        raise ValueError(
            f"app name {name!r} must match {NAME_RE.pattern!r}. It keys the /api/<name> "
            "namespace, the version table, and the settings keys. Rename the app to match "
            "the pattern."
        )
    # Read the names only, with no entry.load(). A list of the entry points imports
    # nothing. A name that collides with an installed app makes the boot fail.
    installed = {entry.name for entry in metadata.entry_points(group="druks.apps")}
    if app_name in installed:
        raise ValueError(f"app {app_name!r} is already installed. Choose another name.")
    # The folder and the distribution use hyphens, like the other druks apps.
    # The app name and the package keep underscores. The name keys /api/<name>,
    # the table prefix, and the settings namespace.
    slug = app_name.replace("_", "-")
    target = parent / f"druks-{slug}"
    if target.exists():
        raise ValueError(f"{target} already exists. Remove it, or choose another directory.")

    values = {
        "{{ name }}": app_name,
        "{{ slug }}": slug,
        "{{ Name }}": "".join(part.capitalize() for part in app_name.split("_")),
    }
    for source in sorted(path for path in _TEMPLATE.rglob("*") if path.is_file()):
        parts = [
            f"druks_{app_name}" if part == _PACKAGE_DIR else part
            for part in source.relative_to(_TEMPLATE).parts
        ]
        parts[-1] = parts[-1].removesuffix(_TEMPLATE_SUFFIX)
        content = source.read_text()
        for token, value in values.items():
            content = content.replace(token, value)
        destination = target.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    return target
