from druks.api.exceptions import AgentApiError


class RunNotFound(AgentApiError):
    status_code = 404
    code = "RUN_NOT_FOUND"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No run {run_id} of yours. Pass the id of the run you work in.")


class RunNotRunning(AgentApiError):
    status_code = 409
    code = "RUN_NOT_RUNNING"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} has no agent running, so there is no work to check.")


class RunHasNoRepo(AgentApiError):
    status_code = 409
    code = "RUN_HAS_NO_REPO"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} has no subject, so there is no repository to sync.")


class BoxGone(AgentApiError):
    status_code = 409
    code = "BOX_GONE"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"The box of run {run_id} no longer exists.")


class CheckNotDeclared(AgentApiError):
    status_code = 422
    code = "CHECK_NOT_DECLARED"
    retryable = True

    def __init__(self, command: str, declared: tuple[str, ...]) -> None:
        if not declared:
            super().__init__("This run declares no checks. Run the command in your own box.")
        else:
            listed = "; ".join(repr(check) for check in declared)
            super().__init__(f"{command!r} is not a declared check. Declared: {listed}.")


class CheckAlreadyStarted(AgentApiError):
    # One check waits for the end of the agent's turn. A second one would have no turn to
    # wait for: the first answer resumes the session.
    status_code = 409
    code = "CHECK_ALREADY_STARTED"
    retryable = False

    def __init__(self, command: str) -> None:
        super().__init__(
            f"A check is already waiting for your turn to end ({command!r}). End your turn."
        )


class CheckBoxRefused(AgentApiError):
    # Drukbox answered and refused: a clone that failed, a patch that does not apply, a host
    # it could not reach. The same call may fail the same way, so the message decides.
    status_code = 502
    code = "CHECK_BOX_REFUSED"
    retryable = False

    def __init__(self, reason: str) -> None:
        super().__init__(f"The box for the check could not run it: {reason}")


class CheckRejected(AgentApiError):
    # The check box refused what the agent asked, for example a timeout that no box of the
    # provider lives long enough for. The same call fails again: the agent changes it.
    status_code = 422
    code = "CHECK_REJECTED"

    def __init__(self, reason: str) -> None:
        super().__init__(f"The box for the check refused the request: {reason}")


class CheckBoxUnavailable(AgentApiError):
    # Drukbox did not answer, or is at its host limit. The same call works later.
    status_code = 503
    code = "CHECK_BOX_UNAVAILABLE"
    retryable = True

    def __init__(self, reason: str) -> None:
        super().__init__(f"The box for the check is not available now: {reason}")
