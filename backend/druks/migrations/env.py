import logging
from logging.config import fileConfig

import druks.accounts.models  # noqa: F401
import druks.agents.models  # noqa: F401
import druks.browser.models  # noqa: F401
import druks.chat.models  # noqa: F401
import druks.contrib.software_factory.models  # noqa: F401
import druks.core.lessons.models  # noqa: F401
import druks.durable.models  # noqa: F401
import druks.files.models  # noqa: F401
import druks.harnesses.models  # noqa: F401
import druks.mcp.models  # noqa: F401
import druks.notifications.models  # noqa: F401
import druks.schedules.models  # noqa: F401
import druks.services.models  # noqa: F401
import druks.skills.models  # noqa: F401
import druks.teams.models  # noqa: F401
import druks.user_settings.models  # noqa: F401
from alembic import context
from druks.alembic_support import run_alembic_env

# A process that set up its log before it migrates, as druks init-db does, keeps
# that log, so with log_format = "json" the migration's lines are JSON too. A plain
# alembic command takes the logging of alembic.ini, which must not mute the loggers
# that already exist.
if context.config.config_file_name and not logging.getLogger().handlers:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

run_alembic_env()
