from druks.mcp.constants import DRUKS_SERVER_NAME

# A person's schedules fire through DBOS schedules with this name prefix, beside the
# schedules that workflows and tasks declare.
SCHEDULE_NAME_PREFIX = "schedule:"
RUN_SCHEDULE_WORKFLOW = "druks.run_schedule"
# The name of the key a schedule calls its tool with.
SCHEDULE_KEY_NAME = f"{DRUKS_SERVER_NAME}.schedules"
