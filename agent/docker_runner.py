import os

import docker


def run_in_docker(repo_path: str, command: str, network: str = "none") -> dict:
    """
    Mount repo_path into a python:3.12-slim container and run command.
    Returns {stdout, stderr, exit_code}.
    """
    repo_abs = os.path.realpath(repo_path)
    client = docker.from_env()

    try:
        container = client.containers.run(
            image="python:3.12-slim",
            command=["bash", "-c", command],
            volumes={repo_abs: {"bind": "/workspace", "mode": "rw"}},
            working_dir="/workspace",
            network_mode=network,
            remove=False,
            stdout=True,
            stderr=True,
            detach=True,
        )
        exit_code = container.wait()["StatusCode"]
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        container.remove()
    except docker.errors.ContainerError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": 1}
    except docker.errors.ImageNotFound:
        return {"stdout": "", "stderr": "Docker image python:3.12-slim not found.", "exit_code": 1}

    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}


def run_tests(repo_path: str) -> dict:
    """
    Install dependencies (if requirements.txt exists) then run pytest.
    Uses bridge network for pip, returns combined result.
    """
    repo_abs = os.path.realpath(repo_path)
    has_reqs = os.path.exists(os.path.join(repo_abs, "requirements.txt"))

    if has_reqs:
        cmd = "pip install -r requirements.txt -q && pytest --tb=short -q"
        return run_in_docker(repo_path, cmd, network="bridge")
    else:
        return run_in_docker(repo_path, "pytest --tb=short -q", network="none")
