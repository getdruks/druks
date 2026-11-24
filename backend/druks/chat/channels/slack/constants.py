# An unlinked person's link, and the message it holds, live this long.
LINK_TTL_SECONDS = 600
LINK_KEY = "chat:slack:link:{token}"
# A person who stays unlinked hears the welcome again after this long.
WELCOME_TTL_SECONDS = 30 * 24 * 60 * 60
WELCOME_KEY = "chat:slack:welcome:{user}"
# A person's untagged replies in a thread reach their agent while the last message
# of their conversation there is younger than this.
JOINED_THREAD_SECONDS = 24 * 60 * 60
# How much of a thread the agent reads.
THREAD_MESSAGES = 200
LINK_MESSAGE = (
    "I answer your message after you connect your Slack account to Druks. Connect it here: {url}"
)
UNLINKED_REPLY = (
    "<@{user}>, your Slack account is not connected to Druks yet. "
    "I sent you a direct message with the link to connect it."
)
UNACCEPTED_AUTOMATION_REPLY = (
    "I do not answer this automation yet. A Druks admin can accept it here: {url}"
)
AGENT_NOTE = (
    "<@{user}>, {agent} is answering. Your replies in this thread go to {agent} for "
    "{hours} hours after the last message, until you tag another agent. "
    "After that, tag me again."
)
TAG_REQUIRED_AGENT_NOTE = "<@{user}>, {agent} is answering. Tag me in each reply."
WELCOME_TEMPLATE = "chat/channels/slack/welcome.md"
# Slack's limit for the text of all the markdown blocks of one message.
MARKDOWN_TEXT_LIMIT = 12_000
# What ends a message that Druks cut to fit, in a paragraph of its own.
TRUNCATION_MARKER = "\n\n…"
# Slack's limits for a message of inputs, from the Block Kit reference.
MESSAGE_BLOCKS_LIMIT = 50
BLOCK_ID_LIMIT = 255
LABEL_LIMIT = 2000
HINT_LIMIT = 2000
PLACEHOLDER_LIMIT = 150
INITIAL_VALUE_LIMIT = 3000
OPTION_TEXT_LIMIT = 75
OPTION_VALUE_LIMIT = 150
OPTIONS_LIMITS = {
    "plain_text_input": 0,
    "static_select": 100,
    "multi_static_select": 100,
    "radio_buttons": 10,
    "checkboxes": 10,
}
# Slack's limits for the blocks of a notification, from the Block Kit reference.
SECTION_TEXT_LIMIT = 3000
SECTION_FIELDS_LIMIT = 10
FIELD_TEXT_LIMIT = 2000
BUTTON_TEXT_LIMIT = 75
BUTTON_URL_LIMIT = 3000
# Slack's limits for a data table, from the Block Kit reference.
TABLE_ROWS_LIMIT = 200
TABLE_COLUMNS_LIMIT = 20
MESSAGE_CELL_TEXT_LIMIT = 20_000
# Slack takes no empty cell or label. An empty one in a data table or a chart shows this.
EMPTY_CELL_TEXT = "—"
# Slack's limits for a chart, from the Block Kit reference.
MESSAGE_CHARTS_LIMIT = 2
CHART_SERIES_LIMIT = 12
CHART_POINTS_LIMIT = 20
CHART_SEGMENTS_LIMIT = 12
CHART_LABEL_LIMIT = 20
CHART_TITLE_LIMIT = 50
CHART_AXIS_TITLE_LIMIT = 50
# Slack's limits for the dialog that confirms a click. Its title is the label of the button.
CONFIRM_TEXT_LIMIT = 300
CONFIRM_BUTTON_LIMIT = 30
# What the button that closes the dialog says, as on the dashboard.
CONFIRM_DENY_TEXT = "Back"
# The image formats that Slack draws, and its limits for an image, from the Block Kit reference.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif")
IMAGE_URL_LIMIT = 3000
IMAGE_ALT_TEXT_LIMIT = 2000
IMAGE_TITLE_LIMIT = 2000
# What a button that opens a link sends as its action. A click on it answers no notification.
LINK_BUTTON_ACTION_ID = "link"
