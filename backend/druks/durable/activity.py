from datetime import datetime

from dbos import DBOS, WorkflowSerializationFormat


async def set_run_phase(phase: str, *, ends_at: datetime | None = None) -> None:
    # The live infra/agent phase, a DBOS event that each phase overwrites. Portable
    # JSON, not DBOS's default pickle, lets SQL read it with the run (Run.phase). A
    # chat or browser box provisions outside a run: nothing shows its phase.
    if DBOS.workflow_id:
        await DBOS.set_event_async(
            "phase", phase, serialization_type=WorkflowSerializationFormat.PORTABLE
        )
        if ends_at:
            await DBOS.set_event_async(
                "phase_ends_at", ends_at, serialization_type=WorkflowSerializationFormat.PORTABLE
            )


async def get_run_phase(run_id: str) -> str | None:
    return await DBOS.get_event_async(run_id, "phase", timeout_seconds=0)
