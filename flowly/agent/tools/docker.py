"""Docker integration tool for managing containers, images, and compose stacks."""

import asyncio
import json
from typing import Any

from loguru import logger

from flowly.agent.tools.base import Tool


class DockerTool(Tool):
    """
    Tool to manage Docker containers, images, volumes, and compose stacks.

    Requires Docker to be installed and accessible.
    """

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "docker"

    @property
    def description(self) -> str:
        return """Manage Docker containers, images, volumes, and compose stacks.

Actions:
- ps: List running containers (all=true for stopped too)
- logs: Get container logs (container, tail=100)
- start: Start a stopped container
- stop: Stop a running container
- restart: Restart a container
- rm: Remove a container (force=true to force)
- exec: Execute a command in a container
- images: List images
- pull: Pull an image
- stats: Get container resource usage
- inspect: Get detailed container info
- compose_up: Start compose stack (path to docker-compose.yml)
- compose_down: Stop compose stack
- compose_ps: List compose services
- compose_logs: Get compose service logs
- volumes: List volumes
- networks: List networks
- prune: Clean up unused resources (type: containers/images/volumes/all)

Requires Docker to be installed and the user to have Docker permissions."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": [
                        "ps", "logs", "start", "stop", "restart", "rm", "exec",
                        "images", "pull", "stats", "inspect",
                        "compose_up", "compose_down", "compose_ps", "compose_logs",
                        "volumes", "networks", "prune"
                    ]
                },
                "container": {
                    "type": "string",
                    "description": "Container name or ID"
                },
                "image": {
                    "type": "string",
                    "description": "Image name (for pull)"
                },
                "command": {
                    "type": "string",
                    "description": "Command to execute (for exec)"
                },
                "path": {
                    "type": "string",
                    "description": "Path to docker-compose.yml (for compose commands)"
                },
                "service": {
                    "type": "string",
                    "description": "Service name (for compose_logs)"
                },
                "tail": {
                    "type": "integer",
                    "description": "Number of log lines to show (default: 100)"
                },
                "all": {
                    "type": "boolean",
                    "description": "Include stopped containers (for ps)"
                },
                "force": {
                    "type": "boolean",
                    "description": "Force operation (for rm)"
                },
                "type": {
                    "type": "string",
                    "description": "Resource type for prune (containers/images/volumes/all)",
                    "enum": ["containers", "images", "volumes", "all"]
                },
                "detach": {
                    "type": "boolean",
                    "description": "Run in detached mode (for compose_up)"
                }
            },
            "required": ["action"]
        }

    async def _run_command(self, cmd: list[str]) -> tuple[int, str, str]:
        """Run a command and return exit code, stdout, stderr."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout
            )
            max_bytes = 256 * 1024
            out = stdout[:max_bytes].decode(errors="replace")
            err = stderr[:max_bytes].decode(errors="replace")
            return proc.returncode or 0, out, err
        except asyncio.TimeoutError:
            return -1, "", f"Command timed out after {self.timeout}s"
        except FileNotFoundError:
            return -1, "", "Docker not found. Is Docker installed?"
        except Exception as e:
            return -1, "", str(e)

    async def execute(self, action: str, **kwargs: Any) -> str:
        """Execute a Docker action."""
        try:
            if action == "ps":
                return await self._ps(kwargs.get("all", False))
            elif action == "logs":
                return await self._logs(
                    kwargs.get("container", ""),
                    kwargs.get("tail", 100)
                )
            elif action == "start":
                return await self._start(kwargs.get("container", ""))
            elif action == "stop":
                return await self._stop(kwargs.get("container", ""))
            elif action == "restart":
                return await self._restart(kwargs.get("container", ""))
            elif action == "rm":
                return await self._rm(
                    kwargs.get("container", ""),
                    kwargs.get("force", False)
                )
            elif action == "exec":
                return await self._exec(
                    kwargs.get("container", ""),
                    kwargs.get("command", "")
                )
            elif action == "images":
                return await self._images()
            elif action == "pull":
                return await self._pull(kwargs.get("image", ""))
            elif action == "stats":
                return await self._stats(kwargs.get("container"))
            elif action == "inspect":
                return await self._inspect(kwargs.get("container", ""))
            elif action == "compose_up":
                return await self._compose_up(
                    kwargs.get("path", ""),
                    kwargs.get("detach", True)
                )
            elif action == "compose_down":
                return await self._compose_down(kwargs.get("path", ""))
            elif action == "compose_ps":
                return await self._compose_ps(kwargs.get("path", ""))
            elif action == "compose_logs":
                return await self._compose_logs(
                    kwargs.get("path", ""),
                    kwargs.get("service"),
                    kwargs.get("tail", 100)
                )
            elif action == "volumes":
                return await self._volumes()
            elif action == "networks":
                return await self._networks()
            elif action == "prune":
                return await self._prune(kwargs.get("type", "all"))
            else:
                return f"Unknown action: {action}"
        except Exception as e:
            logger.error(f"Docker error: {e}")
            return f"Error: {str(e)}"

    async def _ps(self, all_containers: bool = False) -> str:
        """List containers."""
        cmd = ["docker", "ps", "--format", "json"]
        if all_containers:
            cmd.append("-a")

        code, stdout, stderr = await self._run_command(cmd)
        if code != 0:
            return f"Error: {stderr}"

        if not stdout.strip():
            return "No containers found."

        lines = ["**Docker Containers:**\n"]
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                c = json.loads(line)
                status_icon = "🟢" if "Up" in c.get("Status", "") else "🔴"
                lines.append(f"{status_icon} **{c.get('Names', 'unknown')}**")
                lines.append(f"   Image: {c.get('Image', 'unknown')}")
                lines.append(f"   Status: {c.get('Status', 'unknown')}")
                lines.append(f"   Ports: {c.get('Ports', '-')}")
                lines.append(f"   ID: {c.get('ID', 'unknown')[:12]}")
                lines.append("")
            except json.JSONDecodeError:
                continue

        return "\n".join(lines)

    async def _logs(self, container: str, tail: int = 100) -> str:
        """Get container logs."""
        if not container:
            return "Error: container name or ID required"

        cmd = ["docker", "logs", "--tail", str(tail), container]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"

        output = stdout or stderr  # Some apps log to stderr
        if not output.strip():
            return f"No logs found for container '{container}'"

        # Truncate if too long
        max_chars = 4000
        if len(output) > max_chars:
            output = output[-max_chars:]
            output = f"...(truncated)\n{output}"

        return f"**Logs for {container}** (last {tail} lines):\n```\n{output}\n```"

    async def _start(self, container: str) -> str:
        """Start a container."""
        if not container:
            return "Error: container name or ID required"

        cmd = ["docker", "start", container]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"
        return f"Started container: {container}"

    async def _stop(self, container: str) -> str:
        """Stop a container."""
        if not container:
            return "Error: container name or ID required"

        cmd = ["docker", "stop", container]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"
        return f"Stopped container: {container}"

    async def _restart(self, container: str) -> str:
        """Restart a container."""
        if not container:
            return "Error: container name or ID required"

        cmd = ["docker", "restart", container]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"
        return f"Restarted container: {container}"

    async def _rm(self, container: str, force: bool = False) -> str:
        """Remove a container."""
        if not container:
            return "Error: container name or ID required"

        cmd = ["docker", "rm", container]
        if force:
            cmd.insert(2, "-f")

        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"
        return f"Removed container: {container}"

    async def _exec(self, container: str, command: str) -> str:
        """Execute a command in a container."""
        if not container:
            return "Error: container name or ID required"
        if not command:
            return "Error: command required"

        # Use shlex for proper shell quoting support
        import shlex
        cmd = ["docker", "exec", container] + shlex.split(command)
        code, stdout, stderr = await self._run_command(cmd)

        output = stdout or stderr
        if code != 0:
            return f"Error (exit {code}): {output}"

        if not output.strip():
            return "(no output)"
        return f"```\n{output}\n```"

    async def _images(self) -> str:
        """List images."""
        cmd = ["docker", "images", "--format", "json"]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"

        if not stdout.strip():
            return "No images found."

        lines = ["**Docker Images:**\n"]
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                img = json.loads(line)
                repo = img.get("Repository", "unknown")
                tag = img.get("Tag", "latest")
                size = img.get("Size", "unknown")
                lines.append(f"- **{repo}:{tag}** ({size})")
            except json.JSONDecodeError:
                continue

        return "\n".join(lines)

    async def _pull(self, image: str) -> str:
        """Pull an image."""
        if not image:
            return "Error: image name required"

        cmd = ["docker", "pull", image]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"
        return f"Pulled image: {image}"

    async def _stats(self, container: str | None = None) -> str:
        """Get container stats."""
        cmd = ["docker", "stats", "--no-stream", "--format", "json"]
        if container:
            cmd.append(container)

        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"

        if not stdout.strip():
            return "No running containers."

        lines = ["**Container Stats:**\n"]
        lines.append("| Container | CPU | Memory | Net I/O |")
        lines.append("|-----------|-----|--------|---------|")

        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                s = json.loads(line)
                name = s.get("Name", "unknown")[:15]
                cpu = s.get("CPUPerc", "0%")
                mem = s.get("MemPerc", "0%")
                net = s.get("NetIO", "0B/0B")
                lines.append(f"| {name} | {cpu} | {mem} | {net} |")
            except json.JSONDecodeError:
                continue

        return "\n".join(lines)

    async def _inspect(self, container: str) -> str:
        """Inspect a container."""
        if not container:
            return "Error: container name or ID required"

        cmd = ["docker", "inspect", container]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"

        try:
            data = json.loads(stdout)
            if not data:
                return f"Container '{container}' not found"

            c = data[0]
            state = c.get("State", {})
            config = c.get("Config", {})
            network = c.get("NetworkSettings", {})

            lines = [f"**Container: {container}**\n"]
            lines.append(f"**ID:** {c.get('Id', 'unknown')[:12]}")
            lines.append(f"**Image:** {config.get('Image', 'unknown')}")
            lines.append(f"**Status:** {state.get('Status', 'unknown')}")
            lines.append(f"**Running:** {state.get('Running', False)}")
            lines.append(f"**Started:** {state.get('StartedAt', 'unknown')}")

            # Environment variables (filtered)
            env = config.get("Env", [])
            safe_env = [e for e in env if not any(s in e.lower() for s in ["password", "secret", "key", "token"])]
            if safe_env:
                lines.append(f"\n**Environment:**")
                for e in safe_env[:10]:
                    lines.append(f"  - {e}")

            # Ports
            ports = network.get("Ports", {})
            if ports:
                lines.append(f"\n**Ports:**")
                for port, bindings in ports.items():
                    if bindings:
                        for b in bindings:
                            lines.append(f"  - {b.get('HostPort', '?')} -> {port}")

            # Mounts
            mounts = c.get("Mounts", [])
            if mounts:
                lines.append(f"\n**Mounts:**")
                for m in mounts[:5]:
                    lines.append(f"  - {m.get('Source', '?')} -> {m.get('Destination', '?')}")

            return "\n".join(lines)
        except json.JSONDecodeError:
            return f"Error parsing container info"

    async def _compose_up(self, path: str, detach: bool = True) -> str:
        """Start a compose stack."""
        if not path:
            return "Error: path to docker-compose.yml required"

        cmd = ["docker", "compose", "-f", path, "up"]
        if detach:
            cmd.append("-d")

        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"
        return f"Started compose stack: {path}\n{stdout}"

    async def _compose_down(self, path: str) -> str:
        """Stop a compose stack."""
        if not path:
            return "Error: path to docker-compose.yml required"

        cmd = ["docker", "compose", "-f", path, "down"]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"
        return f"Stopped compose stack: {path}"

    async def _compose_ps(self, path: str) -> str:
        """List compose services."""
        if not path:
            return "Error: path to docker-compose.yml required"

        cmd = ["docker", "compose", "-f", path, "ps", "--format", "json"]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"

        if not stdout.strip():
            return "No services found."

        lines = [f"**Compose Services ({path}):**\n"]
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                s = json.loads(line)
                status_icon = "🟢" if s.get("State") == "running" else "🔴"
                lines.append(f"{status_icon} **{s.get('Service', 'unknown')}**")
                lines.append(f"   Status: {s.get('State', 'unknown')}")
                lines.append(f"   Ports: {s.get('Publishers', [])}")
                lines.append("")
            except json.JSONDecodeError:
                continue

        return "\n".join(lines)

    async def _compose_logs(self, path: str, service: str | None, tail: int = 100) -> str:
        """Get compose service logs."""
        if not path:
            return "Error: path to docker-compose.yml required"

        cmd = ["docker", "compose", "-f", path, "logs", "--tail", str(tail)]
        if service:
            cmd.append(service)

        code, stdout, stderr = await self._run_command(cmd)

        output = stdout or stderr
        if code != 0:
            return f"Error: {output}"

        if not output.strip():
            return "No logs found."

        # Truncate if too long
        max_chars = 4000
        if len(output) > max_chars:
            output = output[-max_chars:]
            output = f"...(truncated)\n{output}"

        service_info = f" ({service})" if service else ""
        return f"**Compose Logs{service_info}:**\n```\n{output}\n```"

    async def _volumes(self) -> str:
        """List volumes."""
        cmd = ["docker", "volume", "ls", "--format", "json"]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"

        if not stdout.strip():
            return "No volumes found."

        lines = ["**Docker Volumes:**\n"]
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                v = json.loads(line)
                lines.append(f"- **{v.get('Name', 'unknown')}** (Driver: {v.get('Driver', 'local')})")
            except json.JSONDecodeError:
                continue

        return "\n".join(lines)

    async def _networks(self) -> str:
        """List networks."""
        cmd = ["docker", "network", "ls", "--format", "json"]
        code, stdout, stderr = await self._run_command(cmd)

        if code != 0:
            return f"Error: {stderr}"

        if not stdout.strip():
            return "No networks found."

        lines = ["**Docker Networks:**\n"]
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                n = json.loads(line)
                lines.append(f"- **{n.get('Name', 'unknown')}** ({n.get('Driver', 'bridge')})")
            except json.JSONDecodeError:
                continue

        return "\n".join(lines)

    async def _prune(self, resource_type: str = "all") -> str:
        """Clean up unused resources."""
        results = []

        if resource_type in ("containers", "all"):
            cmd = ["docker", "container", "prune", "-f"]
            code, stdout, stderr = await self._run_command(cmd)
            if code == 0:
                results.append(f"Containers: {stdout.strip()}")

        if resource_type in ("images", "all"):
            cmd = ["docker", "image", "prune", "-f"]
            code, stdout, stderr = await self._run_command(cmd)
            if code == 0:
                results.append(f"Images: {stdout.strip()}")

        if resource_type in ("volumes", "all"):
            cmd = ["docker", "volume", "prune", "-f"]
            code, stdout, stderr = await self._run_command(cmd)
            if code == 0:
                results.append(f"Volumes: {stdout.strip()}")

        if resource_type == "all":
            cmd = ["docker", "network", "prune", "-f"]
            code, stdout, stderr = await self._run_command(cmd)
            if code == 0:
                results.append(f"Networks: {stdout.strip()}")

        if not results:
            return "Nothing to clean up."

        return "**Cleanup Results:**\n" + "\n".join(results)
