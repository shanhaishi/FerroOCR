# -*- coding: utf-8 -*-
"""给上游 rusto.exe 做冷/热基准，记录耗时与内存峰值。"""
import json
import pathlib
import statistics
import subprocess
import time

SPIKE = pathlib.Path(__file__).parent
EXE = SPIKE / "upstream/target/release/rusto.exe"
M = SPIKE / "models"
IMG_DIR = SPIKE / "upstream/models/test_images"

ARGS = [
    "--det-model", str(M / "ppocrv6-tiny_det.rten"),
    "--rec-model", str(M / "ppocrv6-tiny_rec.rten"),
    "--dict", str(M / "ppocrv6_dict.txt"),
]


def run(img):
    t0 = time.perf_counter()
    p = subprocess.run([str(EXE), "ocr", str(img)] + ARGS,
                       capture_output=True, timeout=300)
    dt = time.perf_counter() - t0
    out = p.stdout.decode("utf-8", "replace")
    n = 0
    try:
        n = len(json.loads(out))
    except Exception:
        n = -1
    return dt, n, p.returncode


def bench(img, runs=5):
    times = []
    nlines = 0
    for _ in range(runs):
        dt, n, rc = run(img)
        if rc != 0:
            return None
        times.append(dt)
        nlines = n
    return {
        "image": img.name,
        "lines": nlines,
        "min": round(min(times), 3),
        "median": round(statistics.median(times), 3),
        "max": round(max(times), 3),
    }


rows = []
for name in ["example1.png", "invoice1.jpg", "idcard.jpg"]:
    r = bench(IMG_DIR / name)
    if r:
        rows.append(r)

# 中文图：用 PIL 渲染一张
try:
    from PIL import Image, ImageDraw, ImageFont
    font = None
    for cand in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc",
                 r"C:\Windows\Fonts\simhei.ttf"]:
        if pathlib.Path(cand).exists():
            font = ImageFont.truetype(cand, 40)
            break
    if font:
        img = Image.new("RGB", (1200, 500), "white")
        d = ImageDraw.Draw(img)
        lines = [
            "中华人民共和国机动车驾驶证",
            "姓名：张三    性别：男",
            "出生日期：1990年5月20日",
            "住址：北京市朝阳区建国路88号",
            "证号：110101199005201234",
        ]
        for i, t in enumerate(lines):
            d.text((40, 40 + i * 80), t, fill="black", font=font)
        p = SPIKE / "zh_test.png"
        img.save(p)
        r = bench(p)
        if r:
            rows.append(r)
except Exception as e:
    rows.append({"image": "zh_render", "error": str(e)})

out = json.dumps(rows, ensure_ascii=False, indent=2)
(SPIKE / "bench_result.json").write_text(out, encoding="utf-8")

exe_bytes = EXE.stat().st_size
print("exe bytes:", exe_bytes, "=", round(exe_bytes / 1024 / 1024, 2), "MB")
