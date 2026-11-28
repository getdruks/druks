import subprocess

from druks.workspaces import RepoWorkspace

from .app import SoftwareFactory
from .models import ProjectRepo


@SoftwareFactory.implement.exec
async def run_in_environment(
    *, command: str, workspace: RepoWorkspace
) -> subprocess.CompletedProcess[str]:
    project_repo = await ProjectRepo.get_for_repo(workspace.subject.repo, raise_on_missing=True)
    template = project_repo.effective_profile.get("environment_template")
    if template:
        return await workspace.run_on_copy(command, template=template)
    return subprocess.CompletedProcess(
        command, 69, "This repository declares no environment. Run the command here.\n"
    )
