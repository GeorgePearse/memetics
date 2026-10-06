"""Run trusted manifest commands on a disposable copy, without host secrets/network."""

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from .git import clean_env


class DockerValidator:
    def run(self, checkout, config):
        image = config["adaptation"]["validation_image"]
        results = []
        with tempfile.TemporaryDirectory(prefix="memetics-check-") as tmp:
            workspace = Path(tmp) / "workspace"
            shutil.copytree(
                checkout,
                workspace,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git"),
            )
            for command in config["adaptation"]["validation_commands"]:
                name = "memetics-" + uuid.uuid4().hex
                argv = [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--pull=never",
                    "--network=none",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--read-only",
                    "--pids-limit=256",
                    "--memory=1g",
                    "--cpus=2",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--tmpfs",
                    "/tmp:rw,nosuid,size=256m",
                    "--env",
                    "HOME=/tmp",
                    "--mount",
                    f"type=bind,src={workspace},dst=/workspace",
                    "--workdir",
                    "/workspace",
                    image,
                    "sh",
                    "-lc",
                    command,
                ]
                try:
                    # A file prevents a noisy test from filling the worker's RAM.
                    with tempfile.TemporaryFile() as log:
                        run = subprocess.run(
                            argv,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            env=clean_env(),
                            timeout=config["adaptation"]["timeout_seconds"],
                        )
                        log.seek(0, 2)
                        size = log.tell()
                        log.seek(max(0, size - 16000))
                        text = log.read().decode(errors="replace")
                    results.append(
                        {
                            "command": command,
                            "exit_code": run.returncode,
                            "output": text,
                            "output_truncated": size > 16000,
                        }
                    )
                except (subprocess.TimeoutExpired, OSError) as exc:
                    results.append(
                        {"command": command, "exit_code": None, "output": str(exc)}
                    )
                finally:
                    try:
                        subprocess.run(
                            ["docker", "rm", "-f", name],
                            capture_output=True,
                            env=clean_env(),
                            timeout=15,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        return {
            "passed": all(r["exit_code"] == 0 for r in results),
            "runner": "docker",
            "image": image,
            "results": results,
        }
