from druks.exceptions import DruksError


class NotificationError(DruksError):
    pass


class UnknownTokenError(NotificationError):
    # The token is the respond capability handle — never echo it.
    def __init__(self):
        super().__init__("Unknown respond token.")


class InvalidChoiceError(NotificationError):
    pass


class AnswerNotAllowedError(NotificationError):
    def __init__(self, reason: str, *, link: str | None = None) -> None:
        # A person whom Druks cannot name here, such as a Slack account that is not
        # linked, can still answer on the run's page, where Druks knows who signed in.
        super().__init__(f"{reason} Open it in Druks: {link}" if link else reason)


class AlreadyAcknowledgedError(NotificationError):
    def __init__(self):
        super().__init__("This notification was already acknowledged.")


class StaleRoundError(NotificationError):
    def __init__(self):
        super().__init__("The run has moved past the round this notification was sent for.")


class DeliveryError(NotificationError):
    # The message lands in logs and stored failure reasons, so it carries only a
    # short reason.
    def __init__(self, reason: str):
        super().__init__(f"Delivery failed: {reason}.")


class ChannelUnavailableError(DeliveryError):
    """The channel takes no post until a person fixes it: the bot is not in the channel,
    or the channel is archived or gone."""


class BlocksRefusedError(DeliveryError):
    """Slack refuses the blocks of the message. The same message can still go as text."""


class ActionRefusedError(NotificationError):
    """An action of a notification did not run. The message says why, for the person who
    clicked its button."""


class MalformedButtonError(NotificationError):
    # A button action_id embeds the respond token, so the message never echoes
    # the input.
    def __init__(self):
        super().__init__("Malformed button action_id.")
