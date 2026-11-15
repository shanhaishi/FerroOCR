# -*- coding: utf-8 -*-
"""测峰值内存 + 大图耗时。"""
import ctypes
import ctypes.wintypes as wt
import json
import pathlib
import subprocess
import time

SPIKE = pathlib.Path(__file__).parent
EXE = SPIKE / "upstream/target/release/rusto.exe"
M = SPIKE / "models"
ARGS = ["--det-model", str(M / "ppocrv6-tiny_det.rten"),
        "--rec-model", str(M / "ppocrv6-tiny_rec.rten"),
        "--dict", str(M / "ppocrv6_dict.txt")]

PROCESS_MEMORY_COUNTERS = 2
class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]

def peak_mem(proc):
    """轮询进程工作集，返回峰值 MB。"""
    peak = 0
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    while proc.poll() is None:
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ok = k32.K32GetProcessMemoryInfo(wt.HANDLE(int(proc._handle)),
                                         ctypes.byref(pmc), pmc.cb)
        if ok:
            peak = max(peak, pmc.PeakWorkingSetSize)
        time.sleep(0.01)
    return round(peak / 1024 / 1024, 1)


def measure(img, label=None):
    t0 = time.perf_counter()
    p = subprocess.Popen([str(EXE), "ocr", str(img)] + ARGS,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mem = peak_mem(p)
    dt = time.perf_counter() - t0
    return {"image": label or img.name, "sec": round(dt, 3), "peak_rss_mb": mem}


rows = [measure(SPIKE / "upstream/models/test_images/example1.png"),
        measure(SPIKE / "zh_test.png")]

# 造大图：把 example1 放大到接近 4K 宽度
try:
    from PIL import Image
    src = Image.open(SPIKE / "upstream/models/test_images/example1.png")
    for w, tag in [(2400, "big_2400"), (3600, "big_3600")]:
        h = int(src.height * w / src.width)
        big = src.resize((w, h), Image.LANCZOS)
        p = SPIKE / (tag + ".png")
        big.save(p)
        rows.append(measure(p, tag))
        p.unlink()
except Exception as e:
    rows.append({"error_large": str(e)})

(SPIKE / "bench_mem.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
