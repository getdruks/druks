# The github MCP server build ships into its own runs — build's requirement
# (there is no build without github), not an operator-facing catalog entry.
# Its token is per-repo, minted at workspace setup from the identity reviews
# act as (druks.contrib.software_factory.github).
GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
TICKET_TOOLS = (
    "software_factory_get_ticket",
    "software_factory_add_comment",
    "software_factory_set_status",
    "software_factory_update_ticket",
    "software_factory_create_ticket",
)
# The tool that runs one of the repo's declared test commands on another box. A build's box
# always holds it: an account's key for Druks' server is revoked and minted again whenever
# the tools differ, so a tool list that followed the repo would revoke the key of every other
# running build of the account.
CHECK_TOOLS = ("core_run_check",)
# A repo with CI registers a check within this long of a push. With none by then, the
# repo has no CI, and the build goes on as if CI were green.
CI_REGISTER_SECONDS = 3 * 60
# CI still running after this long goes to the review unfinished.
CI_TTL_SECONDS = 60 * 60
# A webhook wakes the CI wait early. The poll catches a check that finished before the
# wait began, and a delivery that never came.
CI_POLL_SECONDS = 60
# A suite that passed or did not apply.
CI_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# Actions run conclusions a rerun can clear. A flake lands here, so the build reruns
# once before it sends the red to an implement call.
RERUN_CONCLUSIONS = frozenset({"failure", "timed_out"})
# An Actions run that has not finished, as a rerun is until it ends.
RUN_ACTIVE_STATUSES = frozenset({"queued", "in_progress"})
# A rerun re-queues its run within seconds. Poll this often for the new attempt before
# the build reads CI again, so it does not read the old red.
RERUN_POLL_SECONDS = 5
# Stop waiting for the rerun to start after this many polls, and read CI anyway.
RERUN_START_POLLS = 24
# The effort an implement call runs at for a change of this size. A size left out, a large one,
# runs at the operator's setting. The setting is also the ceiling: a call never runs above it.
IMPLEMENT_EFFORT_BY_SIZE = {"small": "high", "medium": "high"}
# A Linear issue in a state of these types is finished or dropped, so it blocks nothing.
LINEAR_CLOSED_STATE_TYPES = frozenset({"completed", "canceled"})
