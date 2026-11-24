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
# A repo with CI registers a check within this long of a push. With none by then, the
# repo has no CI, and the build goes on as if CI were green.
CI_REGISTER_SECONDS = 3 * 60
# CI still running after this long goes to the review unfinished.
CI_TTL_SECONDS = 60 * 60
# A webhook wakes the CI wait early. The poll catches a check that finished before the
# wait began, and a delivery that never came.
CI_POLL_SECONDS = 60
# A webhook wakes the wait for a blocker's merge when GitHub moves the PR. The hourly read
# catches a move that came with no delivery.
BLOCKER_POLL_SECONDS = 60 * 60
# A blocker that does not merge in two weeks fails the build that waits for it.
BLOCKER_TTL_SECONDS = 14 * 24 * 60 * 60

# GitHub runs the merge of a stacked PR in the background, and a merge may take a few
# minutes. The run asks again this often until the merge is no longer pending.
MERGE_POLL_SECONDS = 10
# GitHub answers 409 to a stack call while it changes the stack, after a merge below. The
# build tries again this often.
STACK_RETRY_SECONDS = 10
# A suite that passed or did not apply.
CI_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# A Linear issue in a state of these types is finished or dropped, so it blocks nothing.
LINEAR_CLOSED_STATE_TYPES = frozenset({"completed", "canceled"})
