# app/tools/container_toolset.py
"""Container tools used by the commander agent.

Two tools are exposed to the agent:

* ``container_image_finder_tool``: finds and downloads (pulls) a container image
  from an approved registry and starts a long-lived sandbox container out of it.
* ``run_container_command_tool``: runs a shell command *inside* that container.
  Commands are treated as potentially long running: they can be awaited with a
  deadline, launched in the background (``wait=False``) and polled later by
  passing back the ``task_id``.

Nothing is ever executed on the host machine: the only host side work is talking
to the Docker daemon API.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import time
import uuid
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from google.adk.tools import ToolContext

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        logging.warning("Invalid integer for %s, using default %s", name, default)
        return default


# --- Registry / sandbox policy -----------------------------------------------
APPROVED_REGISTRIES = {
    r.strip().lower()
    for r in _env_str(
        "CONTAINER_APPROVED_REGISTRIES",
        ",".join(
            [
                "docker.io",
                "registry.k8s.io",
                "gcr.io",
                "ghcr.io",
                "quay.io",
                "public.ecr.aws",
                "mcr.microsoft.com",
            ]
        ),
    ).split(",")
    if r.strip()
}
ENFORCE_APPROVED_REGISTRIES = (
    _env_str("CONTAINER_ENFORCE_APPROVED_REGISTRIES", "True").lower() == "true"
)
DEFAULT_IMAGE = _env_str("COMMANDER_DEFAULT_IMAGE", "python:3.11-slim")
DEFAULT_PLATFORM = _env_str("CONTAINER_PLATFORM", "linux/amd64")

# --- Timeouts / resource limits ----------------------------------------------
PULL_TIMEOUT_SECONDS = _env_int("CONTAINER_PULL_TIMEOUT", 900)
COMMAND_TIMEOUT_SECONDS = _env_int("CONTAINER_COMMAND_TIMEOUT", 600)
MAX_COMMAND_TIMEOUT_SECONDS = _env_int("CONTAINER_MAX_COMMAND_TIMEOUT", 3600)
POLL_INTERVAL_SECONDS = _env_int("CONTAINER_POLL_INTERVAL", 2)
PROGRESS_LOG_INTERVAL = _env_int("CONTAINER_PROGRESS_LOG_INTERVAL", 30)
MAX_OUTPUT_CHARS = _env_int("CONTAINER_MAX_OUTPUT_CHARS", 16000)
TASK_HISTORY_LIMIT = _env_int("CONTAINER_TASK_HISTORY_LIMIT", 20)
CONTAINER_WORKDIR = _env_str("CONTAINER_WORKDIR", "/workspace")
CONTAINER_MEMORY_LIMIT = _env_str("CONTAINER_MEMORY_LIMIT", "")
CONTAINER_NETWORK = _env_str("CONTAINER_NETWORK", "bridge")
TASKS_DIR = "/tmp/commander"
LABEL_PREFIX = "ai.commander"

# --- ToolContext state keys --------------------------------------------------
KEY_IMAGE = "container_image"
KEY_IMAGE_ID = "container_image_id"
KEY_CONTAINER_ID = "container_id"
KEY_CONTAINER_NAME = "container_name"
KEY_WORKDIR = "container_workdir"
KEY_SHELL = "container_shell"
KEY_PACKAGES = "container_package_manager"
KEY_TASKS = "container_tasks"

# Docker repository path: lowercase components separated by / (official regex).
_REPO_PATH = (
    r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*)*"
)
_REPO_PATH_RE = re.compile(rf"^{_REPO_PATH}$")

IMAGE_RE = re.compile(
    r"^(?:(?P<registry>[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)*(?::[0-9]+)?)/)?"
    r"(?P<repository>[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*)*)"
    r"(?::(?P<tag>[\w][\w.\-]{0,127}))?"
    r"(?:@(?P<digest>[A-Za-z][A-Za-z0-9+._-]*:[0-9a-fA-F]{32,}))?$"
)
_PLACEHOLDER_RE = re.compile(r"[\{\}<>]|TODO|FIXME|example\.com|your-", re.IGNORECASE)
_TOOLCHAIN_PROBE = (
    "for entry in sh:bash python3:python pip3:pip npm:node node:node "
    "git:git curl:curl wget:wget apt-get:apk dnf:yum microdnf apk zypper; do "
    'tool="${entry%%:*}"; probe="${entry#*:}"; '
    "if command -v \"$probe\" >/dev/null 2>&1; then printf '%s\\n' \"$tool\"; fi; "
    "done; printf '__CMDRC__%s\\n' \"${PIPESTATUS[0]:-$?}\""
)
_KEEPALIVE_COMMANDS = (
    ["sleep", "infinity"],
    ["sleep", "2147483647"],
    ["/bin/sh", "-c", "trap 'exit 0' TERM; while :; do sleep 3600; done"],
    ["/bin/sh", "-c", "while :; do sleep 3600; done"],
)
_PACKAGE_MANAGER_HINTS = {
    "apt-get": "apt-get update && apt-get install -y <packages>",
    "apk": "apk add --no-cache <packages>",
    "dnf": "dnf install -y <packages>",
    "microdnf": "microdnf install -y <packages>",
    "yum": "yum install -y <packages>",
    "zypper": "zypper install -y <packages>",
}


class UserCredentials:
    """
    A class to represent user credentials for accessing container images.

    Attributes:
        username (str): The username for authentication.
        password (str): The password for authentication.
    """

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password


class RegistryNotAllowedError(ValueError):
    """Raised when an image reference is not hosted on an approved registry."""


def _docker_client() -> Any:
    """Creates a Docker client for the local daemon (never runs host commands)."""
    return docker.from_env()


def _close_client(client: Any) -> None:
    """Best-effort close of a docker-py client to avoid leaking sockets."""
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must never fail the tool
            pass


def _get_state(tool_context: ToolContext | None, key: str, default: Any = None) -> Any:
    """Best-effort read of a ToolContext state value."""
    if tool_context is None:
        return os.getenv(f"COMMANDER_{key.upper()}", default)
    try:
        value = tool_context.state.get(key)
    except Exception:  # pragma: no cover - defensive for read-only contexts
        value = None
    return default if value is None else value


def _set_state(tool_context: ToolContext | None, key: str, value: Any) -> None:
    """Best-effort write of a ToolContext state value (ignored when read-only)."""
    if tool_context is None:
        return
    try:
        tool_context.state[key] = value
    except Exception:  # pragma: no cover - defensive for read-only contexts
        logging.debug("Could not persist tool state key %s", key)


def _session_scope(tool_context: ToolContext | None) -> str:
    """Stable short hash identifying the current session (fallback: process)."""
    session_id = None
    session = getattr(tool_context, "session", None) if tool_context else None
    if session is not None:
        session_id = getattr(session, "id", None)
    if not session_id:
        session_id = os.getenv("COMMANDER_SESSION_SCOPE", "local-process")
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:12]


def _container_name(image_name: str, scope: str) -> str:
    """Builds a deterministic, DNS friendly container name for the sandbox."""
    slug = re.sub(r"[^a-z0-9-]+", "-", image_name.split("/")[-1].lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:40] or "sandbox"
    return f"commander-{slug}-{scope}"[:63].strip("-")


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """Keeps command output LLM friendly by trimming the middle of long logs."""
    text = text or ""
    if len(text) <= limit:
        return text, False
    keep = max(limit // 2, 1)
    return (
        text[:keep] + "\n...[output truncated by commander]...\n" + text[-keep:],
        True,
    )



def _is_registry_host(component: str) -> bool:
    """True when the first path component of a reference is a registry host.

    Mirrors Docker's own rule: a host has a dot or a port, or is ``localhost``.
    Anything else (``acme/app``) is a Docker Hub namespace.
    """
    return "." in component or ":" in component or component == "localhost"


def normalize_image_reference(image_name: str) -> dict[str, Any]:
    """Splits an image reference into registry/repository/tag and applies policy.

    Args:
        image_name: Reference such as ``python:3.11-slim``, ``ghcr.io/acme/app:1.0``
            or ``nginx@sha256:<digest>``.

    Returns:
        Dict with ``registry``, ``repository``, ``tag``, ``digest`` and the fully
        qualified ``reference`` (the form the Docker Engine API expects).

    Raises:
        ValueError: The reference is empty, a placeholder or malformed.
        RegistryNotAllowedError: The registry is not in the approved list.
    """
    raw = (image_name or "").strip()
    if not raw:
        raise ValueError(
            "image_name is required. Pass an explicit image or set "
            f"COMMANDER_DEFAULT_IMAGE (currently {DEFAULT_IMAGE!r})."
        )
    if _PLACEHOLDER_RE.search(raw):
        raise ValueError(
            f"{raw!r} looks like a placeholder documentation value, not a real "
            "image reference. Re-read the documentation and provide a concrete name."
        )
    match = IMAGE_RE.match(raw)
    if not match:
        raise ValueError(
            f"{raw!r} is not a valid container image reference "
            "(expected '[registry/]repository[:tag][@digest]')."
        )

    registry = (match.group("registry") or "").lower()
    if registry in ("index.docker.io", "registry.docker.io", "dockerhub"):
        registry = "docker.io"
    repository = match.group("repository")
    if registry and not _is_registry_host(registry):
        # First path component without a dot/port (e.g. "acme/app") is a
        # Docker Hub namespace, not a registry host.
        repository = f"{registry}/{repository}"
        registry = ""
    registry = registry or "docker.io"
    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    if not _REPO_PATH_RE.match(repository):
        # e.g. an uppercase namespace captured by the registry group.
        raise ValueError(
            f"{raw!r} is not a valid container image reference: repository "
            f"{repository!r} must use lowercase path components "
            "(expected '[registry/]repository[:tag][@digest]')."
        )

    if ENFORCE_APPROVED_REGISTRIES and registry not in APPROVED_REGISTRIES:
        raise RegistryNotAllowedError(
            f"Registry {registry!r} is not approved. Approved registries: "
            f"{', '.join(sorted(APPROVED_REGISTRIES))}. Choose an image hosted on one "
            "of them, or ask the user to extend CONTAINER_APPROVED_REGISTRIES."
        )

    tag, digest = match.group("tag"), match.group("digest")
    reference = f"{registry}/{repository}"
    reference += f"@{digest}" if digest else f":{tag or 'latest'}"
    return {
        "registry": registry,
        "repository": repository,
        "tag": tag or ("latest" if not digest else None),
        "digest": digest,
        "reference": reference,
        "implicit_tag": tag is None and digest is None,
    }


def _auth_config(credentials: UserCredentials | dict | None) -> dict | None:
    """Normalizes tool credentials into the Docker ``auth_config`` payload."""
    if credentials is None:
        return None
    if isinstance(credentials, dict):
        username = credentials.get("username") or credentials.get("user")
        password = credentials.get("password") or credentials.get("pat")
    else:
        username = getattr(credentials, "username", None)
        password = getattr(credentials, "password", None)
    if not username and not password:
        return None
    return {"username": username or "", "password": password or ""}

def _image_payload(image: Any) -> dict[str, Any]:
    """Summarizes a local Docker image object for the LLM."""
    attrs = getattr(image, "attrs", None) or {}
    size = attrs.get("Size") or 0
    return {
        "id": (getattr(image, "id", "") or "").replace("sha256:", "")[:12],
        "repo_tags": attrs.get("RepoTags") or [],
        "size_mb": round(size / 1_000_000, 1),
        "architecture": attrs.get("Architecture"),
        "os": attrs.get("Os"),
        "created": attrs.get("Created"),
    }


def _local_images(client: Any, limit: int = 15) -> list[str]:
    """Lists locally available images (used to give the agent better hints)."""
    try:
        names: list[str] = []
        for image in client.images.list()[:limit]:
            names.extend(getattr(image, "tags", None) or [])
        return names
    except Exception:  # pragma: no cover - purely informational
        return []


def _pull_image(
    client: Any,
    parsed: dict[str, Any],
    credentials: UserCredentials | dict | None,
    timeout: int,
) -> dict[str, Any]:
    """Downloads the image from its registry, streaming progress to the logs.

    Blocking: callers run this in a worker thread with an asyncio deadline.
    """
    reference = parsed["reference"]
    digest = parsed.get("digest")
    tag_value = parsed.get("tag")
    # docker-py low-level pull takes the repository without tag plus a separate
    # tag; digest references must keep the @digest suffix with no tag param.
    pull_repository = f"{parsed['registry']}/{parsed['repository']}"
    pull_kwargs: dict[str, Any] = {
        "stream": True,
        "decode": True,
        "auth_config": _auth_config(credentials),
    }
    if DEFAULT_PLATFORM:
        pull_kwargs["platform"] = DEFAULT_PLATFORM
    if digest:
        pull_target = f"{pull_repository}@{digest}"
    else:
        pull_target = pull_repository
        pull_kwargs["tag"] = tag_value or "latest"
    started, last_log, last_line = time.monotonic(), 0.0, ""
    # Pull the fully qualified name so non-hub registries (ghcr.io/...) are
    # not resolved against docker.io.
    stream = client.api.pull(pull_target, **pull_kwargs)
    for event in stream:
        if time.monotonic() - started > timeout:
            raise TimeoutError(
                f"Pull of {reference} exceeded {timeout}s. Retry with a smaller "
                "image or raise CONTAINER_PULL_TIMEOUT."
            )
        if event.get("error"):
            detail = event.get("errorDetail") or {}
            raise APIError(
                str(event["error"]), explanation=str(detail.get("message", ""))
            )
        status = str(event.get("status") or event.get("id") or "").strip()
        progress = str(event.get("progress") or "").strip()
        line = f"{status} {progress}".strip()
        if not line or line == last_line:
            continue
        last_line = line
        # Docker emits hundreds of progress events; throttle routine updates
        # but always surface completion lines.
        done = "complete" in line.lower() or "downloaded" in line.lower()
        now = time.monotonic()
        if done or (now - last_log >= max(1, PROGRESS_LOG_INTERVAL)):
            last_log = now
            logging.info("Pull %s: %s", reference, line)
    return _image_payload(client.images.get(reference))

_MARKER_RE = re.compile(r"__CMDRC__(-?\d+)")
_KNOWN_TOOLS = (
    "sh",
    "bash",
    "python3",
    "pip3",
    "npm",
    "node",
    "git",
    "curl",
    "wget",
    "apt-get",
    "dnf",
    "microdnf",
    "yum",
    "apk",
    "zypper",
)
_PACKAGE_MANAGERS = ("apt-get", "apk", "dnf", "microdnf", "yum", "zypper")


def _exec_collect(
    client: Any,
    container_id: str,
    shell: str,
    argv: list[str],
    workdir: str | None,
    timeout: int,
) -> dict[str, Any]:
    """Runs a short-lived blocking exec inside the container and returns output."""
    exec_id = client.api.exec_create(
        container_id,
        [shell] + argv,
        stdout=True,
        stderr=True,
        workdir=workdir,
    )["Id"]
    output = client.api.exec_start(exec_id, demux=True)
    if isinstance(output, tuple):
        stdout_bytes, stderr_bytes = output
    else:
        stdout_bytes, stderr_bytes = output, None
    text = ((stdout_bytes or b"") + (stderr_bytes or b"")).decode(
        "utf-8", errors="replace"
    )
    info = client.api.exec_inspect(exec_id) or {}
    return {
        "output": text,
        "exit_code": info.get("ExitCode"),
        "running": bool(info.get("Running")),
        "exec_id": exec_id,
    }


def _detect_toolchain(client: Any, container: Any, shell: str) -> dict[str, Any]:
    """Probes the sandbox for interpreters and package managers available on PATH."""
    probe = _exec_collect(
        client,
        container.id,
        shell,
        ["-c", _TOOLCHAIN_PROBE],
        workdir=None,
        timeout=60,
    )
    markers = _MARKER_RE.findall(probe["output"])
    exit_code = int(markers[-1]) if markers else probe.get("exit_code")
    found = [
        line.strip()
        for line in _MARKER_RE.sub("", probe["output"] or "").splitlines()
        if line.strip()
    ]
    tools = [tool for tool in _KNOWN_TOOLS if tool in found]
    managers = [manager for manager in _PACKAGE_MANAGERS if manager in tools]
    return {
        "tools": tools,
        "package_managers": managers,
        "install_hint": (
            _PACKAGE_MANAGER_HINTS[managers[0]] if managers else None
        ),
        "probe_exit_code": exit_code,
    }


def _create_sandbox(
    client: Any, parsed: dict[str, Any], name: str, scope: str, workdir: str
) -> Any:
    """Creates and starts the long-lived sandbox container for this session."""
    last_error: Exception | None = None
    for command in _KEEPALIVE_COMMANDS:
        container = None
        try:
            container = client.containers.create(
                image=parsed["reference"],
                command=command,
                name=name,
                working_dir=workdir or None,
                platform=DEFAULT_PLATFORM or None,
                labels={
                    f"{LABEL_PREFIX}.session": scope,
                    f"{LABEL_PREFIX}.image": parsed["reference"][:120],
                },
                mem_limit=CONTAINER_MEMORY_LIMIT or None,
                network=CONTAINER_NETWORK or None,
                stdin_open=True,
                tty=True,
                auto_remove=False,
            )
            container.start()
            container.reload()
            if getattr(container, "status", None) != "running":
                raise APIError(
                    f"container exited immediately with status {container.status!r}"
                )
            return container
        except APIError as exc:
            last_error = exc
            logging.warning("Sandbox with command %s unusable: %s", command, exc)
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:  # pragma: no cover - best effort cleanup
                    pass
    raise APIError(
        f"Image {parsed['reference']} provides no usable keepalive process "
        f"(tried sleep and shell loops). Last error: {last_error}"
    )

def _q(text: str) -> str:
    """Shell-quotes a path/value so only our own wrapper syntax is executed."""
    try:
        return shlex.quote(text)
    except ValueError:
        return "'" + text.replace("'", "'\\''") + "'"


def _task_paths(task_id: str) -> dict[str, str]:
    """Log/exit-code file paths used to capture output of long commands."""
    return {
        "log_path": f"{TASKS_DIR}/{task_id}.log",
        "rc_path": f"{TASKS_DIR}/{task_id}.rc",
    }


def _build_task_command(command: str, paths: dict[str, str]) -> str:
    """Wraps the user command so its output lands in a file we can poll.

    The documentation command itself is quoted as data: only the wrapper's
    redirections are interpreted by the outer shell, and the command keeps
    running (writing to its log) even if the tool call is interrupted.
    """
    log_path, rc_path = paths["log_path"], paths["rc_path"]
    return (
        f"mkdir -p {_q(TASKS_DIR)} && {{ {_q(command)} ; }} "
        f"> {_q(log_path)} 2>&1; "
        f"echo $? > {_q(rc_path)}"
    )


def _start_task(
    client: Any,
    container_id: str,
    shell: str,
    command: str,
    workdir: str | None,
) -> dict[str, Any]:
    """Launches a command as a detached exec inside the container."""
    task_id = uuid.uuid4().hex[:12]
    paths = _task_paths(task_id)
    exec_id = client.api.exec_create(
        container_id,
        [shell, "-c", _build_task_command(command, paths)],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )["Id"]
    client.api.exec_start(exec_id, detach=True)
    task = {
        "task_id": task_id,
        "exec_id": exec_id,
        "container_id": container_id,
        "command": command,
        "workdir": workdir,
        "shell": shell,
        "offset": 0,
        "started_at": time.time(),
        "finished_at": None,
        "exit_code": None,
    }
    task.update(paths)
    return task


def _read_task_output(client: Any, task: dict[str, Any]) -> str:
    """Reads the not-yet-consumed part of the task log (incremental tail)."""
    log_path = _q(task["log_path"])
    probe = (
        f"if [ -f {log_path} ]; then "
        f"tail -c +{int(task['offset']) + 1} {log_path} 2>/dev/null; fi"
    )
    result = _exec_collect(
        client,
        task["container_id"],
        task.get("shell") or "/bin/sh",
        ["-c", probe],
        workdir=None,
        timeout=60,
    )
    text = result["output"]
    task["offset"] = int(task["offset"]) + len(text.encode("utf-8", errors="replace"))
    return text


def _poll_task_once(client: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Single non-blocking status pass: new output, running flag and exit code.

    The detached wrapper process always exits 0 (it only writes the real exit
    code of the user command to ``rc_path``), so once the exec finishes the rc
    file is the authoritative status source.
    """
    chunk = _read_task_output(client, task)
    running, exit_code, inspect_exit = True, None, None
    try:
        info = client.api.exec_inspect(task["exec_id"]) or {}
        running = bool(info.get("Running"))
        inspect_exit = info.get("ExitCode")
    except Exception:
        running = False  # exec handle is gone; fall back to the rc file.
    if not running:
        try:
            rc = _exec_collect(
                client,
                task["container_id"],
                task.get("shell") or "/bin/sh",
                ["-c", f"cat {_q(task['rc_path'])} 2>/dev/null || true"],
                workdir=None,
                timeout=60,
            )
            tokens = rc["output"].split()
            if tokens and tokens[-1].lstrip("-").isdigit():
                exit_code = int(tokens[-1])
        except Exception:  # pragma: no cover - container may be gone
            logging.debug("Could not read rc file for task %s", task.get("task_id"))
        if exit_code is None:
            exit_code = (
                task["exit_code"] if task.get("exit_code") is not None else inspect_exit
            )
        running = exit_code is None  # rc not written yet -> command still running
    if exit_code is not None:
        task["exit_code"] = exit_code
    if not running and not task.get("finished_at"):
        task["finished_at"] = time.time()
    return {
        "chunk": chunk,
        "running": running,
        "exit_code": task.get("exit_code"),
    }

def _container_image_id(container: Any) -> str | None:
    """Best-effort image id of a running sandbox container (short form)."""
    try:
        image = getattr(container, "image", None)
        raw = ""
        if isinstance(image, dict):
            raw = str(image.get("Id") or image.get("ID") or "")
        else:
            raw = str(getattr(image, "id", "") or "")
            if not raw:
                attrs = getattr(container, "attrs", None) or {}
                raw = str(attrs.get("Image") or "")
        raw = raw.replace("sha256:", "")
        return raw[:12] or None
    except Exception:  # pragma: no cover - informational only
        return None


def _resolve_container(client: Any, tool_context: ToolContext | None) -> Any:
    """Finds the session sandbox container, restarting it when it stopped."""
    container_id = _get_state(tool_context, KEY_CONTAINER_ID)
    container = None
    if container_id:
        try:
            container = client.containers.get(container_id)
        except Exception:
            container = None
    if container is None:
        scope = _session_scope(tool_context)
        try:
            known = client.containers.list(
                all=True, filters={"label": f"{LABEL_PREFIX}.session={scope}"}
            )
        except APIError:
            known = []
        container = known[0] if known else None
    if container is None:
        raise RuntimeError(
            "No sandbox container is attached to this session yet. Call "
            "container_image_finder_tool first to select and download an image."
        )
    container.reload()
    status = getattr(container, "status", None)
    if status != "running":
        try:
            container.start()
            container.reload()
        except APIError as exc:
            raise RuntimeError(
                f"Container {container.id[:12]} is '{status}' and could not be "
                f"restarted ({exc}). Call container_image_finder_tool again."
            ) from exc
        if getattr(container, "status", None) != "running":
            raise RuntimeError(
                f"Container {container.id[:12]} is still '{status}'. Call "
                "container_image_finder_tool to recreate the sandbox."
            )
    return container


def _get_task(
    tool_context: ToolContext | None, task_id: str
) -> dict[str, Any] | None:
    """Reads a previously started long-running task from the session state."""
    tasks = _get_state(tool_context, KEY_TASKS, {}) or {}
    task = tasks.get(task_id) if isinstance(tasks, dict) else None
    return task if isinstance(task, dict) else None


def _store_task(tool_context: ToolContext | None, task: dict[str, Any]) -> None:
    """Persists task metadata in the session state, bounding the history size."""
    tasks = _get_state(tool_context, KEY_TASKS, {}) or {}
    if not isinstance(tasks, dict):
        tasks = {}
    tasks[str(task["task_id"])] = task
    recent = sorted(tasks.values(), key=lambda t: t.get("started_at") or 0.0)
    kept = recent[-max(TASK_HISTORY_LIMIT, 1) :]
    _set_state(tool_context, KEY_TASKS, {str(t["task_id"]): t for t in kept})


def _sandbox_snapshot(tool_context: ToolContext | None) -> dict[str, Any]:
    """Compact sandbox description attached to successful tool responses."""
    return {
        "image": _get_state(tool_context, KEY_IMAGE),
        "container_id": str(_get_state(tool_context, KEY_CONTAINER_ID) or "")[:12],
        "container_name": _get_state(tool_context, KEY_CONTAINER_NAME),
        "workdir": _get_state(tool_context, KEY_WORKDIR),
        "shell": _get_state(tool_context, KEY_SHELL),
        "package_managers": _get_state(tool_context, KEY_PACKAGES, []),
    }


def _error(message: str, **extra: Any) -> dict[str, Any]:
    """Uniform failure payload: agents act on ``status`` + ``error_message``."""
    return {"status": "error", "error_message": message, **extra}

async def container_image_finder_tool(
    tool_context: ToolContext,
    image_name: str = "",
    credentials: UserCredentials | dict | None = None,
    force_pull: bool = False,
    start_container: bool = True,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Finds and downloads a container image, then prepares it as a sandbox.

    Call this FIRST, before run_container_command_tool. It resolves the image
    reference, rejects images outside the approved registries, downloads the image
    from its registry (or reuses the local copy), starts a long-lived container
    from it and reports which interpreters and package managers exist inside.

    Args:
        tool_context: The context for the tool; holds the sandbox state.
        image_name: Image to find and download, e.g. "python:3.11-slim",
            "node:20-bookworm" or "ghcr.io/acme/tool:1.4.2". Empty string uses
            the configured COMMANDER_DEFAULT_IMAGE.
        credentials: Optional registry login for private images, as
            {"username": "...", "password": "..."}.
        force_pull: Download the image again even if it exists locally.
        start_container: Also start the long-lived sandbox container (default
            True). Set False to only download the image.
        timeout: Optional download timeout in seconds.

    Returns:
        dict with ``status`` ("ready", "image_ready", "already_ready" or
        "error"), ``content`` (human readable summary for the conversation),
        ``image``, ``image_details``, ``downloaded`` and ``environment``
        (available tools, package manager, install hint, workdir, shell).
        On failure ``status`` is "error" and ``error_message`` says why.
    """
    requested = (image_name or "").strip() or DEFAULT_IMAGE
    pull_timeout = max(1, int(timeout if timeout is not None else PULL_TIMEOUT_SECONDS))
    try:
        parsed = normalize_image_reference(requested)
    except (ValueError, RegistryNotAllowedError) as exc:
        return _error(str(exc), requested_image=requested)

    reference = parsed["reference"]
    client: Any = None
    try:
        client = await asyncio.to_thread(_docker_client)

        container: Any = None
        try:
            container = await asyncio.to_thread(
                _resolve_container, client, tool_context
            )
        except (RuntimeError, APIError, NotFound, DockerException):
            container = None

        # Reuse the sandbox only when it already serves the requested image.
        # Fast path: same reference recorded in state and the running
        # container's image id still matches. The pull step below can still
        # invalidate reuse (force_pull / remote update -> new image id).
        stored_image = _get_state(tool_context, KEY_IMAGE)
        stored_image_id = _get_state(tool_context, KEY_IMAGE_ID)
        container_image_id = _container_image_id(container) if container else None
        reuse = (
            container is not None
            and stored_image == reference
            and (not stored_image_id or container_image_id in (None, stored_image_id))
        )

        downloaded = False
        image_details: dict[str, Any] | None = None
        if not force_pull:
            try:
                image_details = await asyncio.to_thread(
                    lambda: _image_payload(client.images.get(reference))
                )
            except (ImageNotFound, APIError):
                image_details = None
        if image_details is None:
            downloaded = True
            logging.info("Downloading container image %s", reference)
            image_details = await asyncio.wait_for(
                asyncio.to_thread(
                    _pull_image, client, parsed, credentials, pull_timeout
                ),
                timeout=pull_timeout + 30,
            )
        # A fresh pull may have updated the local image: if the running
        # container was started from an older image id, do not reuse it —
        # force recreation below so the sandbox matches what we report.
        if reuse and container is not None and image_details:
            fresh_id = str(image_details.get("id") or "") or None
            if fresh_id and container_image_id and container_image_id != fresh_id:
                logging.info(
                    "Sandbox image %s updated (%s -> %s); recreating container",
                    reference,
                    container_image_id,
                    fresh_id,
                )
                reuse = False

        if not start_container:
            return {
                "status": "image_ready",
                "content": (
                    f"Image {reference} is available locally "
                    f"({'downloaded now' if downloaded else 'already present'}). "
                    "Call this tool again with start_container=True to open it."
                ),
                "image": reference,
                "downloaded": downloaded,
                "image_details": image_details,
            }

        scope = _session_scope(tool_context)
        name = _container_name(parsed["repository"], scope)
        if reuse:
            # Reused sandbox: state already matches, but the recorded image
            # id may be stale (older client). Refresh it from the pull result
            # so future calls compare against the current local image.
            container_id = str(getattr(container, "id", "") or "")
            container_name = str(getattr(container, "name", "") or "") or name
            current_image_id = (
                image_details.get("id") if image_details else None
            )
            if current_image_id and _get_state(
                tool_context, KEY_IMAGE_ID
            ) != current_image_id:
                _set_state(tool_context, KEY_IMAGE_ID, current_image_id)
            return {
                "status": "already_ready",
                "content": (
                    f"Sandbox already ready: {reference} is running as container "
                    f"{container_id[:12] or container_name}. Shell: "
                    f"{_get_state(tool_context, KEY_SHELL) or '/bin/sh'}. Use "
                    "run_container_command_tool to run the documented commands."
                ),
                "image": reference,
                "downloaded": downloaded,
                "reused": True,
                "image_details": image_details,
                "environment": {
                    **_sandbox_snapshot(tool_context),
                },
            }
        container_id, container_name = "", name
        if not reuse:
            stale_list: list[Any] = []
            for stale_filter in (
                {"name": name},
                {"label": f"{LABEL_PREFIX}.session={scope}"},
            ):
                try:
                    stale_list.extend(
                        await asyncio.to_thread(
                            lambda f=stale_filter: client.containers.list(
                                all=True, filters=f
                            )
                        )
                    )
                except APIError:
                    continue
            if container is not None:
                stale_list = [*stale_list, container]
            seen: set[str] = set()
            for stale in stale_list:
                stale_id = getattr(stale, "id", None)
                if not stale_id or stale_id in seen:
                    continue
                seen.add(stale_id)
                try:
                    await asyncio.to_thread(stale.remove, force=True)
                except Exception:  # pragma: no cover - best effort cleanup
                    logging.warning("Could not remove stale sandbox %s", stale.name)
            container = await asyncio.to_thread(
                _create_sandbox, client, parsed, name, scope, CONTAINER_WORKDIR
            )
        if container is None:  # pragma: no cover - defensive; reuse implies set
            return _error(
                f"Could not prepare a sandbox container for {reference}. Check "
                "the Docker daemon and retry.",
                image=reference,
            )
        shell = "/bin/sh"
        for candidate in ("/bin/sh", "/bin/bash", "/bin/ash", "/bin/dash"):
            try:
                probe = await asyncio.to_thread(
                    _exec_collect,
                    client,
                    container.id,
                    candidate,
                    ["-c", "echo ok"],
                    None,
                    20,
                )
            except Exception:
                continue
            if "ok" in probe["output"]:
                shell = candidate
                break

        try:
            environment = await asyncio.to_thread(
                _detect_toolchain, client, container, shell
            )
        except Exception:  # pragma: no cover - probe is informational only
            environment = {
                "tools": [],
                "package_managers": [],
                "install_hint": None,
            }

        container_id = str(getattr(container, "id", "") or "")
        container_name = str(getattr(container, "name", "") or "") or name
        _set_state(tool_context, KEY_IMAGE, reference)
        _set_state(tool_context, KEY_IMAGE_ID, image_details.get("id"))
        _set_state(tool_context, KEY_CONTAINER_ID, container_id)
        _set_state(tool_context, KEY_CONTAINER_NAME, container_name)
        _set_state(tool_context, KEY_WORKDIR, CONTAINER_WORKDIR)
        _set_state(tool_context, KEY_SHELL, shell)
        _set_state(
            tool_context, KEY_PACKAGES, environment.get("package_managers", [])
        )

        managers = ", ".join(environment.get("package_managers") or []) or "none"
        if reuse:
            action = "already running from a previous call"
        else:
            action = "downloaded now" if downloaded else "already present locally"
        return {
            "status": "ready",
            "content": (
                f"Sandbox ready: {reference} ({action}) is running as container "
                f"{container_id[:12] or container_name}. Shell: {shell}. Package "
                f"managers: {managers}. Working directory: {CONTAINER_WORKDIR}. "
                "Use run_container_command_tool next to install dependencies and "
                "run the documented commands."
            ),
            "image": reference,
            "downloaded": downloaded,
            "reused": reuse,
            "image_details": image_details,
            "environment": {
                **_sandbox_snapshot(tool_context),
                "tools": environment.get("tools", []),
                "install_hint": environment.get("install_hint"),
            },
        }
    except (asyncio.TimeoutError, TimeoutError):
        return _error(
            f"Downloading {reference} did not finish within {pull_timeout + 30}s. "
            "Retry with a smaller image, or ask the user to raise "
            "CONTAINER_PULL_TIMEOUT.",
            image=reference,
        )
    except ImageNotFound as exc:
        available = _local_images(client) if client is not None else []
        return _error(
            f"Image {reference} was not found in its registry ({exc}). Double check "
            f"the image name and tag. Locally available images: {available or 'none'}",
            image=reference,
            local_images=available,
        )
    except RegistryNotAllowedError as exc:
        return _error(str(exc), image=reference)
    except (DockerException, APIError) as exc:
        return _error(
            f"Docker error while preparing {reference}: {exc!s}. Confirm the Docker "
            "daemon is running and reachable, then retry.",
            image=reference,
        )
    except Exception as exc:  # pragma: no cover - safety net for agent output
        logging.error("container_image_finder_tool failed", exc_info=True)
        return _error(f"{type(exc).__name__}: {exc}", image=reference)
    finally:
        _close_client(client)


async def run_container_command_tool(
    tool_context: ToolContext,
    command: str = "",
    task_id: str = "",
    wait: bool = True,
    timeout: int | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Runs a shell command inside the sandbox started by container_image_finder_tool.

    Commands are treated as potentially long running: with ``wait=True`` the
    call returns when the command finishes or the deadline expires; with
    ``wait=False`` the command is launched in the background and this call
    returns a ``task_id`` immediately. Poll the background command later by
    passing that ``task_id`` back to this tool.

    Args:
        tool_context: The context for the tool; holds the sandbox state.
        command: Shell command to run inside the container, e.g.
            "pip install -r requirements.txt" or "python -m pytest -q".
            Required unless ``task_id`` is passed to poll an earlier command.
        task_id: Id returned by a previous call with ``wait=False`` (or by a
            call whose deadline expired). When set, no new command is started.
        wait: Block until the command finishes or ``timeout`` seconds elapse
            (default True). Set False to background it and get a ``task_id``.
        timeout: Seconds to wait before returning control with the command
            still running. Clamped to [1, CONTAINER_MAX_COMMAND_TIMEOUT].
            Defaults to CONTAINER_COMMAND_TIMEOUT.
        workdir: Optional working directory inside the container. Defaults to
            the sandbox workdir recorded by container_image_finder_tool.

    Returns:
        dict with ``status``:
        * "success": command finished with exit code 0; includes ``output``
          (possibly truncated) and ``exit_code``.
        * "running": command is still running (backgrounded or past its
          deadline); includes ``task_id`` to poll later and any ``output``
          collected so far.
        * "error": ``error_message`` says why (bad input, no sandbox, Docker
          failure or non-zero exit); failed commands also include
          ``exit_code`` and ``output``.
    """
    command = (command or "").strip()
    task_id = (task_id or "").strip()
    if task_id and command:
        return _error(
            "Pass either command (to start a new command) or task_id (to poll "
            "an existing one), not both."
        )
    if not task_id and not command:
        return _error(
            "command is required, e.g. 'python --version'. To poll an "
            "earlier background command, pass its task_id instead."
        )
    deadline_seconds = min(
        max(1, int(timeout or COMMAND_TIMEOUT_SECONDS)),
        max(1, MAX_COMMAND_TIMEOUT_SECONDS),
    )

    client: Any = None
    try:
        client = await asyncio.to_thread(_docker_client)

        if task_id:
            task = _get_task(tool_context, task_id)
            if task is None:
                return _error(
                    f"No background task {task_id!r} is known for this session. "
                    "Start a new command instead, or check the task_id."
                )
        else:
            try:
                container = await asyncio.to_thread(
                    _resolve_container, client, tool_context
                )
            except RuntimeError as exc:
                return _error(str(exc))
            shell = str(_get_state(tool_context, KEY_SHELL) or "/bin/sh")
            target_workdir = str(
                workdir or _get_state(tool_context, KEY_WORKDIR) or CONTAINER_WORKDIR
            )
            task = await asyncio.to_thread(
                _start_task, client, container.id, shell, command, target_workdir
            )
            _store_task(tool_context, task)

        chunks: list[str] = []
        deadline = time.monotonic() + deadline_seconds
        while True:
            poll = await asyncio.to_thread(_poll_task_once, client, task)
            if poll["chunk"]:
                chunks.append(poll["chunk"])
            _store_task(tool_context, task)
            if not poll["running"]:
                break
            if not wait or time.monotonic() >= deadline:
                output, _ = _truncate("".join(chunks))
                elapsed = int(time.time() - (task.get("started_at") or time.time()))
                return {
                    "status": "running",
                    "content": (
                        f"Task {task['task_id']} is still running after {elapsed}s "
                        f"and keeps running in the background: {task['command']}. "
                        "Call run_container_command_tool again with this task_id "
                        "to collect more output."
                    ),
                    "task_id": task["task_id"],
                    "command": task["command"],
                    "output": output,
                    "elapsed_seconds": elapsed,
                }
            await asyncio.sleep(max(1, POLL_INTERVAL_SECONDS))

        output, truncated = _truncate("".join(chunks))
        exit_code = task.get("exit_code")
        summary = {
            "task_id": task["task_id"],
            "command": task["command"],
            "exit_code": exit_code,
            "output": output,
            "output_truncated": truncated,
        }
        if exit_code == 0:
            return {
                "status": "success",
                "content": (
                    f"Command finished successfully: {task['command']}\n{output}"
                ),
                **summary,
            }
        return _error(
            f"Command exited with status {exit_code}: {task['command']}",
            **summary,
        )
    except (DockerException, APIError) as exc:
        return _error(
            f"Docker error while running the command: {exc!s}. Confirm the "
            "Docker daemon is running and the sandbox from "
            "container_image_finder_tool still exists, then retry.",
            task_id=task_id or None,
        )
    except Exception as exc:  # pragma: no cover - safety net for agent output
        logging.error("run_container_command_tool failed", exc_info=True)
        return _error(f"{type(exc).__name__}: {exc}", task_id=task_id or None)

