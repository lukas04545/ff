"""CPU and memory of the PC (or server) the bot runs on – for the control panel's System page.

Only the Python standard library – nothing to install: Linux reads /proc, Windows asks the system through ctypes
(GetSystemTimes, GlobalMemoryStatusEx, GetProcessMemoryInfo), other systems show what they can (cores, load).
CPU usage is the difference between two readings, so a sampler takes one every few seconds and keeps the last
minutes for the charts.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import time
from collections import deque
from pathlib import Path

SAMPLE_SECONDS = 5
HISTORY = 120                     # samples kept: 10 minutes at 5 s


# ------------------------------------------------------------ Reading the system (one implementation per OS)

def _linux_cpu_times() -> tuple[float, float] | None:
    """(idle, total) jiffies of all cores together."""
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [float(v) for v in fields]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)        # idle + iowait
    return idle, sum(values[:8])                                     # without guest (already counted in user)


def _linux_memory() -> tuple[int, int] | None:
    """(total, available) bytes."""
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024
        return info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _linux_rss(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _linux_uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


if sys.platform == "win32":                                          # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except OSError:
        _psapi = None

    def _ft(ft: wintypes.FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def _win_cpu_times() -> tuple[float, float] | None:
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return float(_ft(idle)), float(_ft(kernel) + _ft(user))       # kernel time includes idle time

    def _win_memory() -> tuple[int, int] | None:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not _k32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys), int(status.ullAvailPhys)

    def _win_rss(pid: int) -> int | None:
        if _psapi is None:
            return None
        handle = _k32.OpenProcess(0x1000 | 0x0010, False, pid)       # QUERY_LIMITED_INFORMATION | VM_READ
        if not handle:
            return None
        try:
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            if not _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize)
        finally:
            _k32.CloseHandle(handle)

    def _win_uptime() -> float | None:
        _k32.GetTickCount64.restype = ctypes.c_ulonglong
        return _k32.GetTickCount64() / 1000


def cpu_times() -> tuple[float, float] | None:
    if sys.platform.startswith("linux"):
        return _linux_cpu_times()
    if sys.platform == "win32":
        return _win_cpu_times()
    return None


def memory() -> tuple[int, int] | None:
    if sys.platform.startswith("linux"):
        return _linux_memory()
    if sys.platform == "win32":
        return _win_memory()
    return None


def process_memory(pid: int) -> int | None:
    """Resident memory of one process in bytes (None if unknown or gone)."""
    if sys.platform.startswith("linux"):
        return _linux_rss(pid)
    if sys.platform == "win32":
        return _win_rss(pid)
    return None


def uptime() -> float | None:
    if sys.platform.startswith("linux"):
        return _linux_uptime()
    if sys.platform == "win32":
        return _win_uptime()
    return None


# ------------------------------------------------------------ Sampler

class SystemMonitor:
    """Takes a reading every SAMPLE_SECONDS while the panel runs; keeps the last HISTORY for the charts."""

    def __init__(self) -> None:
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self._last_cpu: tuple[float, float] | None = None
        self._last_proc: tuple[float, float] | None = None         # (process cpu seconds, wall clock)
        self._task: asyncio.Task | None = None

    def sample(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        cpu = None
        times = cpu_times()
        if times is not None and self._last_cpu is not None:
            idle = times[0] - self._last_cpu[0]
            total = times[1] - self._last_cpu[1]
            if total > 0:
                cpu = round(max(0.0, min(100.0, 100 * (1 - idle / total))), 1)
        self._last_cpu = times
        t = os.times()
        used, wall = t.user + t.system, time.monotonic()
        bot_cpu = None
        if self._last_proc is not None and wall > self._last_proc[1]:
            bot_cpu = round(100 * (used - self._last_proc[0]) / (wall - self._last_proc[1]) / (os.cpu_count() or 1), 1)
        self._last_proc = (used, wall)
        mem = memory()
        point = {"time": int(now), "cpu": cpu, "bot_cpu": bot_cpu,
                 "mem_used": (mem[0] - mem[1]) if mem else None, "mem_total": mem[0] if mem else None,
                 "bot_mem": process_memory(os.getpid())}
        if cpu is not None or mem is not None:
            self.history.append(point)
        return point

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.sample()                                            # first reading: the base for the CPU delta
            self._task = asyncio.create_task(self._run(), name="system-monitor")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            try:
                self.sample()
            except Exception:                                        # never let a reading stop the panel
                pass

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def snapshot(self, processes: dict[str, int | None] | None = None, disk_path: str | Path = ".") -> dict:
        """Everything for the System page: the latest values, the history and the bot's processes."""
        latest = self.history[-1] if self.history else self.sample()
        try:
            disk = shutil.disk_usage(disk_path)
            disk_info = {"total": disk.total, "used": disk.used, "free": disk.free}
        except OSError:
            disk_info = None
        load = None
        if hasattr(os, "getloadavg"):
            try:
                load = [round(v, 2) for v in os.getloadavg()]
            except OSError:
                load = None
        procs = [{"name": name, "pid": pid, "memory": process_memory(pid) if pid else None}
                 for name, pid in (processes or {}).items()]
        return {"supported": cpu_times() is not None or memory() is not None,
                "os": f"{platform.system()} {platform.release()}".strip(), "machine": platform.machine(),
                "python": platform.python_version(), "cores": os.cpu_count(), "uptime": uptime(),
                "load": load, "disk": disk_info, "latest": latest, "history": list(self.history),
                "processes": procs, "interval": SAMPLE_SECONDS}
