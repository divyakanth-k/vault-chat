#!/usr/bin/env python3
"""Look at this machine and work out what it can actually run.

The number that decides everything on a CPU is memory bandwidth, not core
count or RAM size: generating a token means reading every weight the model
uses out of memory, so speed is roughly bandwidth divided by active bytes per
token. Rather than guess at that, this measures it — a short NumPy streaming
benchmark gets within range of what an inference runtime will see.

    ./hardware.py              # report and recommendations
    ./hardware.py --json       # machine-readable, used by install.py
    ./hardware.py --quick      # skip the bandwidth benchmark

Every probe degrades to None rather than raising, so an unknown platform
produces a thinner report instead of a crash.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

GB = 1024 ** 3
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _run(cmd: list[str], timeout: float = 6.0) -> str | None:
    """Run a probe, resolving the binary ourselves. System tools live in
    /usr/sbin and /sbin, which are not always on PATH — relying on PATH makes
    every probe fail silently and the whole report collapse to defaults."""
    exe = shutil.which(cmd[0])
    if exe is None:
        for prefix in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
            candidate = Path(prefix) / cmd[0]
            if candidate.exists():
                exe = str(candidate)
                break
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _sysctl_int(name: str) -> int | None:
    """Read a macOS sysctl through libc rather than by spawning the `sysctl`
    binary. Subprocesses can be unavailable — a restricted PATH, a sandbox, a
    locked-down host — and when they are, every probe fails at once and the
    report silently collapses to defaults."""
    if not IS_MAC:
        return None
    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        value = ctypes.c_uint64(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(
            name.encode(), ctypes.byref(value), ctypes.byref(size), None, ctypes.c_size_t(0)
        )
        return int(value.value) if rc == 0 and value.value else None
    except (OSError, AttributeError, ValueError):
        return None


def _sysctl_str(name: str) -> str | None:
    if not IS_MAC:
        return None
    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        size = ctypes.c_size_t(0)
        if libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None,
                             ctypes.c_size_t(0)) != 0 or not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctlbyname(name.encode(), buf, ctypes.byref(size), None,
                             ctypes.c_size_t(0)) != 0:
            return None
        return buf.value.decode("utf-8", "replace").strip() or None
    except (OSError, AttributeError, ValueError):
        return None


def _sysconf(name: str) -> int | None:
    """POSIX sysconf — works on both Linux and macOS, needs no subprocess,
    and cannot be defeated by a thin PATH."""
    try:
        value = os.sysconf(name)
        return value if value and value > 0 else None
    except (ValueError, OSError, AttributeError):
        return None


# ------------------------------------------------------------------ memory


def total_ram() -> int | None:
    if IS_WIN:
        return _win_memory()[0]

    # Portable first; the platform-specific paths are only fallbacks.
    pages, page_size = _sysconf("SC_PHYS_PAGES"), _sysconf("SC_PAGE_SIZE")
    if pages and page_size:
        return pages * page_size

    try:
        if IS_LINUX:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        elif IS_MAC:
            out = _run(["sysctl", "-n", "hw.memsize"])
            if out:
                return int(out.strip())
    except (OSError, ValueError, IndexError):
        pass
    return None


def available_ram() -> int | None:
    try:
        if IS_LINUX:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        elif IS_MAC:
            out = _run(["vm_stat"])
            if not out:
                return None
            page = 4096
            match = re.search(r"page size of (\d+) bytes", out)
            if match:
                page = int(match.group(1))
            free = 0
            for key in ("Pages free", "Pages inactive", "Pages speculative"):
                hit = re.search(rf"{key}:\s+(\d+)", out)
                if hit:
                    free += int(hit.group(1))
            return free * page
        elif IS_WIN:
            return _win_memory()[1]
    except (OSError, ValueError, IndexError):
        pass

    pages, page_size = _sysconf("SC_AVPHYS_PAGES"), _sysconf("SC_PAGE_SIZE")
    if pages and page_size:
        return pages * page_size
    return None


def _win_memory() -> tuple[int | None, int | None]:
    class Status(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = Status()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
    except (AttributeError, OSError):
        return None, None


# --------------------------------------------------------------------- cpu


def logical_cores() -> int:
    return os.cpu_count() or 1


def physical_cores() -> int | None:
    """Physical cores matter more than logical ones: hyper-threaded siblings
    share a memory path, and running threads on both usually makes CPU
    inference slower rather than faster."""
    try:
        if IS_LINUX:
            pairs = set()
            physical = core = None
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("physical id"):
                    physical = line.split(":")[1].strip()
                elif line.startswith("core id"):
                    core = line.split(":")[1].strip()
                elif not line.strip() and physical is not None and core is not None:
                    pairs.add((physical, core))
                    physical = core = None
            if physical is not None and core is not None:
                pairs.add((physical, core))
            if pairs:
                return len(pairs)
        elif IS_MAC:
            for key in ("hw.physicalcpu_max", "hw.physicalcpu"):
                value = _sysctl_int(key)
                if value:
                    return value
                out = _run(["sysctl", "-n", key])
                if out and out.strip().isdigit():
                    return int(out.strip())
        elif IS_WIN:
            out = _run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor | "
                 "Measure-Object -Property NumberOfCores -Sum).Sum"],
                timeout=20,
            )
            if out and out.strip().isdigit():
                return int(out.strip())
    except (OSError, ValueError, IndexError):
        pass
    return None


def cpu_name() -> str:
    try:
        if IS_LINUX:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        elif IS_MAC:
            brand = _sysctl_str("machdep.cpu.brand_string")
            if brand:
                return brand
            out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
            if out and out.strip():
                return out.strip()
        elif IS_WIN:
            name = os.environ.get("PROCESSOR_IDENTIFIER")
            if name:
                return name
    except OSError:
        pass
    # platform.processor() returns the useless "i386" on macOS and an empty
    # string on many Linux builds, so fall through to the architecture.
    guess = (platform.processor() or "").strip()
    return guess if guess and guess not in ("i386", "x86_64", "unknown") else platform.machine()


def cpu_features() -> list[str]:
    """Only the ones that change inference speed."""
    wanted = ("avx2", "avx512f", "avx_vnni", "amx_int8", "neon", "sve")
    found: list[str] = []
    try:
        if IS_LINUX:
            text = Path("/proc/cpuinfo").read_text().lower()
            found = [f for f in wanted if re.search(rf"\b{f}\b", text)]
            if "asimd" in text and "neon" not in found:
                found.append("neon")
        elif IS_MAC:
            if platform.machine() == "arm64":
                found = ["neon"]
            else:
                out = (_run(["sysctl", "-n", "machdep.cpu.features",
                             "machdep.cpu.leaf7_features"]) or "").lower()
                found = [f for f in wanted if f in out]
    except OSError:
        pass
    return found


# --------------------------------------------------------------------- gpu


def gpus() -> list[dict]:
    found: list[dict] = []

    out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits"])
    if out:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2 and parts[1].isdigit():
                found.append({"vendor": "nvidia", "name": parts[0],
                              "vram": int(parts[1]) * 1024 * 1024, "unified": False})

    if not found and shutil.which("rocm-smi"):
        out = _run(["rocm-smi", "--showproductname"])
        if out:
            for line in out.splitlines():
                if "Card series" in line or "Card Series" in line:
                    found.append({"vendor": "amd", "name": line.split(":")[-1].strip(),
                                  "vram": None, "unified": False})

    if IS_MAC and platform.machine() == "arm64":
        # Unified memory: the GPU shares system RAM, so "VRAM" is the same
        # pool the model already fits in.
        found.append({"vendor": "apple", "name": f"Apple {platform.machine()} GPU (Metal)",
                      "vram": total_ram(), "unified": True})

    return found


# --------------------------------------------------------------- bandwidth


def memory_bandwidth(seconds: float = 0.8) -> float | None:
    """Sustained read bandwidth in GB/s, measured by streaming an array that
    is far too large for cache. This is the single best predictor of how fast
    a model will generate on this machine."""
    try:
        import numpy as np
    except ImportError:
        return None

    try:
        size = 256 * 1024 * 1024 // 8  # 256 MiB of float64
        data = np.ones(size, dtype=np.float64)
        data.sum()  # warm the pages so we time reads, not first-touch faults

        best = 0.0
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            start = time.perf_counter()
            data.sum()
            elapsed = time.perf_counter() - start
            if elapsed > 0:
                best = max(best, data.nbytes / elapsed / 1e9)
        del data
        return round(best, 1) if best else None
    except (MemoryError, ValueError):
        return None


# ------------------------------------------------------------------ models


# Sizes are the real on-disk figures for common GGUF builds, not derived from
# parameter counts — vocabulary size makes the naive calculation wrong.
CATALOG = [
    {
        "key": "embed", "role": "embed", "label": "Nomic Embed Text",
        "params": 0.14, "active": 0.14,
        "ollama": "nomic-embed-text",
        "lmstudio": "text-embedding-nomic-embed-text-v1.5",
        "quants": {"Q8_0": 0.3},
    },
    {
        "key": "tiny", "role": "autocomplete", "label": "Qwen2.5-Coder 1.5B (base)",
        "params": 1.5, "active": 1.5,
        "ollama": "qwen2.5-coder:1.5b-base", "lmstudio": "qwen2.5-coder-1.5b",
        "quants": {"Q4_K_M": 1.1, "Q5_K_M": 1.3, "Q6_K": 1.5, "Q8_0": 1.9},
    },
    {
        "key": "mini", "role": "chat", "label": "Qwen2.5-Coder 3B Instruct",
        "params": 3.1, "active": 3.1,
        "ollama": "qwen2.5-coder:3b", "lmstudio": "qwen2.5-coder-3b-instruct",
        "quants": {"Q4_K_M": 1.9, "Q5_K_M": 2.2, "Q6_K": 2.5, "Q8_0": 3.3},
    },
    {
        "key": "small", "role": "chat", "label": "Qwen2.5-Coder 7B Instruct",
        "params": 7.6, "active": 7.6,
        "ollama": "qwen2.5-coder:7b", "lmstudio": "qwen2.5-coder-7b-instruct",
        "quants": {"Q4_K_M": 4.7, "Q5_K_M": 5.4, "Q6_K": 6.3, "Q8_0": 8.1},
    },
    {
        "key": "medium", "role": "chat", "label": "Qwen2.5-Coder 14B Instruct",
        "params": 14.8, "active": 14.8,
        "ollama": "qwen2.5-coder:14b", "lmstudio": "qwen2.5-coder-14b-instruct",
        "quants": {"Q4_K_M": 9.0, "Q5_K_M": 10.5, "Q6_K": 12.1, "Q8_0": 15.7},
    },
    {
        "key": "moe-small", "role": "chat", "label": "gpt-oss 20B (MoE)",
        "params": 20.9, "active": 3.6,
        "ollama": "gpt-oss:20b", "lmstudio": "openai/gpt-oss-20b",
        "quants": {"MXFP4": 12.1},
    },
    {
        "key": "moe-large", "role": "chat", "label": "Qwen3-Coder 30B-A3B (MoE)",
        "params": 30.5, "active": 3.3,
        "ollama": "qwen3-coder:30b", "lmstudio": "qwen3-coder-30b-a3b-instruct",
        "quants": {"Q4_K_M": 18.6, "Q5_K_M": 21.7, "Q6_K": 25.1, "Q8_0": 32.5},
    },
]

QUANT_ORDER = ["Q4_K_M", "MXFP4", "Q5_K_M", "Q6_K", "Q8_0"]
QUALITY_NOTE = {
    "Q4_K_M": "smallest sensible; mild quality loss",
    "MXFP4": "the format this model ships in",
    "Q5_K_M": "noticeably better than Q4 for ~15% more memory",
    "Q6_K": "very close to full precision",
    "Q8_0": "effectively lossless",
}

EFFICIENCY = 0.8  # inference runtimes reach roughly this share of stream bandwidth


def predict_tokens_per_second(model: dict, quant: str, bandwidth: float | None) -> float | None:
    if not bandwidth:
        return None
    size_gb = model["quants"][quant]
    active_gb = size_gb * (model["active"] / model["params"])
    if active_gb <= 0:
        return None
    return round(bandwidth / active_gb * EFFICIENCY, 1)


RESPONSIVE = 8.0  # tokens/sec below which conversation starts to feel like waiting


def _entry(model: dict, quant: str, bandwidth: float | None) -> dict:
    return {
        "key": model["key"], "label": model["label"], "quant": quant,
        "size_gb": model["quants"][quant],
        "tokens_per_second": predict_tokens_per_second(model, quant, bandwidth),
        "ollama": model["ollama"], "lmstudio": model["lmstudio"],
        "note": QUALITY_NOTE.get(quant, ""),
    }


def recommend(info: dict) -> dict:
    """Choose the largest model that both fits in memory and stays
    responsive, then spend leftover memory on precision rather than on more
    parameters — a 7B at Q6 beats a 14B at Q4 that runs half as fast."""
    ram_gb = (info.get("ram_total") or 8 * GB) / GB
    bandwidth = info.get("bandwidth_gbps")
    gpu = next((g for g in info.get("gpus", [])
                if g.get("vram") and not g.get("unified")), None)

    if gpu:
        # On a discrete GPU the weights live in VRAM and CPU bandwidth stops
        # being the constraint, so the speed estimates below do not apply.
        weight_budget = gpu["vram"] / GB * 0.85
        reserve = 0.0
        where = "gpu"
    else:
        reserve = max(2.5, ram_gb * 0.28)   # operating system, editor, browser
        weight_budget = max(ram_gb - reserve - 2.0, 0.8)  # 2 GB for the KV cache
        where = "ram"

    candidates = [
        _entry(model, quant, bandwidth)
        for model in CATALOG if model["role"] == "chat"
        for quant in QUANT_ORDER
        if quant in model["quants"] and model["quants"][quant] <= weight_budget
    ]

    if where == "gpu" or not bandwidth:
        ranked = sorted(candidates, key=lambda c: -c["size_gb"])
    else:
        responsive = [c for c in candidates if (c["tokens_per_second"] or 0) >= RESPONSIVE]
        if responsive:
            # Biggest model that still keeps up, with precision as the tiebreak.
            ranked = sorted(responsive, key=lambda c: (-c["size_gb"],
                                                       -(c["tokens_per_second"] or 0)))
        else:
            # Nothing here will feel quick, so optimise for the least bad
            # experience: fastest first, not largest.
            ranked = sorted(candidates, key=lambda c: -(c["tokens_per_second"] or 0))

    chat = ranked[0] if ranked else None
    if chat is None:
        # Very small machine. The autocomplete model is still a usable
        # assistant, and saying so beats reporting nothing.
        tiny_model = next(m for m in CATALOG if m["key"] == "tiny")
        chat = _entry(tiny_model, "Q4_K_M", bandwidth)
        chat["note"] = "only this fits — memory is very tight"

    tiny = next(m for m in CATALOG if m["key"] == "tiny")
    tiny_speed = predict_tokens_per_second(tiny, "Q4_K_M", bandwidth)
    # Autocomplete fires on nearly every keystroke, so it has to be instant.
    autocomplete = bool(gpu) or (tiny_speed is not None and tiny_speed >= 25)

    free_after = ram_gb - reserve - chat["size_gb"] if where == "ram" else weight_budget
    if free_after >= 16:
        ctx = 32768
    elif free_after >= 8:
        ctx = 16384
    elif free_after >= 3:
        ctx = 8192
    else:
        ctx = 4096

    cores = info.get("physical_cores") or max((info.get("logical_cores") or 2) // 2, 1)
    threads = max(cores - 1, 1) if cores > 4 else cores

    return {
        "placement": where,
        "budget_gb": round(weight_budget, 1),
        "reserved_gb": round(reserve, 1),
        "chat": chat,
        "embed": {
            "label": "Nomic Embed Text",
            "ollama": "nomic-embed-text",
            "lmstudio": "text-embedding-nomic-embed-text-v1.5",
            "size_gb": 0.3,
        },
        "autocomplete": autocomplete,
        "autocomplete_tokens_per_second": tiny_speed,
        "context_length": ctx,
        "threads": threads,
        "alternatives": [c for c in ranked[1:6] if c["key"] != chat["key"]][:4],
    }


# ------------------------------------------------------------------ report


def collect(quick: bool = False, disk_path: str | None = None) -> dict:
    info = {
        "platform": platform.system(),
        "release": platform.release(),
        "arch": platform.machine(),
        "cpu": cpu_name(),
        "logical_cores": logical_cores(),
        "physical_cores": physical_cores(),
        "features": cpu_features(),
        "ram_total": total_ram(),
        "ram_available": available_ram(),
        "gpus": gpus(),
        "bandwidth_gbps": None if quick else memory_bandwidth(),
    }
    try:
        usage = shutil.disk_usage(disk_path or str(Path.home()))
        info["disk_free"] = usage.free
        info["disk_total"] = usage.total
    except OSError:
        info["disk_free"] = info["disk_total"] = None
    info["recommendation"] = recommend(info)
    return info


def human(n: int | None, unit: str = "GB") -> str:
    if not n:
        return "unknown"
    return f"{n / GB:.1f} {unit}"


def print_report(info: dict) -> None:
    rec = info["recommendation"]
    print(f"\n  {info['platform']} {info['release']} · {info['arch']}")
    print(f"  {info['cpu']}")

    cores = info["physical_cores"]
    core_text = f"{cores} physical" if cores else "unknown physical"
    print(f"  cores        {core_text} / {info['logical_cores']} logical")
    if info["features"]:
        print(f"  features     {', '.join(info['features'])}")
    print(f"  memory       {human(info['ram_total'])} total, "
          f"{human(info['ram_available'])} free")
    print(f"  disk         {human(info['disk_free'])} free")

    if info["gpus"]:
        for gpu in info["gpus"]:
            vram = human(gpu["vram"]) if gpu["vram"] else "unknown"
            tag = " (unified with system memory)" if gpu.get("unified") else ""
            print(f"  gpu          {gpu['name']} · {vram}{tag}")
    else:
        print("  gpu          none detected — CPU inference")

    if info["bandwidth_gbps"]:
        print(f"  bandwidth    {info['bandwidth_gbps']} GB/s measured")
    else:
        print("  bandwidth    not measured (needs numpy, or --quick was used)")

    print("\n  Recommended setup")
    chat = rec["chat"]
    if not chat:
        print("    Nothing in the catalog fits this machine's memory budget of "
              f"{rec['budget_gb']} GB. A 1.5B model will still run.")
        return

    speed = chat["tokens_per_second"]
    speed_text = f"~{speed} tok/s" if speed else "speed unknown"
    print(f"    chat model   {chat['label']} at {chat['quant']}")
    print(f"                 {chat['size_gb']} GB · {speed_text} · {chat['note']}")
    print(f"    embeddings   {rec['embed']['label']} · {rec['embed']['size_gb']} GB")
    print(f"    context      {rec['context_length']} tokens")
    print(f"    threads      {rec['threads']}")

    if rec["autocomplete"]:
        print("    autocomplete worth enabling (the 1.5B model should feel instant)")
    else:
        tiny = rec["autocomplete_tokens_per_second"]
        detail = f" (~{tiny} tok/s is too slow)" if tiny else ""
        print(f"    autocomplete leave it off{detail}")

    if rec["placement"] == "gpu":
        print("\n    A GPU was found, so the model will be offloaded to it and the"
              "\n    CPU bandwidth estimate above does not apply — expect far more.")

    if rec["alternatives"]:
        print("\n  Also fits")
        for alt in rec["alternatives"]:
            speed = alt["tokens_per_second"]
            print(f"    {alt['label']:<34} {alt['quant']:<7} {alt['size_gb']:>5} GB"
                  f"  {('~' + str(speed) + ' tok/s') if speed else ''}")

    disk_needed = chat["size_gb"] + 0.3 + 1.1
    free = info.get("disk_free")
    if free and free / GB < disk_needed + 5:
        print(f"\n  Disk is tight: about {disk_needed:.1f} GB of models plus room to "
              "work, against\n  only " + human(free) + " free.")

    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--quick", action="store_true", help="skip the bandwidth benchmark")
    parser.add_argument("--disk", default=None, help="which path to check for free space")
    parser.add_argument("--save", default=None, metavar="PATH",
                        help="also write the JSON here, so a caller gets both the "
                             "report and the data from one benchmark run")
    args = parser.parse_args()

    info = collect(quick=args.quick, disk_path=args.disk)
    if args.save:
        Path(args.save).write_text(json.dumps(info, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print_report(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
