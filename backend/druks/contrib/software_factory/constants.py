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
# The steps of a build, in the order that its status shows them.
BUILD_STEPS = ("Plan", "Implement", "Draft pull request", "Review")
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
# A Linear issue in a state of these types is finished or dropped, so it blocks nothing.
LINEAR_CLOSED_STATE_TYPES = frozenset({"completed", "canceled"})
# The lessons page lists this many gate corrections, newest first, and counts those of the
# last month: the judgement point of the memory epic is 30 builds or a month.
CORRECTIONS_SHOWN = 50
CORRECTIONS_WINDOW_DAYS = 30
# Paulo, 19 Nov: judge the lessons after this many builds or a month, whichever comes first.
JUDGEMENT_BUILDS = 30
# Human corrections for each build between 4 and 26 Nov 2026, and the share that repeated an
# earlier correction (DRU-1247). The page sets its own numbers beside them.
CORRECTIONS_PER_BUILD_BASELINE = 0.28
CORRECTION_REPEAT_SHARE_BASELINE = 0.63
