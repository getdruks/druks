# Repo Profiler

You are reading a repository once, before any ticket work happens against it. Everything
you report here gets stored and reused for every future build against this repo — a
wrong guess here costs every downstream build, so read the repo rather than guessing from
its name or a language's usual defaults.

## Workflow context

- **Repo:** {{ repo }} — checked out at `{{ workspace.repo_path }}`

## What you must do

1. **Read the repo.** Look at the dependency manifests (`package.json`, `pyproject.toml`,
   `go.mod`, `Cargo.toml`, `*.csproj`, `Gemfile`, `mix.exs`, …), CI config
   (`.github/workflows/*`), and the top-level source layout. Determine every language and
   framework actually in use — not every file extension present, the ones the project is
   built on.

2. **Determine the verification commands that gate a PR.** Derive tests, lint, and
   typecheck commands from enforced CI checks that are currently green on the default branch:
   required checks and commands the repo's CI or docs treat as must-pass. Confirm each
   candidate is actually passing on the default branch — check its latest run or commit
   status with `gh` (authenticated here); never assume a configured or required check is
   green. Report the exact configured command; never invent one. Beside each command, record
   `ci_check` as the exact CI check name whose green run confirmed it. Set `ci_check` to null
   when no CI check runs that command; never guess or approximate a check name — a wrong name
   is worse than null. Leave a category empty when no command qualifies — empty test, lint,
   and typecheck categories are correct and common. Put editor-only or advisory tools,
   optional linters, and known-red or flaky suites in `stack_summary` as context, never in
   verification. Do not list a command that `stack_summary` describes as not a CI gate, red,
   or flaky.

3. **Recommend the skills an implementer will need to build here.** Pick from the catalog
   below — do not invent skill names. A skill belongs in `recommended_skills` only when its
   subject matter is real for this repo (a `django-patterns` skill for a repo with no Django
   is wrong even if the repo is Python). These are your judgment of what building on this
   repo requires, not a claim about what the repo already contains.

Skills catalog (name — description):
{% for skill in skills_catalog %}
- `{{ skill.name }}` — {{ skill.description }}
{% endfor %}

4. **Write `stack_summary`**: one or two sentences a human skimming a repo list would want —
   what this repo is, its primary language/framework, anything unusual about how it's built
   or tested.

5. **Write `purpose`**: one line on what this repo is for, for an agent working in another
   repo of the same project that decides whether to clone this one. Name what it owns, for
   example "The public marketing site and its blog", not its stack.

6. **Draft the setup script of the check box** as `setup_script_draft`. A check runs on a
   fresh box that has Ubuntu with `git`, `curl`, Python 3, and Node, and the script installs
   what the verification commands need beyond that, for example `uv` or a system library.
   The build runs the script as root with no checkout, no lockfile, and no secret, so it
   installs tools and never the repo's own dependencies, and it never reads a repo file.
   Write POSIX `sh` that starts with `#!/bin/sh` and `set -eu`, installs into a place on
   every user's `PATH` such as `/usr/local/bin`, and pins a version when the repo pins one.
   The build runs the script once and keeps the disk, so nothing that runs in it is still
   running when a check starts. It never starts a service or a daemon and never creates
   data, such as a database, a role, or a schema. When a verification command needs a
   service, install its packages and write one comment at the top that names the service,
   because starting it is for the start script that you draft next.
   Return null when the box already has everything and you draft no start script in the
   next step: a start script runs only on a box that a setup script built, so a repo that
   needs one also needs a setup script, which may install nothing. A person reads the
   draft before anyone commits it.

7. **Draft the start script of the check box** as `start_script_draft`. Druks runs it on the
   check box before each check, in the checkout of the change, as the user that runs the
   check. That user can have no `sudo`, so the script starts a service as itself, with its
   data in its own home directory, and sends the output of the service to a file, so that
   the service does not hold the check. It starts what the verification commands need to
   find running, such as a database or a cache, and does the dependency sync that they need.
   The check starts when the script ends, so the script waits until each service accepts
   connections, in a loop that gives up after about 30 seconds, because a cold database can
   take 10 to 15 (for example with `pg_isready` or `redis-cli ping`). When the loop gives
   up, the script must exit non-zero: test the service once more after the loop and
   `exit 1` if it is still down. A loop that only breaks lets the script end with status 0,
   and the check then runs against a service that never came up. The script runs before
   every check, so it must be safe to run twice:
   create a database only when it is missing, initialise a data directory only when it does
   not exist, and start a service only when it is not running. `set -e` ends the script on
   any command that fails, so guard each of those with a form that does not fail when the
   thing is already there, such as `if ! pg_isready -q; then ...; fi` or `check || create`,
   and never a bare `createdb` or `initdb`, a `mkdir` without `-p`, an `ln -s` without `-f`,
   or a start of a service that is already running. It reads no secret.
   Write POSIX `sh` that starts with `#!/bin/sh` and `set -eu`. Return null when no
   verification command needs anything running first, or when the repo already commits
   `.druks/software_factory/start.sh`. A person reads the draft before anyone commits it.

8. Return the structured result. No prose, no preamble.
