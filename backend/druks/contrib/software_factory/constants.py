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
# A review takes minutes. A build still waiting after this long was never answered.
REVIEW_TTL_SECONDS = 2 * 60 * 60
# A repo with CI registers a check within this long of a push. With none by then, the
# repo has no CI, and the build goes on as if CI were green.
CI_REGISTER_SECONDS = 3 * 60
# CI still running after this long goes to the review unfinished.
CI_TTL_SECONDS = 60 * 60
# A webhook wakes the CI wait early. The poll catches a check that finished before the
# wait began, and a delivery that never came.
CI_POLL_SECONDS = 60
# The lockfiles at a repo's root that the sandbox template is keyed on, so that a changed
# dependency builds a new template.
LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Gemfile.lock",
    "go.sum",
    "Cargo.lock",
    "composer.lock",
)

# A webhook wakes the wait for a blocker's merge early. The poll catches a move that came
# with no delivery.
BLOCKER_POLL_SECONDS = 5 * 60
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
# The implementer ends its turn to have Druks run a declared test command, and Druks resumes it
# with the result. A round that asks for more runs than this has stopped converging.
MAX_CHECK_RUNS = 10
# The effort an implement call runs at for a change of this size. A size left out, a large one,
# runs at the operator's setting. The setting is also the ceiling: a call never runs above it.
IMPLEMENT_EFFORT_BY_SIZE = {"small": "high", "medium": "high"}
# A test command that runs longer than this is killed, and the implementer is told so.
CHECK_TIMEOUT_SECONDS = 30 * 60
# The result keeps this many trailing lines of the command's output. The agent reads the
# rest from the log on its box.
CHECK_TAIL_LINES = 200
# Each of those lines keeps this many characters.
CHECK_LINE_CHARS = 1000
CHECK_LOG_PATH = "/tmp/druks-check.log"
# A Linear issue in a state of these types is finished or dropped, so it blocks nothing.
LINEAR_CLOSED_STATE_TYPES = frozenset({"completed", "canceled"})
