"""System monitoring tool for CPU, RAM, disk, network, and process info."""

import asyncio
import platform
import os
from typing import Any
from datetime import datetime

from loguru import logger

from flowly.agent.tools.base import Tool


class SystemTool(Tool):
    """
    Tool to monitor system resources: CPU, RAM, disk, network, processes.

    Works on Linux, macOS, and Windows without external dependencies.
    """

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.system = platform.system().lower()

    @property
    def name(self) -> str:
        return "system"

    @property
    def description(self) -> str:
        return """Monitor system resources and processes.

Actions:
- overview: Quick system overview (CPU, RAM, disk, uptime)
- cpu: Detailed CPU information and usage
- memory: RAM and swap usage
- disk: Disk usage for all drives
- network: Network interfaces and connections
- processes: Top processes by CPU/memory (sort_by: cpu/memory, limit: 10)
- uptime: System uptime and boot time
- info: System information (OS, kernel, hostname)
- services: List running services
- ports: List listening ports

Works on Linux, macOS, and Windows without external dependencies."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": [
                        "overview", "cpu", "memory", "disk", "network",
                        "processes", "uptime", "info", "services", "ports"
                    ]
                },
                "sort_by": {
                    "type": "string",
                    "description": "Sort processes by (for processes action)",
                    "enum": ["cpu", "memory"]
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of processes to show (default: 10)"
                }
            },
            "required": ["action"]
        }

    async def _run_command(self, cmd: str) -> tuple[int, str]:
        """Run a shell command and return exit code and output."""
        try:
            max_bytes = 128 * 1024
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout
            )
            out = stdout[:max_bytes].decode(errors="replace")
            err = stderr[:max_bytes].decode(errors="replace")
            output = out or err
            return proc.returncode or 0, output
        except asyncio.TimeoutError:
            return -1, f"Command timed out after {self.timeout}s"
        except Exception as e:
            return -1, str(e)

    async def _run_powershell(self, cmd: str) -> tuple[int, str]:
        """Run a PowerShell command and return exit code and output."""
        try:
            max_bytes = 128 * 1024
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
            out = stdout[:max_bytes].decode(errors="replace")
            err = stderr[:max_bytes].decode(errors="replace")
            output = out or err
            return proc.returncode or 0, output
        except asyncio.TimeoutError:
            return -1, f"Command timed out after {self.timeout}s"
        except Exception as e:
            return -1, str(e)

    async def execute(self, action: str, **kwargs: Any) -> str:
        """Execute a system monitoring action."""
        try:
            if action == "overview":
                return await self._overview()
            elif action == "cpu":
                return await self._cpu()
            elif action == "memory":
                return await self._memory()
            elif action == "disk":
                return await self._disk()
            elif action == "network":
                return await self._network()
            elif action == "processes":
                return await self._processes(
                    kwargs.get("sort_by", "cpu"),
                    kwargs.get("limit", 10)
                )
            elif action == "uptime":
                return await self._uptime()
            elif action == "info":
                return await self._info()
            elif action == "services":
                return await self._services()
            elif action == "ports":
                return await self._ports()
            else:
                return f"Unknown action: {action}"
        except Exception as e:
            logger.error(f"System tool error: {e}")
            return f"Error: {str(e)}"

    async def _overview(self) -> str:
        """Get a quick system overview."""
        lines = ["**System Overview**\n"]

        # Get all info in parallel
        cpu_task = self._get_cpu_usage()
        mem_task = self._get_memory_info()
        disk_task = self._get_disk_usage()
        uptime_task = self._get_uptime()

        cpu, mem, disk, uptime = await asyncio.gather(
            cpu_task, mem_task, disk_task, uptime_task
        )

        # CPU
        lines.append(f"**CPU:** {cpu}")

        # Memory
        lines.append(f"**Memory:** {mem}")

        # Disk
        lines.append(f"**Disk:** {disk}")

        # Uptime
        lines.append(f"**Uptime:** {uptime}")

        # Load (Linux/macOS only)
        if self.system != "windows":
            code, output = await self._run_command("uptime")
            if code == 0 and "load average" in output.lower():
                load = output.split("load average:")[-1].strip() if "load average:" in output else output.split("load averages:")[-1].strip()
                lines.append(f"**Load:** {load}")

        return "\n".join(lines)

    async def _get_cpu_usage(self) -> str:
        """Get CPU usage percentage."""
        if self.system == "windows":
            code, output = await self._run_powershell(
                "(Get-CimInstance Win32_Processor).LoadPercentage"
            )
            if code == 0 and output.strip():
                try:
                    usage = float(output.strip())
                    return f"{usage:.1f}% used"
                except:
                    pass
        elif self.system == "darwin":
            # macOS
            code, output = await self._run_command(
                "top -l 1 -n 0 | grep 'CPU usage'"
            )
            if code == 0 and output:
                return output.strip().replace("CPU usage: ", "")
        else:
            # Linux
            code, output = await self._run_command(
                "top -bn1 | grep 'Cpu(s)' | awk '{print $2}'"
            )
            if code == 0 and output:
                try:
                    usage = float(output.strip().replace(",", "."))
                    return f"{usage:.1f}% used"
                except:
                    pass

            # Fallback: /proc/stat
            code, output = await self._run_command(
                "grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {print usage}'"
            )
            if code == 0 and output:
                try:
                    usage = float(output.strip())
                    return f"{usage:.1f}% used"
                except:
                    pass

        return "N/A"

    async def _get_memory_info(self) -> str:
        """Get memory usage."""
        if self.system == "windows":
            code, output = await self._run_powershell(
                "$os = Get-CimInstance Win32_OperatingSystem; "
                "$total = $os.TotalVisibleMemorySize * 1024; "
                "$free = $os.FreePhysicalMemory * 1024; "
                "$used = $total - $free; "
                "$pct = [math]::Round(($used / $total) * 100, 1); "
                "Write-Output \"$used|$total|$pct\""
            )
            if code == 0 and "|" in output:
                try:
                    parts = output.strip().split("|")
                    used = int(float(parts[0]))
                    total = int(float(parts[1]))
                    pct = float(parts[2])
                    return f"{self._format_bytes(used)} / {self._format_bytes(total)} ({pct}%)"
                except:
                    pass
        elif self.system == "darwin":
            # macOS - use vm_stat
            code, output = await self._run_command("vm_stat")
            if code == 0:
                try:
                    lines = output.split("\n")
                    stats = {}
                    for line in lines:
                        if ":" in line:
                            key, val = line.split(":")
                            stats[key.strip()] = int(val.strip().replace(".", ""))

                    page_size = 16384  # 16KB pages on Apple Silicon, 4KB on Intel
                    # Try to get actual page size
                    code2, ps_out = await self._run_command("pagesize")
                    if code2 == 0:
                        try:
                            page_size = int(ps_out.strip())
                        except:
                            pass

                    free = stats.get("Pages free", 0) * page_size
                    active = stats.get("Pages active", 0) * page_size
                    inactive = stats.get("Pages inactive", 0) * page_size
                    wired = stats.get("Pages wired down", 0) * page_size

                    total_code, total_out = await self._run_command("sysctl -n hw.memsize")
                    total = int(total_out.strip()) if total_code == 0 else (free + active + inactive + wired)

                    used = active + wired
                    used_pct = (used / total) * 100 if total > 0 else 0

                    return f"{self._format_bytes(used)} / {self._format_bytes(total)} ({used_pct:.1f}%)"
                except Exception as e:
                    pass
        else:
            # Linux - use /proc/meminfo
            code, output = await self._run_command("cat /proc/meminfo")
            if code == 0:
                try:
                    stats = {}
                    for line in output.split("\n"):
                        if ":" in line:
                            key, val = line.split(":")
                            # Value is in kB
                            num = int(val.strip().split()[0]) * 1024
                            stats[key.strip()] = num

                    total = stats.get("MemTotal", 0)
                    available = stats.get("MemAvailable", stats.get("MemFree", 0))
                    used = total - available
                    used_pct = (used / total) * 100 if total > 0 else 0

                    return f"{self._format_bytes(used)} / {self._format_bytes(total)} ({used_pct:.1f}%)"
                except:
                    pass

        return "N/A"

    async def _get_disk_usage(self) -> str:
        """Get primary disk usage."""
        if self.system == "windows":
            code, output = await self._run_powershell(
                "$d = Get-PSDrive C; "
                "$used = $d.Used; $free = $d.Free; $total = $used + $free; "
                "$pct = [math]::Round(($used / $total) * 100, 0); "
                "Write-Output \"$used|$total|$pct\""
            )
            if code == 0 and "|" in output:
                try:
                    parts = output.strip().split("|")
                    used = int(float(parts[0]))
                    total = int(float(parts[1]))
                    pct = int(float(parts[2]))
                    return f"{self._format_bytes(used)} / {self._format_bytes(total)} ({pct}%)"
                except:
                    pass
        else:
            code, output = await self._run_command("df -h / | tail -1")
            if code == 0 and output:
                parts = output.split()
                if len(parts) >= 5:
                    used = parts[2]
                    total = parts[1]
                    pct = parts[4]
                    return f"{used} / {total} ({pct})"
        return "N/A"

    async def _get_uptime(self) -> str:
        """Get system uptime."""
        if self.system == "windows":
            code, output = await self._run_powershell(
                "$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime; "
                "$uptime = (Get-Date) - $boot; "
                "'{0} days, {1} hours, {2} minutes' -f $uptime.Days, $uptime.Hours, $uptime.Minutes"
            )
            if code == 0 and output:
                return output.strip()
        else:
            code, output = await self._run_command("uptime -p 2>/dev/null || uptime")
            if code == 0 and output:
                if "up " in output:
                    # Extract just the uptime part
                    uptime_part = output.split("up ")[-1].split(",")[0:2]
                    return ", ".join(uptime_part).strip().rstrip(",")
        return "N/A"

    def _format_bytes(self, bytes_val: int) -> str:
        """Format bytes to human readable."""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if bytes_val < 1024:
                return f"{bytes_val:.1f} {unit}"
            bytes_val /= 1024
        return f"{bytes_val:.1f} PB"

    async def _cpu(self) -> str:
        """Get detailed CPU information."""
        lines = ["**CPU Information**\n"]

        if self.system == "windows":
            # CPU model
            code, output = await self._run_powershell(
                "(Get-CimInstance Win32_Processor).Name"
            )
            if code == 0 and output:
                lines.append(f"**Model:** {output.strip()}")

            # Core count
            code, output = await self._run_powershell(
                "(Get-CimInstance Win32_Processor).NumberOfCores"
            )
            if code == 0 and output:
                lines.append(f"**Cores:** {output.strip()}")

            # Logical processors
            code, output = await self._run_powershell(
                "(Get-CimInstance Win32_Processor).NumberOfLogicalProcessors"
            )
            if code == 0 and output:
                lines.append(f"**Logical Processors:** {output.strip()}")

        elif self.system == "darwin":
            code, output = await self._run_command("sysctl -n machdep.cpu.brand_string")
            if code == 0 and output:
                lines.append(f"**Model:** {output.strip()}")

            code, output = await self._run_command("sysctl -n hw.ncpu")
            if code == 0 and output:
                lines.append(f"**Cores:** {output.strip()}")
        else:
            code, output = await self._run_command("cat /proc/cpuinfo | grep 'model name' | head -1 | cut -d: -f2")
            if code == 0 and output:
                lines.append(f"**Model:** {output.strip()}")

            code, output = await self._run_command("nproc")
            if code == 0 and output:
                lines.append(f"**Cores:** {output.strip()}")

        # Current usage
        usage = await self._get_cpu_usage()
        lines.append(f"**Usage:** {usage}")

        # Load averages (Unix only)
        if self.system != "windows":
            code, output = await self._run_command("uptime")
            if code == 0 and "load average" in output.lower():
                load = output.split("load average")[-1].replace(":", "").replace("s:", "").strip()
                lines.append(f"**Load (1/5/15 min):** {load}")

        # Per-core usage (Linux only)
        if self.system == "linux":
            code, output = await self._run_command(
                "mpstat -P ALL 1 1 2>/dev/null | grep -E '^Average:' | tail -n +2 | head -5"
            )
            if code == 0 and output:
                lines.append("\n**Per-Core Usage:**")
                for line in output.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 3:
                        core = parts[1]
                        idle = parts[-1] if parts[-1] != "-" else "0"
                        try:
                            usage = 100 - float(idle.replace(",", "."))
                            lines.append(f"  Core {core}: {usage:.1f}%")
                        except:
                            pass

        return "\n".join(lines)

    async def _memory(self) -> str:
        """Get detailed memory information."""
        lines = ["**Memory Information**\n"]

        # RAM usage
        mem = await self._get_memory_info()
        lines.append(f"**RAM:** {mem}")

        if self.system == "windows":
            # Virtual memory / page file
            code, output = await self._run_powershell(
                "$pf = Get-CimInstance Win32_PageFileUsage; "
                "if ($pf) { "
                "$used = $pf.CurrentUsage * 1MB; "
                "$total = $pf.AllocatedBaseSize * 1MB; "
                "$pct = if($total -gt 0){[math]::Round(($used/$total)*100,1)}else{0}; "
                "Write-Output \"$used|$total|$pct\" "
                "}"
            )
            if code == 0 and "|" in output:
                try:
                    parts = output.strip().split("|")
                    used = int(float(parts[0]))
                    total = int(float(parts[1]))
                    pct = float(parts[2])
                    lines.append(f"**Page File:** {self._format_bytes(used)} / {self._format_bytes(total)} ({pct}%)")
                except:
                    pass

        elif self.system == "linux":
            # Detailed breakdown from /proc/meminfo
            code, output = await self._run_command("cat /proc/meminfo")
            if code == 0:
                stats = {}
                for line in output.split("\n"):
                    if ":" in line:
                        key, val = line.split(":")
                        try:
                            num = int(val.strip().split()[0]) * 1024
                            stats[key.strip()] = num
                        except:
                            pass

                if "Buffers" in stats:
                    lines.append(f"**Buffers:** {self._format_bytes(stats['Buffers'])}")
                if "Cached" in stats:
                    lines.append(f"**Cached:** {self._format_bytes(stats['Cached'])}")
                if "SwapTotal" in stats and stats["SwapTotal"] > 0:
                    swap_used = stats["SwapTotal"] - stats.get("SwapFree", 0)
                    swap_pct = (swap_used / stats["SwapTotal"]) * 100
                    lines.append(f"**Swap:** {self._format_bytes(swap_used)} / {self._format_bytes(stats['SwapTotal'])} ({swap_pct:.1f}%)")
        else:
            # macOS swap
            code, output = await self._run_command("sysctl -n vm.swapusage")
            if code == 0 and output:
                lines.append(f"**Swap:** {output.strip()}")

        return "\n".join(lines)

    async def _disk(self) -> str:
        """Get disk usage for all mounts/drives."""
        lines = ["**Disk Usage**\n"]

        if self.system == "windows":
            code, output = await self._run_powershell(
                "Get-PSDrive -PSProvider FileSystem | ForEach-Object { "
                "$used = $_.Used; $free = $_.Free; "
                "if ($used -ne $null -and $free -ne $null) { "
                "$total = $used + $free; "
                "$pct = if($total -gt 0){[math]::Round(($used/$total)*100,0)}else{0}; "
                "Write-Output ('{0}|{1}|{2}|{3}|{4}' -f $_.Name, $total, $used, $free, $pct) "
                "} }"
            )
            if code == 0 and output:
                lines.append("| Drive | Size | Used | Free | Use% |")
                lines.append("|-------|------|------|------|------|")

                for line in output.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 5:
                            drive = parts[0] + ":"
                            try:
                                total = int(float(parts[1]))
                                used = int(float(parts[2]))
                                free = int(float(parts[3]))
                                pct = int(float(parts[4]))
                                pct_str = f"{pct}%"
                                if pct >= 90:
                                    pct_str = f"**{pct}%**"
                                lines.append(f"| {drive} | {self._format_bytes(total)} | {self._format_bytes(used)} | {self._format_bytes(free)} | {pct_str} |")
                            except:
                                pass
        else:
            code, output = await self._run_command("df -h | grep -E '^/dev'")
            if code != 0 or not output:
                code, output = await self._run_command("df -h")

            if code == 0 and output:
                lines.append("| Mount | Size | Used | Avail | Use% |")
                lines.append("|-------|------|------|-------|------|")

                for line in output.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 6 and parts[0].startswith("/"):
                        mount = parts[5] if len(parts) > 5 else parts[0]
                        size = parts[1]
                        used = parts[2]
                        avail = parts[3]
                        pct = parts[4]

                        # Add warning for high usage
                        try:
                            pct_num = int(pct.replace("%", ""))
                            if pct_num >= 90:
                                pct = f"**{pct}**"
                        except:
                            pass

                        lines.append(f"| {mount} | {size} | {used} | {avail} | {pct} |")

        return "\n".join(lines)

    async def _network(self) -> str:
        """Get network interface information."""
        lines = ["**Network Interfaces**\n"]

        if self.system == "windows":
            code, output = await self._run_powershell(
                "Get-NetIPAddress -AddressFamily IPv4 | "
                "Where-Object { $_.IPAddress -ne '127.0.0.1' } | "
                "ForEach-Object { Write-Output ('{0}|{1}' -f $_.InterfaceAlias, $_.IPAddress) }"
            )
            if code == 0 and output:
                for line in output.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 2:
                            lines.append(f"- **{parts[0]}:** {parts[1]}")

            # Active connections count
            code, output = await self._run_powershell(
                "(Get-NetTCPConnection -State Established).Count"
            )
            if code == 0 and output:
                try:
                    conn_count = int(output.strip())
                    lines.append(f"\n**Active Connections:** {conn_count}")
                except:
                    pass

        elif self.system == "darwin":
            code, output = await self._run_command("ifconfig | grep -E '^[a-z]|inet '")
            if code == 0 and output:
                current_iface = ""
                for line in output.strip().split("\n"):
                    if not line.startswith("\t") and not line.startswith(" "):
                        current_iface = line.split(":")[0]
                    elif "inet " in line and current_iface:
                        ip = line.strip().split()[1]
                        if not ip.startswith("127."):
                            lines.append(f"- **{current_iface}:** {ip}")

            code, output = await self._run_command("netstat -an | grep ESTABLISHED | wc -l")
            if code == 0 and output:
                try:
                    conn_count = int(output.strip())
                    lines.append(f"\n**Active Connections:** {conn_count}")
                except:
                    pass
        else:
            code, output = await self._run_command("ip -o addr show | grep -v 'scope host'")
            if code == 0 and output:
                for line in output.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4:
                        iface = parts[1]
                        ip = parts[3].split("/")[0]
                        if not ip.startswith("127.") and not ip.startswith("::1"):
                            lines.append(f"- **{iface}:** {ip}")

            code, output = await self._run_command("ss -tun | wc -l")
            if code == 0 and output:
                try:
                    conn_count = int(output.strip()) - 1  # Subtract header
                    lines.append(f"\n**Active Connections:** {max(0, conn_count)}")
                except:
                    pass

        return "\n".join(lines)

    async def _processes(self, sort_by: str = "cpu", limit: int = 10) -> str:
        """Get top processes."""
        lines = [f"**Top {limit} Processes (by {sort_by.upper()})**\n"]

        if self.system == "windows":
            if sort_by == "memory":
                sort_prop = "WorkingSet64"
            else:
                sort_prop = "CPU"

            code, output = await self._run_powershell(
                f"Get-Process | Sort-Object {sort_prop} -Descending | "
                f"Select-Object -First {limit} | "
                "ForEach-Object { "
                "$cpu = [math]::Round($_.CPU, 1); "
                "$mem = [math]::Round($_.WorkingSet64 / 1MB, 1); "
                "Write-Output ('{0}|{1}|{2}|{3}' -f $_.Id, $cpu, $mem, $_.ProcessName) "
                "}"
            )
            if code == 0 and output:
                lines.append("| PID | CPU (s) | MEM (MB) | Process |")
                lines.append("|-----|---------|----------|---------|")

                for line in output.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 4:
                            pid = parts[0]
                            cpu = parts[1]
                            mem = parts[2]
                            name = parts[3][:30]
                            lines.append(f"| {pid} | {cpu} | {mem} | {name} |")
        else:
            if sort_by == "memory":
                if self.system == "darwin":
                    cmd = f"ps aux | sort -nrk 4 | head -{limit + 1}"
                else:
                    cmd = f"ps aux --sort=-%mem | head -{limit + 1}"
            else:
                if self.system == "darwin":
                    cmd = f"ps aux | sort -nrk 3 | head -{limit + 1}"
                else:
                    cmd = f"ps aux --sort=-%cpu | head -{limit + 1}"

            code, output = await self._run_command(cmd)
            if code == 0 and output:
                lines.append("| PID | CPU% | MEM% | Command |")
                lines.append("|-----|------|------|---------|")

                proc_lines = output.strip().split("\n")[1:]  # Skip header
                for line in proc_lines[:limit]:
                    parts = line.split()
                    if len(parts) >= 11:
                        pid = parts[1]
                        cpu = parts[2]
                        mem = parts[3]
                        cmd = " ".join(parts[10:])[:40]  # Truncate command
                        lines.append(f"| {pid} | {cpu} | {mem} | {cmd} |")

        return "\n".join(lines)

    async def _uptime(self) -> str:
        """Get system uptime and boot time."""
        lines = ["**System Uptime**\n"]

        if self.system == "windows":
            code, output = await self._run_powershell(
                "$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime; "
                "$uptime = (Get-Date) - $boot; "
                "Write-Output ('Uptime: {0} days, {1} hours, {2} minutes' -f $uptime.Days, $uptime.Hours, $uptime.Minutes); "
                "Write-Output ('Boot Time: {0}' -f $boot.ToString('yyyy-MM-dd HH:mm:ss'))"
            )
            if code == 0 and output:
                for line in output.strip().split("\n"):
                    if line.startswith("Uptime:"):
                        lines.append(f"**{line}**")
                    elif line.startswith("Boot Time:"):
                        lines.append(f"**{line}**")
        else:
            # Uptime
            code, output = await self._run_command("uptime")
            if code == 0 and output:
                lines.append(f"**Uptime:** {output.strip()}")

            # Boot time
            if self.system == "darwin":
                code, output = await self._run_command("sysctl -n kern.boottime")
                if code == 0 and "sec" in output:
                    try:
                        # Parse { sec = 1234567890, usec = 0 }
                        sec = int(output.split("sec = ")[1].split(",")[0])
                        boot_time = datetime.fromtimestamp(sec)
                        lines.append(f"**Boot Time:** {boot_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    except:
                        pass
            else:
                code, output = await self._run_command("uptime -s 2>/dev/null")
                if code == 0 and output:
                    lines.append(f"**Boot Time:** {output.strip()}")

            # Load averages
            code, output = await self._run_command("uptime")
            if code == 0 and "load average" in output.lower():
                load = output.split("load average")[-1].replace(":", "").replace("s:", "").strip()
                lines.append(f"**Load (1/5/15 min):** {load}")

        return "\n".join(lines)

    async def _info(self) -> str:
        """Get system information."""
        lines = ["**System Information**\n"]

        # Hostname
        if self.system == "windows":
            code, output = await self._run_powershell("$env:COMPUTERNAME")
        else:
            code, output = await self._run_command("hostname")
        if code == 0:
            lines.append(f"**Hostname:** {output.strip()}")

        # OS
        lines.append(f"**OS:** {platform.system()} {platform.release()}")
        lines.append(f"**Platform:** {platform.platform()}")

        # Architecture
        lines.append(f"**Architecture:** {platform.machine()}")

        if self.system == "windows":
            # Windows version details
            code, output = await self._run_powershell(
                "(Get-CimInstance Win32_OperatingSystem).Caption"
            )
            if code == 0 and output:
                lines.append(f"**Windows Version:** {output.strip()}")

            # Build number
            code, output = await self._run_powershell(
                "(Get-CimInstance Win32_OperatingSystem).BuildNumber"
            )
            if code == 0 and output:
                lines.append(f"**Build:** {output.strip()}")

        elif self.system == "linux":
            code, output = await self._run_command("uname -r")
            if code == 0:
                lines.append(f"**Kernel:** {output.strip()}")

            # Distribution
            code, output = await self._run_command("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2")
            if code == 0 and output:
                distro = output.strip().strip('"')
                lines.append(f"**Distribution:** {distro}")
        else:
            # macOS version
            code, output = await self._run_command("sw_vers -productVersion")
            if code == 0:
                lines.append(f"**macOS Version:** {output.strip()}")

        # Current user
        if self.system == "windows":
            lines.append(f"**User:** {os.getenv('USERNAME', 'unknown')}")
        else:
            lines.append(f"**User:** {os.getenv('USER', 'unknown')}")

        return "\n".join(lines)

    async def _services(self) -> str:
        """List running services."""
        lines = ["**Running Services**\n"]

        if self.system == "windows":
            code, output = await self._run_powershell(
                "Get-Service | Where-Object {$_.Status -eq 'Running'} | "
                "Select-Object -First 20 | "
                "ForEach-Object { Write-Output ('{0}|{1}' -f $_.Name, $_.DisplayName) }"
            )
            if code == 0 and output:
                lines.append("| Service | Display Name |")
                lines.append("|---------|--------------|")

                for line in output.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 2:
                            name = parts[0][:20]
                            display = parts[1][:35]
                            lines.append(f"| {name} | {display} |")
        elif self.system == "linux":
            code, output = await self._run_command(
                "systemctl list-units --type=service --state=running --no-pager --no-legend | head -20"
            )

            if code != 0:
                return "Error: systemd not available or permission denied."

            if output:
                lines.append("| Service | Status |")
                lines.append("|---------|--------|")

                for line in output.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4:
                        service = parts[0].replace(".service", "")
                        status = parts[3] if len(parts) > 3 else "running"
                        lines.append(f"| {service} | {status} |")
        else:
            # macOS - use launchctl
            code, output = await self._run_command(
                "launchctl list | head -20"
            )
            if code == 0 and output:
                lines.append("| PID | Status | Label |")
                lines.append("|-----|--------|-------|")

                for line in output.strip().split("\n")[1:]:  # Skip header
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        pid = parts[0] if parts[0] != "-" else "-"
                        status = parts[1]
                        label = parts[2][:40]
                        lines.append(f"| {pid} | {status} | {label} |")

        return "\n".join(lines)

    async def _ports(self) -> str:
        """List listening ports."""
        lines = ["**Listening Ports**\n"]

        if self.system == "windows":
            code, output = await self._run_powershell(
                "Get-NetTCPConnection -State Listen | "
                "Select-Object -First 20 | "
                "ForEach-Object { "
                "$proc = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName; "
                "Write-Output ('{0}|{1}' -f $_.LocalPort, $proc) "
                "}"
            )
            if code == 0 and output:
                lines.append("| Port | Process |")
                lines.append("|------|---------|")

                seen_ports = set()
                for line in output.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 2:
                            port = parts[0]
                            proc = parts[1] if parts[1] else "-"
                            if port not in seen_ports:
                                seen_ports.add(port)
                                lines.append(f"| {port} | {proc} |")

        elif self.system == "darwin":
            code, output = await self._run_command("lsof -iTCP -sTCP:LISTEN -P -n | tail -20")
            if code == 0 and output:
                lines.append("| Port | Process |")
                lines.append("|------|---------|")

                seen_ports = set()
                for line in output.strip().split("\n")[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 9:
                        process = parts[0]
                        port_info = parts[8]
                        if ":" in port_info:
                            port = port_info.split(":")[-1]
                            if port not in seen_ports:
                                seen_ports.add(port)
                                lines.append(f"| {port} | {process} |")
        else:
            code, output = await self._run_command("ss -tlnp 2>/dev/null | tail -20")
            if code != 0 or not output:
                code, output = await self._run_command("netstat -tlnp 2>/dev/null | grep LISTEN | head -20")

            if code == 0 and output:
                lines.append("| Port | Process |")
                lines.append("|------|---------|")

                seen_ports = set()
                for line in output.strip().split("\n")[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 4:
                        local = parts[3] if len(parts) > 3 else ""
                        if ":" in local:
                            port = local.split(":")[-1]
                            process = parts[-1] if "users" in line else "-"
                            if port not in seen_ports:
                                seen_ports.add(port)
                                lines.append(f"| {port} | {process[:30]} |")

        return "\n".join(lines)
