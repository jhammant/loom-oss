"""Thin wrappers over the `docker` CLI.

Shelling out to the docker CLI (rather than a Docker SDK) keeps the CLI free of
native dependencies and avoids SDK/daemon version drift.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .util import LoomError

MANAGED_LABEL = "loom.managed=true"


def _docker_bin() -> str:
    b = shutil.which("docker")
    if not b:
        raise LoomError("`docker` not found on PATH. Install Docker / OrbStack first.")
    return b


def run(args: list[str], *, capture: bool = False, check: bool = True,
        input_text: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [_docker_bin(), *args]
    try:
        return subprocess.run(
            cmd,
            check=check,
            text=True,
            input=input_text,
            capture_output=capture,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() or (e.stdout or "").strip()
        raise LoomError(f"docker {' '.join(args[:2])} failed: {detail}")


def daemon_ok() -> bool:
    r = run(["info"], capture=True, check=False)
    return r.returncode == 0


def network_ensure(name: str) -> None:
    r = run(["network", "inspect", name], capture=True, check=False)
    if r.returncode != 0:
        run(["network", "create", name], capture=True)


def container_exists(name: str) -> bool:
    r = run(["container", "inspect", name], capture=True, check=False)
    return r.returncode == 0


def container_state(name: str) -> str | None:
    """'running' | 'exited' | ... or None if the container does not exist."""
    r = run(["container", "inspect", "-f", "{{.State.Status}}", name], capture=True, check=False)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def rm(name: str, *, force: bool = True) -> None:
    if container_exists(name):
        args = ["rm", "-f", name] if force else ["rm", name]
        run(args, capture=True)


def rmi(image: str) -> None:
    # Best-effort: an image shared by another container shouldn't fail removal.
    run(["rmi", image], capture=True, check=False)


def stop(name: str) -> None:
    run(["stop", name], capture=True)


def start(name: str) -> None:
    run(["start", name], capture=True)


def logs(name: str, *, follow: bool = False, tail: str = "200") -> int:
    args = ["logs", "--tail", str(tail)]
    if follow:
        args.append("-f")
    args.append(name)
    # Stream straight to the terminal (no capture).
    return run(args, check=False).returncode


def build(image: str, context: Path, *, dockerfile: Path | None = None,
          dockerfile_text: str | None = None) -> None:
    args = ["build", "-t", image]
    if dockerfile_text is not None:
        args += ["-f", "-", str(context)]
        run(args, capture=True, input_text=dockerfile_text)
    else:
        if dockerfile is not None:
            args += ["-f", str(dockerfile)]
        args.append(str(context))
        run(args, capture=True)


def run_container(*, name: str, image: str, network: str | None = None,
                  env: dict | None = None, labels: dict | None = None,
                  ports: dict | None = None, volumes: dict | None = None,
                  restart: str = "unless-stopped") -> None:
    args = ["run", "-d", "--name", name, "--restart", restart]
    if network:
        args += ["--network", network]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    for k, v in (labels or {}).items():
        args += ["--label", f"{k}={v}"]
    # ports: {"127.0.0.1:8080": 80}  -> -p 127.0.0.1:8080:80
    for host, container in (ports or {}).items():
        args += ["-p", f"{host}:{container}"]
    # volumes: {"/host/path": "/container/path:ro"}  -> -v /host/path:/container/path:ro
    for host, container in (volumes or {}).items():
        args += ["-v", f"{host}:{container}"]
    args.append(image)
    run(args, capture=True)


def managed_states() -> dict[str, str]:
    """Map app-name -> container state for all Loom-managed containers."""
    r = run(
        ["ps", "-a", "--filter", f"label={MANAGED_LABEL}", "--format", "{{json .}}"],
        capture=True,
        check=False,
    )
    out: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        labels = row.get("Labels", "")
        app = ""
        for kv in labels.split(","):
            if kv.startswith("loom.app="):
                app = kv.split("=", 1)[1]
        if app:
            # docker ps State is like "running" / "exited".
            out[app] = row.get("State", "unknown")
    return out
