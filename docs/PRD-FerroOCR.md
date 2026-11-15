# FerroOCR — 需求文档（PRD）

> **一个 Windows 上的「附属式」OCR 能力组件**：为各种程序/脚本提供统一、开箱即用的本地 OCR，单点调用，默认双击即用。

| 项 | 内容 |
|---|---|
| 文档版本 | **v1.3** |
| 日期 | 2026-11-11 |
| 状态 | Draft / 待确认（§13，剩余 6 条） |
| 本版变更 | ① 三条拍板落地：**代码签名降级为非阻塞**（§12.1 解释哪些分发路径会触发 SmartScreen）、**单包 60 MB 接受**、**不做截屏识别**（新增非目标 N11）；② 新增 **§12.1 SmartScreen 与代码签名** 完整对策（5 档方案 + 成本）；③ §13 从 11 条收敛到 6 条未决（Q2/Q3/Q9/Q10/Q11 已落地） |
| 决策记录 | [docs/adr/](adr/README.md) —— 五条关键决策的背景、备选方案与代价，比 PRD 更适合了解「为什么这么定」 |
| 上一版 | v1.2（单包 + 启动参数 + L2 默认开），已归档到 [archive/PRD-v1.2-单包与默认开HTTP.md](archive/PRD-v1.2-单包与默认开HTTP.md) |
| 更早 | v1.1 [archive/PRD-v1.1-四级接入阶梯.md](archive/PRD-v1.1-四级接入阶梯.md)、v1.0 [archive/PRD-v1.0-NamedPipe单通道.md](archive/PRD-v1.0-NamedPipe单通道.md)、v0.5 [archive/PRD-v0.5-服务化定位.md](archive/PRD-v0.5-服务化定位.md) |
| 上游基础 | [byrizki/rusto-rs](https://github.com/byrizki/rusto-rs)（MIT），锁定 rev `731c3f44a5a6`，包内版本 0.4.1 |
| 目标平台 | **Windows x86-64（唯一）** |
| 调研文档 | [rusto-rs 技术分析](../research/01-rusto-rs-技术分析.md)、[竞品与生态调研](../research/02-竞品与生态调研.md) |

---

## 0. 摘要

### 0.1 这是什么

**一个 exe，没有安装过程，没有配置步骤。** Python 里 `pip install ferroocr` 后一行 `ocr("图片路径")`；按键精灵、易语言、aardio 里复制 4–5 行 HTTP 代码直接调；不想写代码就双击 exe 或 `RunApp` 跑一次命令。**它要么自己常驻着待命，要么跑完就退，长时间没人用会自己退出**。不需要配端口、不需要开服务、不需要装 Python 环境或 CUDA——**无论哪种语言，都是同一个 exe**，功能差异只有一个来源：启动参数。

### 0.2 四条定位（本文档的最高约束）

业务方明确，**这四条冲突时优先满足前者**：

| # | 定位 | 对需求的直接约束 |
|---|---|---|
| **P1** | **附属工具，不是豪华产品**——让不同语言/工具都能有统一、方便、开箱即用的 OCR | 不追求功能大而全；**接口极简、零配置、零依赖**；宁可少 10 个功能，也不能让接入多写 10 行代码 |
| **P2** | **只考虑 Windows** | 砍掉 Linux/macOS 的全部适配工作；可以放心用 Windows 专有机制（Named Pipe、注册表、计划任务）；产物只需 `.exe` |
| **P3** | **产物尽可能少**（不牺牲性能与易用性） | 目标是**一个 exe 文件**（模型内置）；不做安装包、不做 Docker、不做多文件运行时 |
| **P4** | **可手动启动，但优先由其他语言/工具静默拉起** | 核心交互是 **SDK 自动拉起 + 单实例复用 + 空闲自退**；"双击运行"是次要路径 |

### 0.3 一句话架构

```
调用方程序 (按键精灵 / 易语言 / aardio / Python / C# / Go / Node / 批处理 …)
   │  四级接入阶梯，按自身技术栈选最省事的一级（§5.3.1）
   │  ├─ L2 本地 HTTP  ─→ POST 127.0.0.1:1224/ocr   ← 默认开启，老工具首选
   │  ├─ L0 一次性进程 ─→ ferroocr.exe ocr --image a.png --out res.json
   │  ├─ L1 命令+文件  ─→ ferroocr.exe ocr --in req.json --out res.json
   │  └─ L3 Named Pipe ─→ \\.\pipe\ferroocr-v1      ← SDK 默认，最快
   ▼
ferroocr.exe —— 唯一的发行物，一个包内含全部能力
   ├─ 双击 / SDK 拉起 / 任何方式启动，默认姿态：常驻 + HTTP 开 + 管道开
   ├─ 功能差异全由启动参数决定（--no-http / --port / --device / --idle …，§5.2.1）
   ├─ L0/L1 一次性模式跑完即退，无需常驻 —— 老工具的保底路径
   ├─ 内置模型，首次运行释放到 %LOCALAPPDATA%\FerroOCR\
   ├─ CPU: rusto-nn 纯 Rust SIMD ／ GPU: ONNX Runtime + DirectML（零安装，同一 exe 内）
   └─ 常驻时空闲 N 分钟自动退出，不占用资源
```

### 0.4 与上一版的根本差异

| 维度 | v1.2（上一版） | **v1.3（本版）** |
|---|---|---|
| 定位 | 附属式能力组件 | 不变 |
| 交付 | 一个 exe（CPU+GPU+模型） | 不变（体积上限 60 MB 已确认接受） |
| 接入 | 四级接入阶梯，L2 默认开 | 不变 |
| 截屏 | 未明确 | **明确不做**（N11）：只接受调用方传入的图片 |
| 签名 | 列为阻塞项 B2 | **非阻塞**（§12.1）：仅影响「浏览器下载」路径，U 盘/共享盘不触发 |
| 未决问题 | 11 条 | **6 条**（Q2/Q3/Q9/Q10/Q11 已落地） |
| 工期 | 7–8 人周 | **7–8 人周**（签名不占排期） |

---

## 1. 项目命名

> **FerroOCR**（中文「铁识」），可执行文件名 `ferroocr.exe`。

| 用途 | 名称 |
|---|---|
| 可执行文件 | **`ferroocr.exe`** —— **唯一的发行物**：CPU 与 GPU 两条引擎、模型、全部功能模块都在里面 |
| Named Pipe（L3） | `\\.\pipe\ferroocr-v1` |
| HTTP 端口（L2） | **默认开启**，优先 `127.0.0.1:1224`，占用则顺延；实际值写入 `endpoint.json` |
| 数据目录 | `%LOCALAPPDATA%\FerroOCR\`（模型、日志、锁文件） |
| 环境变量前缀 | `FERRO_*`（全部可选，用于覆盖默认行为） |
| SDK 包名 | `ferroocr`（PyPI，P0）/ `FerroOCR`（NuGet，P1）；老工具不用包，直接 HTTP |

**为什么保留这个名字**：`ferroocr` 在 crates.io / PyPI / npm / GitHub 四处**全部空闲**（2026-11-10 实测），`ferro-`（铁）与 Rust 的"氧化铁"意象同源，8 个字符易读易记。备选 `ferroglance` 同样空闲，但作为附属工具 `ferroocr` 的品类语义更直白。

> 因为**只做 Windows 且不发布到 crates.io**，命名冲突的风险进一步降低。

---

## 2. 背景：为什么需要一个"附属工具"

### 2.1 现状痛点（这是本项目的立项理由）

| # | 痛点 | 具体表现 |
|---|---|---|
| 1 | **每种语言都要自己集成一遍 OCR** | Python 有 PaddleOCR、C# 要调 Python 或找 NuGet 包、Go 几乎没有好用的、Node 只有几个不维护的绑定。**同一个能力，集成 5 次，5 套 API** |
| 2 | **Python 生态绑死** | PaddleOCR/RapidOCR 的事实标准是 Python。C#/Go/Rust 程序为了 OCR 要么内嵌 Python（几百 MB、部署灾难），要么调外部进程解析 stdout（脆弱、无并发、无错误码） |
| 3 | **部署负担重** | PaddleOCR：120 MB 二进制 + Python + 695 MB RSS；RapidOCR 需自行打包 onnxruntime；GPU 还要装 CUDA |
| 4 | **没有"标准调用姿势"** | 有的返回 `[[box, text, score]]`，有的返回 `{results:[{text,confidence,text_region}]}`，没有统一契约 |
| 5 | **服务化对我们太重** | 我们不需要 k8s、不需要多租户、不需要 Prometheus——只需要"**在我需要的时候，能识别一张图**" |

### 2.2 上游 `rusto-rs` 恰好提供了合适的底座

`rusto-rs` 是一个**纯 Rust 的 OCR 引擎**（不是服务）：826 个文件里**没有任何 HTTP/服务端代码**，只有库 + CLI + 多语言绑定。

| 已有能力 | 对我们意味着 |
|---|---|
| 纯 Rust 推理（`rusto-nn`）| **编译成一个 exe，无运行时依赖**，完美契合 P3 |
| 模型仅 5.4 MB（Indra 默认档） | **可以内置进 exe**，真正做到开箱即用 |
| CLI 二进制 2.1 MB | 最终 exe 体积可控（~10 MB 含模型） |
| 峰值内存 190 MB | 附属工具可以接受，空闲时退出即释放 |
| 检测+识别+方向+版面+阅读顺序+表格 | 功能足够，无需二次开发算法 |
| MIT 许可 | 可自由分发 |

**上游缺的、正好是本项目要做的**：①没有一个"能被别的程序方便调用"的常驻接口；②没有单实例复用；③没有 Windows 原生 IPC；④没有多语言 SDK。

### 2.3 关键约束（已核实，直接影响方案）

| # | 事实 | 影响 | 证据 |
|---|---|---|---|
| C1 | 引擎**不是 `Sync`**，`detect_text(&mut self)` | 必须自己做实例池；每实例约 190–250 MB | `engine.rs:79` `trait InferenceSession: Send` |
| C2 | 推理后端是**进程级全局单例** | 上游自身无法在同一进程内混合 CPU/GPU；**但 GPU 走 ORT（独立引擎），不碰这个单例** → 因此**一个 exe 可同时具备两条路径**，由运行时参数选择 | `rusto-nn/src/backend.rs:33` `static ACTIVE_BACKEND` |
| C3 | 上游 GPU 只加速 **GEMM**，`conv.rs`/`depthwise.rs` 零分派 | 自研 GPU 路线拿不到 2×，**必须换 ORT** | `kernels/gemm/mod.rs:38`；`conv.rs` 中 `active_backend` 出现 0 次 |
| C4 | 上游下载器**调外部 `curl`/`wget`** | 不能依赖它；**模型必须内置** | `src/download.rs:11,26,46` `Command::new` |
| C5 | 上游 release profile 是 `opt-level="z"` + `panic="abort"` | 必须覆盖为 `opt-level=3` + `unwind` | `Cargo.toml:117,121` |
| C6 | 上游在 crates.io 只到 **0.3.2**，仓库已到 0.4.1 且**没有任何 git tag**；`rusto-nn-cuda` **未发布** | 必须用 git 依赖并锁 rev（不能锁 tag） | crates.io API + `git tag` |
| C7 | ORT 的 `DirectML` EP **只需随包带 dylib，用户零安装** | GPU 引擎可做到"不装 CUDA"，且与 CPU 引擎共存于同一个 exe | `ort` 文档 `execution-providers.mdx:205` |
| C8 | ORT 的 EP **可链式注册并逐算子回退到 CPU** | 天然实现"有 GPU 用 GPU、没有就 CPU" | 同上 |

---

## 3. 目标与非目标

### 3.1 目标（v1.3）

| # | 目标 | 可度量结论 |
|---|---|---|
| G1 | **一行代码接入** | 任意支持语言，SDK 只需 `ocr(path)` 一个调用；**无需任何配置**（不配端口、不配路径、不启动服务） |
| G2 | **产物极简：只出一个包** | **唯一的发行物是一个 `ferroocr.exe`**（CPU + GPU 能力全在里面，含模型），≤ 60 MB，**无安装包、无外部运行时、无配置文件**；所有功能差异均由启动参数决定，不靠"多出几个包"来区分（§5.2.1） |
| G3 | **静默自动拉起** | 首次调用到返回结果 ≤ 3 s（含进程启动 + 模型加载）；**全程无窗口、无控制台、无 UAC 弹窗** |
| G4 | **单实例 + 空闲自退** | 多个客户端并发调用复用一个进程；空闲 5 分钟后自动退出，内存归零 |
| G5 | **识别质量不退化** | 与上游 CLI 输出的**逐字符一致率 100%**（同模型同参数） |
| G6 | **GPU 可选且零安装** | **同一个 `ferroocr.exe` 内含两套引擎**，`--device gpu` 启用 ONNX Runtime + DirectML；在 Win10/11 任意 DX12 显卡上可用，**不要求用户装 CUDA**；比 CPU 快 ≥ 2× |
| G7 | **SDK 只做真正需要的** | 首发 SDK 仅 **Python**（P0）与 **C#/Generic CLI 包装**（P1）；按键精灵 / 易语言 / aardio **不写 SDK**，改用 L2 HTTP 直接调（§5.5） |

### 3.2 非目标（明确不做）

| # | 非目标 | 理由 |
|---|---|---|
| N1 | ❌ **不做常驻服务** | 不需要运维、不需要 `systemctl`/`sc` 注册、不需要守护进程管理 |
| N2 | ⚠️ **不做网络服务** | 不做服务端、不监听 `0.0.0.0`、不接受远程连接。**L2 本地 HTTP 是"回环适配器"而非网络服务**：只绑 `127.0.0.1`，默认开启（v1.2 起），因为它是最多调用方用得上的接口（§5.3.1） |
| N3 | ❌ **不做异步任务队列** | 附属工具没有"跑通宵批处理"的场景；批量由调用方循环实现 |
| N4 | ❌ **不做多租户/鉴权/限流** | 本机单用户场景 |
| N5 | ❌ **不做 Prometheus/OpenTelemetry** | 只保留最小日志（可关） |
| N6 | ❌ **不做 Linux/macOS** | 按 P2 |
| N7 | ❌ **不做 Docker/K8s** | 与 P3 冲突 |
| N8 | ❌ **不做 GUI/托盘图标** | "附属"，不该出现在用户的桌面上 |
| N9 | ❌ **不做 PDF 栅格化** | v1 只接图片；PDF 由调用方自行转图（SDK 提供建议） |
| N10 | ❌ **不自研 OCR 算法** | 复用上游；只做封装与接入体验 |
| N11 | ❌ **不做截屏 / 屏幕区域识别**（Q11 已确认） | 只接受调用方传入的图片。截屏本来就该由调用方做：按键精灵有 `Plugin.Screen` / `GetCursorPos`，Python 有 `PIL.ImageGrab`，aardio 有 `mouse.screen`——它们比我们更清楚要截哪块屏。自己做还要处理多屏、DPI 缩放、权限，并且会把「被动工具」变成「主动监控进程」，扩大杀软误报面（R12） |

---

## 4. 用户场景

| 用户 | 场景 | 关键诉求 |
|---|---|---|
| **U1 后端开发者（Python）** | 在 FastAPI 服务里识别用户上传的发票 | `pip install ferroocr`，两行代码，不引入 OCR 依赖树 |
| **U2 桌面开发者（C#/WPF）** | 截图后识别文字，做本地搜索 | NuGet 一个包，**不能弹任何窗口**，不能要求装 Python |
| **U3 运维脚本（Go）** | 批量处理扫描件目录 | `go get`，静态单二进制，无 CGO 依赖 |
| **U4 Node 工具链** | 构建脚本里提取 PDF 截图文字 | npm 安装，同步调用 |
| **U5 手工试用的用户** | 双击 `ferroocr.exe` 打开图片试一下 | 双击能用，但**不是主要场景** |
| **U6 老旧脚本工具（按键精灵 / 易语言 / aardio / AutoHotkey / VBA）** | 游戏辅助、RPA、办公自动化脚本里识别屏幕文字 | **不能写 SDK、不能连管道**；只能用 `RunApp` 跑命令，或用各自自带的 HTTP 对象发 POST。**本项目的首要交付对象**（`aardio` 走 `inet.http`，`易语言` 走 `WinHttp.WinHttpRequest.5.1`，`按键精灵` 走 `MSXML2.XMLHTTP`） |

**最重要的调用链 A（现代语言，P4 的体现）**

```python
# U1 的全部代码 —— 没有启动服务、没有端口、没有配置文件
from ferroocr import ocr

result = ocr(r"C:\scans\invoice.png")
for line in result.lines:
    print(line.text, line.score)
```

第一次调用时：SDK 发现管道不存在 → 用 `CreateProcess(DETACHED_PROCESS)` 静默拉起 `ferroocr.exe --daemon` → 等待管道就绪（≤2 s）→ 发送请求。**用户全程无感。**

**最重要的调用链 B（老旧工具，U6，本版新增 P0）**

```basic
' 按键精灵的全部代码 —— 没有 SDK、没有管道、没有安装器
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "POST", "http://127.0.0.1:1224/ocr", False
http.setRequestHeader "Content-Type", "application/json"
http.Send "{""image_path"":""C:\shot.png""}"
If http.Status = 200 Then TracePrint http.ResponseText
```

```basic
' 若连"确保服务在跑"都嫌麻烦，用 L0 兜底：一个命令 + 一个文件
RunApp "C:\FerroOCR\ferroocr.exe ocr --image ""C:\shot.png"" --out ""C:\temp\r.json"""
Delay 3000
s = Plugin.File.ReadFileEx("C:\temp\r.json")
```


---

## 5. 产品需求

### 5.1 交付物形态（P3 核心）

| ID | 需求 | 验收 |
|---|---|---|
| F-1.1 | **单文件交付**：`ferroocr.exe` 一个文件即为全部（程序 + 默认模型 + 字典） | 把 exe 拷到干净 Windows 机器，双击或调用即可用；**无任何其他文件** |
| F-1.2 | **体积目标**：单包 ≤ **60 MB**（含 ORT + DirectML + 5.4 MB 模型）；另有 CPU-only 构建档 ≤ **15 MB** 供内部分发 | 实测 `dir` 输出 |
| F-1.3 | **只出一个包**：唯一发行物是 `ferroocr.exe`，GPU 能力（ORT + DirectML）编进同一个 exe。用 CPU 还是 GPU 由 `--device auto/cpu/gpu` 决定 | 用户只需下载一个文件；不存在「下错版本」 |
| F-1.3b | **GPU 运行时自解压**：`onnxruntime.dll` + `DirectML.dll` 内嵌进 exe，首次使用 GPU 时释放到 `%LOCALAPPDATA%\FerroOCR\runtime\<ver>\`，SHA-256 校验（同模型机制） | 单文件分发，无需用户装 ORT；CPU-only 机器永不释放这两个 DLL |
| F-1.4 | **无安装器**：绿色免安装，删除 exe 即卸载干净（仅 `%LOCALAPPDATA%\FerroOCR\` 缓存需手动清） | 卸载测试 |
| F-1.5 | **Windows 子系统 = `windows`**（非 console） | 双击**不弹黑框**；SDK 拉起时也没有窗口 |
| F-1.6 | **静态链接 CRT**（`+crt-static`） | 干净机器无需 VC++ Redistributable |
| F-1.7 | **提供 `--version`/`--selftest`** | 用于排查部署问题（唯一需要命令行的场景） |
| F-1.8 | **无参数即全功能**：不带任何参数双击，默认就是「常驻 + HTTP 开 + 管道开」 | 老工具拿到 exe 直接就能用，不必教它传参数 |

> **为什么模型内置**：若模型外置，用户拷了 exe 到别的机器就不可用，违反"开箱即用"；且上游下载器依赖外部 `curl`（C4），不可靠。**代价**是 exe 增大 5–6 MB、更新需重下整个 exe——对附属工具可接受。

### 5.2 启动与生命周期（P4 核心）

| ID | 需求 | 验收 |
|---|---|---|
| F-2.1 | **静默自动拉起**（L3/L2 模式）：SDK 检测到无服务时，以 `DETACHED_PROCESS \| CREATE_NO_WINDOW` 启动，**绝不弹窗** | 从无窗口程序调用，任务管理器可见进程但无窗口条目 |
| F-2.1b | **无进程也能用**（L0/L1 模式）：exe 支持一次性执行，跑完退出，不依赖常驻、不依赖单元实例 | 老工具用 `RunApp` 直接调，无需任何前置 |
| F-2.2 | **单实例**：常驻模式下系统级唯一（`Global\` 互斥量），第二个启动尝试自动转为"连接已有实例"；**一次性模式下允许并发多进程**（不抢互斥量） | 并发启动 10 次常驻，进程数仍为 1；L0 并发不受影响 |
| F-2.3 | **就绪握手**：客户端连接后先发 `ping`，服务返回 `ready`（含版本、能力、设备）后才发业务请求 | 冷启动首请求不超时、不返回半初始化状态 |
| F-2.4 | **空闲自退**：默认 300 秒无请求则退出（`--idle <sec>` / `FERRO_IDLE` 可调，`0` = 永不退出） | 空闲后进程消失，内存释放 |
| F-2.5 | **崩溃自愈**：SDK 检测到连接断开时，自动清理僵死状态并重启一次 | 手动 kill 进程后，下次调用自动恢复 |
| F-2.6 | **手动启动支持**：无参数双击即后台常驻；`--no-http` 只留管道；`--once` 跑完即退 | 各姿态与 SDK 互操作 |
| F-2.7 | **优雅退出**：收到关闭信号时，等待在飞请求完成（≤5 s）再退出 | 压测中关闭不丢请求 |
| F-2.8 | **退出即释放**：退出后无残留进程、无残留锁文件（崩溃遗留的锁需能被下次启动识别并清理） | 任务管理器 + 文件检查 |
| F-2.9 | **`--wait` 同步阻塞**：L0/L1 模式下若已有常驻实例，**优先转发给它**而不是自己再算一遍 | 避免 10 个进程各吃 350 MB |

#### 5.2.1 启动参数（v1.2 核心：一个包，功能靠参数区分）

**原则**：默认姿态是**全功能开启**，因为最大多数的调用方（老工具）不会传参数；需要收紧的人**显式关**。这样"只出一个包"与"老工具零配置"两件事不冲突。

| 参数 | 默认 | 作用 | 什么时候用 |
|---|---|---|---|
| *（无参数）* | 常驻 + L2 开 + L3 开 | 双击即用，HTTP 立刻可连 | **老工具分发的主路径** |
| `--no-http` | — | 关闭 L2，只留管道 | 不想占端口、只想给 SDK 用 |
| `--http` | 已默认开启 | 幂等开关（兼容旧脚本） | — |
| `--port <n>` | 1224，占用则顺延 | 指定端口 | 端口被占 / 要走固定端口 |
| `--bind <ip>` | `127.0.0.1` | **只允许回环**；传非回环值直接拒绝启动 | 安全底线，不做远程服务 |
| `--pipe <name>` | `ferroocr-v1` | 管道名 | 多套环境并行隔离 |
| `--idle <sec>` | 300 | 空闲自退；`0` = 永不退出 | 常驻 vs 用完即走 |
| `--device <auto\|cpu\|gpu>` | auto | 强制引擎（auto = 有 DX12 显卡走 GPU） | 复现问题、对比性能 |
| `--pool <n>` | `min(4, cores/2)` | 引擎实例数 | 内存敏感环境调小 |
| `--max-ram <mb>` | 2048 | 内存上限，超限返回 `BUSY` | 同上 |
| `--log <level>` | warn | 日志级别 | 排障 |
| `--wait` | — | 若已有实例在跑，把请求转发给它；否则自己算 | 防止多进程各吃一份内存 |
| `--once` | — | 处理完一个请求即退出（等价 L0 语义） | 批处理脚本 |
| `--selftest` / `--version` / `--help` | — | 诊断与自省 | 部署排查 |

**参数来源的优先级**（高 → 低）：命令行参数 > 环境变量 > `endpoint.json` 中的既有配置 > 内置默认。

同名环境变量：`FERRO_HTTP_PORT`、`FERRO_IDLE`、`FERRO_DEVICE`、`FERRO_POOL`、`FERRO_MAX_RAM`、`FERRO_LOG`、`FERRO_PIPE`。

**`FERRO_ARGS`**：SDK 拉起 exe 时会读取这个环境变量，把其中的参数原样附加到命令行。这样**调用方改不了 SDK，也能统一改服务行为**（例如设 `FERRO_ARGS=--no-http` 全局关掉端口）。

**`--help` 与 `GET /info` 必须如实回显当前生效的全部参数**（值 + 来源），用于排查"为什么端口不是 1224"这类问题。

### 5.3 通信协议

| ID | 需求 | 说明 |
|---|---|---|
| F-3.1 | **四级接入阶梯**：同一个 exe 同时支持 L0 一次性 CLI / L1 命令行+文件 / L2 本地 HTTP / L3 Named Pipe，**调用方按自身技术栈选最省事的一级** | 四级都能识别同一张图，输出 JSON 结构一致（详见 §5.3.1） |
| F-3.2 | **L0 · 一次性进程（保底，任何语言都能用）**：`ferroocr.exe ocr --image "C:\a.png" --out "C:\a.json"`，进程跑完即退出 | 老工具只需"运行命令 + 读文件"两步 |
| F-3.3 | **L1 · 命令行 + 结果文件**：请求写 `--in req.json`、结果写 `--out res.json`，**不依赖 stdout 重定向**（老工具拿不到子进程输出） | 按键精灵/易语言/AutoHotkey 等可用 |
| F-3.7 | **L2 · 本地 HTTP（老工具的"万能适配层"）**：`POST http://127.0.0.1:<port>/ocr`，JSON 进出。**默认开启**（`--no-http` 可关），只绑回环 | 按键精灵用 `MSXML2.XMLHTTP`、易语言用 `WinHttp.WinHttpRequest.5.1`、aardio 用 `inet.http` 即可调用 |
| F-3.8 | **L3 · Named Pipe（高性能，给现代语言）**：`\\.\pipe\ferroocr-v1`，NDJSON。**SDK 默认走这一级** | 无端口、无防火墙弹窗、延迟最低 |
| F-3.4 | **帧格式统一**：无论哪一级，**JSON 结构完全一致**（同一套 schema、同一套错误码） | 换级不用改业务代码 |
| F-3.5 | **路径优先**：请求默认传 `image_path`，**服务端直接读文件** | 老工具传路径比传 base64 容易得多 |
| F-3.6 | **内存图像支持**：可选 `image_base64`（截图/剪贴板等无文件场景） | 与 `image_path` 二选一 |
| F-3.9 | **大小限制**：单次请求 ≤ 32 MB（base64 解码后），像素 ≤ 10000×10000 | 超限返回 `IMAGE_TOO_LARGE` |
| F-3.10 | **端口策略（L2）**：优先 1224（对齐 Umi-OCR 的行业惯例），被占用则顺延；`--port` 可固定。实际端口写入 `%LOCALAPPDATA%\FerroOCR\endpoint.json`，调用方可读取 | 老工具不该被「猜端口」卡住 |
| F-3.11 | **超时**：客户端默认 60 s，服务端单请求默认 120 s | 大图在慢机器上留余量 |

#### 5.3.1 四级接入阶梯（本版核心修正）

| 级别 | 方式 | 延迟 | 需要常驻 | 适用调用方 | 举例 |
|---|---|---|---|---|---|
| **L0** | 一次性进程：`RunApp`/`system()`/`Process.Start` | ~1–2 s | ❌ | **任何语言，零依赖** | 按键精灵、批处理、shell |
| **L1** | 同上，但**请求/结果走文件** | ~1–2 s | ❌ | 拿不到子进程 stdout 的老工具 | 按键精灵、易语言、AHK、VBScript |
| **L2** ⭐ | 本地 HTTP `127.0.0.1`，**默认开启** | ~5–15 ms | ✅ | 有 HTTP 能力但无管道能力 | 按键精灵（MSXML2）、易语言（WinHttp）、aardio（inet.http）、PowerShell、curl |
| **L3** | Named Pipe | ~1–3 ms | ✅ | 有管道客户端能力的语言 | Python、C#（SDK 内部使用）；其他语言自建客户端亦可直连 |

**为什么必须保留 L0/L1**：「静默拉起常驻进程」这个设计假设调用方能**自己启动进程并连管道**。按键精灵之类的工具做不到这件事——它最多能 `RunApp` 一个命令然后等文件出现。**四级阶梯的 L0/L1 就是为这类调用方准备的兜底路径**，且不牺牲任何性能（本身就不需要常驻）。

**为什么 L2 HTTP 要重新加回来**：v1.0 把它列为"可选备用、默认关闭"，这是低估了它的实际价值。对老工具而言，**HTTP 是比管道容易一个数量级的接口**——`CreateObject("MSXML2.XMLHTTP")` 一行就能 POST，而 Named Pipe 在按键精灵里基本无解（要调 `CreateFile`/`ReadFile` 并处理 `OVERLAPPED`，远超脚本能力）。端口只绑 `127.0.0.1`，不改变「不暴露网络」的安全性质。

**为什么 L2 从「默认关闭」改为「默认开启」**：v1.1 关端口的理由是「不暴露网络」，但端口只绑回环时这个理由并不成立；反而让老工具必须被「教着传 `--http`」——而老工具的用户不会传参数。**默认开、只需一道回环限制**，比默认关、要求每个调用方都会传参更接近 P1。想关的人加 `--no-http` 即可。

**降级链设计**：SDK 按 L3 → L2 → L0 顺序尝试；纯命令行调用方直接用 L0/L1。**同一个 exe 全部支持，不需要装两个版本。**


**协议样例（四级通用，JSON 结构一致）**

```jsonc
// 请求体（L2 的 body / L1 的 --in 文件内容 / L3 的一行）
{"id":"1","cmd":"ocr","image_path":"C:\\scans\\invoice.png","lang":"ch","preset":"document","output":"json"}

// 响应体
{"id":"1","ok":true,"elapsed_ms":842,
 "image":{"width":2480,"height":3508},
 "lines":[{"text":"COFFEE SHOP","score":0.992,"polygon":[[95,30],[275,30],[275,55],[95,55]]}],
 "text":"COFFEE SHOP\nTotal $16.50"}

// 错误
{"id":"1","ok":false,"code":"IMAGE_NOT_FOUND","message":"路径不存在: C:\\scans\\x.png"}
```

**同一请求的四种调用形态**

```bat
:: L0 —— 一次性进程，结果直接写文件（最简单，任何语言都能用）
ferroocr.exe ocr --image "C:\scans\invoice.png" --lang ch --out "C:\temp\res.json"

:: L1 —— 请求/结果都走文件（老工具拿不到 stdout 时用这个）
ferroocr.exe ocr --in "C:\temp\req.json" --out "C:\temp\res.json"
```

```basic
' 按键精灵 / VBScript —— L2 本地 HTTP（推荐给老工具）
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "POST", "http://127.0.0.1:1224/ocr", False
http.setRequestHeader "Content-Type", "application/json"
http.Send "{""image_path"":""C:\scans\invoice.png"",""lang"":""ch""}"
text = http.ResponseText        ' 拿到 JSON 字符串
```

```basic
' 按键精灵 —— L0 兜底（连 HTTP 对象都用不了时）
RunApp "C:\FerroOCR\ferroocr.exe ocr --image ""C:\scans\invoice.png"" --out ""C:\temp\res.json"""
Delay 3000
txt = Plugin.File.ReadFileEx("C:\temp\res.json")
```

### 5.4 接口能力（极小集）

| ID | 需求 | 优先级 |
|---|---|---|
| F-4.1 | **核心命令 `ocr`**：输入图片路径/base64，输出行级文本+坐标+置信度 | P0 |
| F-4.2 | **输出粒度**：`lines`（默认）、`words`（词级坐标）、`text`（纯文本）、`tsv` | P0 |
| F-4.3 | **语言选择**：`ch`（中英，默认）/ `latin` / `ja` / `ko` / `world` 等上游支持的档位 | P0 |
| F-4.4 | **预设（preset）**：`plain`（只检测+识别，快）/ `document`（加版面+阅读顺序，默认）/ `scan`（加方向+去畸变） | P0 |
| F-4.5 | **阈值**：`score_threshold`（默认 0.5）、`box_threshold` | P0 |
| F-4.6 | **方向纠正**：自动（默认）处理 EXIF 旋转与 0/90/180/270 | P0 |
| F-4.7 | **`ping`/`info` 命令**：返回版本、上游版本、已加载模型、设备（CPU/GPU）、并发数 | P0 |
| F-4.8 | **`shutdown` 命令**：供调用方主动释放 | P1 |
| F-4.9 | 局部识别 `bbox`：只识别指定区域 | P1 |
| F-4.10 | 批量数组（一次请求多图）：**不做**，由客户端循环调用（保持协议与内存简单） | ✗ |

> **刻意不做**：异步任务、进度回调、流式输出、取消。**同步返回**是附属工具最合理的语义——调用方本来就是"等着一个结果"。

### 5.5 多语言 SDK（P1 核心）

| ID | 需求 | 验收 |
|---|---|---|
| F-5.1 | **Python（唯一 P0 SDK）**：PyPI `ferroocr`，`pip install ferroocr` 后 `from ferroocr import ocr` 可用；纯 Python 实现通信，**不编译扩展** | 同步接口 + 可选 `concurrent.futures` 友好形态 |
| F-5.2 | **C#/.NET（P1，跟进）**：NuGet `FerroOCR`，`netstandard2.0` + `net8.0` | WPF/WinForms/控制台均可用 |
| F-5.3 | **Node.js / Go / Rust（P2，按需再做）**：不列入首发；在此之前它们可以直接用 L2 HTTP 或 L0 | 首发不做，避免 5 套 SDK 的维护负担 |
| F-5.4 | **aardio 不写 SDK**：aardio 自带 `inet.http`，一行 `http.post()` 即可调 L2；文档给可粘贴片段 | `import inet.http` + `JSON.stringify` 共 4–5 行 |
| F-5.5 | **易语言不写 SDK**：用内置 COM 对象 `WinHttp.WinHttpRequest.5.1` 调 L2；不需要任何模块/支持库 | 5 行易语言代码可复制即用 |
| F-5.6 | **契约测试**：Python SDK 与「老工具片段」跑同一批 JSON 用例，输出必须一致 | CI 中同一用例集，结果逐字节一致 |
| F-5.7 | **SDK 不内含二进制**：SDK 只负责通信与自动拉起，exe 由用户放置或用 `ferroocr-sdk` 的 `ensure_binary()` 下载 | 避免 SDK 包体积膨胀 |
| F-5.8 | **老工具不提供 SDK**：按键精灵 / 易语言 / aardio / VBA / AutoHotkey 一律**用 L2（HTTP）或 L0/L1 直接调**，不写任何 SDK 或插件 DLL | ①这些生态维护 SDK 不划算；②要求装插件就违反 P1。文档给出**可直接粘贴**的片段即可 |
| F-5.9 | **每个语言 ≤ 2 个可复制示例**，放文档不放进包 | 降低维护面 |

> **SDK 的核心职责只有三件事**：①找到 exe（`FERRO_BIN` 环境变量 → 同目录 → `PATH` → `%LOCALAPPDATA%\FerroOCR\bin`）；②确保进程在跑（连接失败则拉起并等待就绪）；③把 JSON 编解码成该语言的原生对象。**每套 SDK 控制在 300 行以内。**

### 5.6 模型管理（简化）

| ID | 需求 | 验收 |
|---|---|---|
| F-6.1 | **默认模型内置**：Indra OCR（5.4 MB，OmniDocBench 45.70%）编入 exe | 首次运行无需下载 |
| F-6.2 | **首次运行释放**：把内置模型解压到 `%LOCALAPPDATA%\FerroOCR\models\<version>\`（约 200 ms） | 二次启动直接复用，无解压开销 |
| F-6.3 | **完整性校验**：释放时校验 SHA-256（内置哈希表），不匹配则重新释放 | 篡改模型文件后能自愈 |
| F-6.4 | **多语言包可选下载**：`ferroocr.exe --install-lang ja`（**进程内 HTTP，不依赖 curl**） | 无 curl 的机器上也能装语言包 |
| F-6.5 | **完全离线可用**：默认模型内置，断网也能用全部基础功能 | 断网测试 |
| F-6.6 | **不提供模型热切换**：换模型 = 换 exe 版本 | 简化 |

### 5.7 设备与 GPU

| ID | 需求 | 验收 |
|---|---|---|
| F-7.1 | **CPU 引擎零依赖**：纯 Rust SIMD（AVX2/AVX-512），无任何外部 DLL | 干净机器直接跑 |
| F-7.2 | **GPU 引擎走 ONNX Runtime + DirectML**：覆盖 **N 卡 / AMD / Intel 核显全部 DX12 显卡**，**用户零安装**；编进同一个 exe，`--device gpu` 启用 | 在无 NVIDIA 驱动的 AMD/Intel 机器上 GPU 能启用 DirectML |
| F-7.3 | **EP 自动回退**：`DirectML → CPU`；`--gpu-strict` 时失败即报错，不静默 | 无显卡机器上 `--device gpu` 自动用 CPU 且不报错 |
| F-7.4 | **加速目标**：A4 300DPI 整页，GPU ≤ CPU 的 1/2（即 ≥2×） | 基准报告 |
| F-7.5 | **`info` 如实报告**：真实使用的设备与 EP（不出现"以为在用 GPU 其实在 CPU"） | 对比 ETW/任务管理器 GPU 占用 |

> **为什么 GPU 走 ORT 而不是改造 rusto-nn**：上游 `DeviceBackend` 只暴露 `gemm()`，conv/depthwise 零分派（C3），而算力主要在 conv/depthwise。上游核显实测 **密集页慢 21%**（`PURE_RUST_MNN_PERFORMANCE.md:46-52`）。ORT 有完整 GPU kernel，且 DirectML 零安装（C7）。代价是 GPU 版 exe 从 15 MB 涨到 ~50 MB——但它是**可选版本**，不影响 CPU 版的 P3 目标。

### 5.8 并发与资源

| ID | 需求 | 验收 |
|---|---|---|
| F-8.1 | **并发处理**：服务端维护引擎实例池，默认 `min(4, cores/2)`；超出排队（有界，默认 16） | 8 个并发调用全部成功 |
| F-8.2 | **内存上限**：默认 `FERRO_MAX_RAM=2GB`，超限拒绝新请求并返回 `BUSY`，不 OOM | 压测无内存暴涨 |
| F-8.3 | **单请求隔离**：单个请求 panic 不影响其他请求（`catch_unwind`） | 注入坏图，服务不崩 |
| F-8.4 | **不抢 CPU**：空闲时不占 CPU；处理时线程数可配（默认 `cores-1`） | 空闲 CPU 占用 ~0% |
| F-8.5 | **进程优先级**：默认 `BELOW_NORMAL_PRIORITY_CLASS`，不干扰用户前台程序 | 任务管理器可见 |

### 5.9 日志与诊断（最小化）

| ID | 需求 |
|---|---|
| F-9.1 | **默认静默**：不产生任何控制台输出、不弹窗 |
| F-9.2 | **日志文件**：`%LOCALAPPDATA%\FerroOCR\logs\ferroocr.log`，滚动（最大 5 MB × 3）；默认只记 warn/error |
| F-9.3 | **`--verbose` / `FERRO_LOG=debug`** 提升日志级别 |
| F-9.4 | **`--selftest`**：自检（模型可读、管道可建、HTTP 端口可绑定、设备可用、权限正常），输出可复制的诊断文本 |
| F-9.5 | **崩溃转储**：panic 时写 `crash-<时间>.txt`（含版本、请求上下文） |

### 5.10 安全（附属工具的合理边界）

| ID | 需求 |
|---|---|
| F-10.1 | **L2 默认开启，但只绑 `127.0.0.1`**（绝不绑 `0.0.0.0`）；`--no-http` 可完全关闭；`--bind` 传非回环值直接拒绝启动 |
| F-10.1b | **L2 安全约束**：只接受本机回环、校验 `Origin`/`Host` 防 DNS rebinding；可选 `FERRO_HTTP_TOKEN` 简单口令 |
| F-10.2 | **管道 ACL**：仅当前用户可连接（`SECURITY_ATTRIBUTES` 限制），防止其他用户截获 |
| F-10.3 | **路径访问约束**：只读 `image_path` 指向的文件，不写任何用户目录（除自己的 `%LOCALAPPDATA%`） |
| F-10.4 | **无提权**：全程以当前用户权限运行，**不请求 UAC** |
| F-10.5 | **不联网**（除用户显式执行 `--install-lang`） |

---

## 6. 非功能需求

| 指标 | 目标 | 备注 |
|---|---|---|
| **exe 体积（单包，含 GPU）** | ≤ 60 MB | ORT ~15 MB + DirectML ~15 MB + 引擎 ~4 MB + 模型 5.5 MB + 压缩余量 |
| **exe 体积（CPU-only 构建档）** | ≤ 15 MB | 见下方体积预算，供内部分发或后续精简版 |
| **冷启动到就绪** | ≤ 2 s | 进程启动 + 模型加载（上游模型加载仅 0.45 ms，主要成本在进程与解压） |
| **首次调用端到端** | ≤ 3 s | 含拉起 + 首图识别 |
| **后续调用端到端（IPC 开销）** | ≤ 5 ms | 除识别本身外的通信成本 |
| **空闲内存** | 服务就绪后 ≤ 50 MB（未识别时） | 模型按需加载 |
| **识别时内存** | ≤ 350 MB（单并发，indra 档） | 上游 190–250 MB + 池冗余 |
| **空闲 CPU** | ~0% | 事件驱动，无轮询 |
| **并发能力** | 8 并发不排队失败 | 4 worker |
| **识别延迟（A4 300DPI，CPU）** | ~5 s（业务实测） | 上游 1600×993 为 678 ms，A4 面积大 5.5× |
| **识别延迟（A4 300DPI，GPU）** | ≤ 2.5 s | ≥2× 加速 |
| **精度** | 与上游 CLI 逐字符一致率 100% | 同模型同参数 |
| **长期稳定性** | 24 h 反复调用无内存增长、无句柄泄漏 | 关键：管道句柄必须正确释放 |

**体积预算（一个包，≤60 MB 是否够；CPU-only 编译档 ≤15 MB）**

CPU 部分（任何情况下都在包里）：

| 组件 | 估算 | 依据 |
|---|---|---|
| 引擎代码（`rusto-core` + `rusto-nn`，`opt-level=3`+LTO+strip） | **2–4 MB** | 上游 Linux 实测 CLI **2.1 MB**；Windows MSVC 略大 |
| Windows IPC/守护/CLI 代码（本项目新增） | **< 500 KB** | 只有管道、线程池、JSON |
| `serde_json` + 少量依赖 | ~500 KB | — |
| **内置模型**（Indra det+rec+dict） | **5.5 MB** | 上游 README 数据 |
| **CPU 合计** | **≈ 9–11 MB** | 15 MB 有余量 |

GPU 部分（同一 exe 内，`--device gpu` 才启用）：

| 组件 | 估算 | 说明 |
|---|---|---|
| ONNX Runtime 主 dylib（DirectML 构建） | ~25–35 MB | 内嵌为资源，**首次用 GPU 时**才释放到 `%LOCALAPPDATA%\FerroOCR\runtime\<ver>\` |
| DirectML.dll | ~5–8 MB | 同上；Win10 1809+ 系统自带一份可优先复用 |
| ONNX 模型（det+rec，与 CPU 用的同一套权重或 INT8 版） | ~5–10 MB | 可与 CPU 共用权重文件 |
| **GPU 增量** | **≈ 35–50 MB** | — |
| **单包合计** | **≈ 45–60 MB** | 落在 ≤60 MB 预算内 |

> **若嫌大，还有更小的档**：`ppocrv6-tiny` INT8 只有 **3.4 MB**，可把总包压到 **7–8 MB**，代价是精度从 45.70% 降到约 45.44%（同档，差距很小）。**建议首发用 Indra（精度优先），提供 `-tiny` 可选发行版**（Q3）。
>
> **注意**：模型是压缩存储还是原样嵌入会影响体积——`.mnn`/`.rten` 本身已是紧凑格式，压缩收益有限（约 10–20%），但首次运行要多花解压时间。建议**原样嵌入**（启动更快）。

---

## 7. 技术方案

### 7.1 工程结构

```
ferroocr/
├── crates/
│   ├── ferroocr-engine/     # 封装 rusto-core（唯一接触上游的地方）
│   ├── ferroocr-engine-ort/ # GPU 引擎：ORT + DirectML（`--device gpu` 时启用）
│   ├── ferroocr-pool/       # 引擎实例池
│   ├── ferroocr-ipc/        # L3 Named Pipe 服务端（帧编解码、多客户端）
│   ├── ferroocr-http/       # L2 最小 HTTP 服务端（std::net 手写）
│   ├── ferroocr-cli-run/    # L0/L1 一次性执行路径
│   ├── ferroocr-daemon/     # 生命周期：单实例、就绪、空闲退出
│   └── ferroocr-cli/        # 命令行入口 + 一次性模式
├── src/main.rs              # bin: ferroocr.exe（唯一发行物）
├── sdks/
│   ├── python/              # pip
│   ├── dotnet/              # nuget
└── models/                  # 构建期嵌入（build.rs include_bytes!）
```

### 7.2 关键技术决策

| 关注点 | 决策 | 理由 |
|---|---|---|
| **IPC（L3）** | Windows Named Pipe：`windows-sys 0.61` 直接调用 | 无端口/无防火墙/无网络暴露；延迟最低。给现代语言用 |
| **HTTP（L2）** | **`std::net` + 手写最小 HTTP**（只支持 POST + 定长 body） | 老工具的唯一现实接口。`tiny_http` 最新版仍是 **0.12.0（2022-10）**，4 年未更新，为它引入依赖不划算。手写约 150 行 |
| **一次性模式（L0/L1）** | 独立进程执行，结果写文件 | 老工具的保底路径；不依赖常驻、不依赖管道 |
| **帧协议** | JSON（L2/L3 为 NDJSON 一行一帧；L1 为单文件） | 人可读、易调试；**四级结构一致** |
| **异步/并行** | `crossbeam-channel 0.5`（引擎池借用）+ 原生线程 | 不用 tokio：OCR 是阻塞计算，async 无收益且体积大 |
| **图像传递** | 优先 `image_path`（服务端直接读） | 同机调用，省传输与 base64 膨胀；老工具传路径最容易 |
| **进程启动** | `CreateProcess` + `DETACHED_PROCESS \| CREATE_NO_WINDOW` | 彻底无窗口 |
| **单实例** | 命名互斥量 `Global\FerroOCR-v1`（失败则用 `Local\`）；**仅常驻模式加锁** | 一次性模式不加锁，允许并发 |
| **空闲退出** | 常驻模式下定时器，重置于每次请求 | 不常驻 |
| **模型分发** | `include_bytes!` 编入 exe，首运行释放到 `%LOCALAPPDATA%` | 单文件 + 免重复解压 |
| **并发模型** | 监听线程（L3）/ HTTP 线程（L2）+ 引擎池 | OCR 是阻塞计算，不用 async |
| **GPU** | `ort 2.0.0-rc.15` + `directml` feature（编进同一个 exe，`--device gpu` 启用） | 成熟 kernel + 零安装 |
| **构建 profile** | `opt-level=3`、`lto="fat"`、`strip=true`、`panic="unwind"`、`+crt-static` | 覆盖上游的 `"z"`/`abort`（C5） |
| **依赖锁定** | `git = "https://github.com/byrizki/rusto-rs", rev = "731c3f44a5a60b8b66211d287a48884f8af88c2b"` | **上游没有打任何 tag**，只能用 rev 锁定；crates.io 只到 0.3.2（C6） |

### 7.3 自动拉起时序

```
Client                          Daemon
  │                                │
  ├─ connect \\.\pipe\ferroocr-v1 ─┤ 找不到
  │  ← ERROR_FILE_NOT_FOUND       │
  ├─ CreateProcess(DETACHED) ────→ │ 启动
  │                                ├─ 抢互斥量（已存在则直接退出）
  │                                ├─ 释放模型（首用 GPU 时再释放 ORT dylib）到 %LOCALAPPDATA%
  │                                ├─ 建管道
  │  ← 轮询 connect（10ms 间隔）───┤
  ├─ connect 成功                  │
  ├─ {"cmd":"ping"} ─────────────→ │
  │  ← {"ok":true,"ready":true}    │
  ├─ {"cmd":"ocr",...} ──────────→ │
  │  ← {"lines":[...]}             │
  │                                ├─ 空闲 5 min → 退出
```

---

## 8. 里程碑

| 里程碑 | 内容 | DoD |
|---|---|---|
| **M0 · 骨架（3 天）** | workspace、`ferroocr-engine` 封装上游、`ferroocr.exe ocr --image x --out y`（L0）、**参数解析骨架（§5.2.1）** | 输出与上游 CLI 逐字符一致；`--help` 能列出全部参数 |
| **M1 · 常驻与传输（1.5 周）** | **L2 最小 HTTP（先做，老工具最先用得上）**、L0/L1 一次性 CLI、L3 Named Pipe、NDJSON 协议、ping/ocr/info、优雅退出 | 四种方式都能识别同一张图，JSON 一致；**双击 exe 后按键精灵 5 行脚本直接可用** |
| **M2 · 生命周期（3 天）** | 静默拉起、就绪握手、空闲自退、崩溃自愈、锁清理 | 从零调用 → 2 s 内就绪；空闲后进程消失 |
| **M3 · 模型内置与单包（3 天）** | `include_bytes!` + 首运行释放 + SHA-256 校验；**CPU 编译档体积 ≤15 MB**；ORT dylib 内嵌（首用 GPU 才落盘） | 单 exe 拷到干净机器可用；CPU 档 ≤15 MB，含 GPU 的单包 ≤60 MB |
| **M4 · Python SDK + 契约测试（1 周）** | 管道客户端（L3→L2→L0 降级链）、自动拉起、错误映射、契约用例集 | `pip install` 后一行代码可用 |
| **M5 · C# SDK 与示例打磨（1 周）** | C# NuGet 包；按键精灵 / 易语言 / aardio / PowerShell 四组可复制片段并实测 | 每种语言示例 ≤5 行且实测通过 |
| **M6 · GPU 接入同一 exe（1 周）** | ORT + DirectML 编入同一 exe、dylib 内嵌与首用释放、`--device` 切换、EP 回退、`info` 如实报告、基准报告 | 无 CUDA 的机器上 `ferroocr.exe --device gpu` 启用 DirectML 且 ≥2×；同一 exe 用 `--device cpu` 结果与 CPU 档一致 |
| **M7 · 打磨（3 天）** | selftest、日志、崩溃转储、README、四语言示例（Python/C#/按键精灵风格/易语言风格） | 手递验收 |

**总计约 7–8 人周**（1 名熟悉 Rust + Windows 的工程师）。C# SDK 若外包给 .NET 开发者，可并行压缩到 6–7 周。

---

## 9. 验收标准

### 9.1 功能验收

| # | 用例 | 通过条件 |
|---|---|---|
| A1 | 干净 Win10/11 机器，只放 `ferroocr.exe` | 调用成功，**无任何缺失 DLL 报错** |
| A2 | Python `ocr(r"C:\a.png")` | 返回 `lines`，无窗口弹出，无控制台 |
| A3 | 程序里连续调用 100 次 | 进程数始终为 1；总耗时约 100 × 单次 |
| A4 | 调用后等 5 分钟 | 进程自动消失；再次调用又能拉起 |
| A5 | 手动 `taskkill` 进程后立即调用 | SDK 自动重启并成功返回 |
| A6 | 同时启动 10 个客户端 | 只有 1 个 daemon；10 个请求全部成功 |
| A7 | 传不存在的路径 | 返回 `IMAGE_NOT_FOUND`，服务不崩 |
| A8 | 传 50 MB 图 | 返回 `IMAGE_TOO_LARGE`，服务不崩 |
| A9 | 传损坏的 PNG | 返回 `DECODE_FAILED`，后续请求正常 |
| A10 | `ferroocr.exe --selftest` | 输出全绿诊断报告 |
| A11 | 双击 exe（无参数） | **不弹黑框**；给出用法提示（写日志或显示简短提示） |
| A12 | Python SDK 与老工具片段跑同一契约用例 | 输出 JSON 完全一致 |
| A13 | 同一个 `ferroocr.exe` 加 `--device gpu` 在 AMD 显卡机器 | 启用 DirectML 并有加速；`GET /info` 如实显示 `directml` |
| A14 | 同一个 `ferroocr.exe --device gpu` 在无独显机器 | 自动回退 CPU，不报错 |
| A15 | **按键精灵用 L2 HTTP 调**（附可复制的 5 行脚本） | 返回 JSON，无需 SDK、无需管道、无需安装 |
| A16 | **按键精灵用 L0 `RunApp` 调** | 结果文件生成，进程自动退出，无残留 |
| A17 | L2 端口 1224 被占用 | 自动顺延到 1225/1226…，`endpoint.json` 写入实际端口 |
| A18 | `--no-http` 启动后访问 `127.0.0.1:1224` | 连接被拒（端口确实关闭） |
| A19 | **只放一个 exe 到干净机器，双击（不带参数）** | HTTP 立即可连，按键精灵 5 行脚本直接跑通——**不需要教用户传任何参数** |
| A20 | 同一个 exe：`--device cpu` 与 `--device gpu` | 两次结果逐字符一致；`info` 如实报告设备；GPU 机器上后者更快 |
| A21 | `--bind 0.0.0.0` | **拒绝启动并说明原因**（不做远程服务） |
| A22 | 易语言用 `WinHttp.WinHttpRequest.5.1`、aardio 用 `inet.http` 各调一次 | 均返回正确 JSON，示例可直接粘贴 |

### 9.2 性能验收

| # | 指标 | 通过条件 |
|---|---|---|
| P1 | exe 体积（单包） | ≤ 60 MB |
| P1b | exe 体积（CPU-only 档） | ≤ 15 MB |
| P2 | 冷启动到就绪 | ≤ 2 s |
| P3 | 首次调用端到端 | ≤ 3 s（含拉起） |
| P4 | IPC 往返开销（L3） | ≤ 5 ms（echo 测试） |
| P4b | HTTP 往返开销（L2，回环） | ≤ 15 ms（echo 测试） |
| P4c | 一次性进程开销（L0/L1） | ≤ 2 s（含进程启动 + 模型加载，模型已释放到磁盘） |
| P5 | 空闲内存 | ≤ 50 MB |
| P6 | 单并发识别内存 | ≤ 350 MB |
| P7 | 空闲 CPU | ≤ 1% |
| P8 | 24 h 反复调用 | 内存增长 < 5%，句柄数不增长 |
| P9 | GPU 加速比 | ≥ 2×（A4 300DPI） |

---

## 10. 风险

| ID | 风险 | 影响 | 概率 | 对策 |
|---|---|---|---|---|
| R1 | **exe 体积压不到 15 MB** | 中（P3 目标未达） | 中 | 优先 `opt-level="z"` + LTO + `strip`；模型用 INT8 档（3.4 MB）；实在不行放宽到 20 MB——**体积是"尽可能少"，不是硬指标** |
| R2 | **SmartScreen 拦截下载的 exe** | 中（仅限「从网络下载」这条分发路径） | 中 | **不是所有 exe 都会弹**：只有带 Mark of the Web 的文件才触发。U 盘拷贝、局域网共享、由别的程序生成/释放出来的 exe 都不带标记 → 过往经验里没遇到是正常的。完整对策见 §12.1，**已降级为非阻塞** |
| R3 | **Named Pipe 权限问题**（多用户/服务账户场景） | 中 | 中 | 管道 ACL 用当前用户 SID；服务账户场景提供 `Local\` 回退；**实在不行让调用方退到 L2/L0** |
| R4 | **首次调用 3 s 内失败**（慢机器/杀软扫描） | 中 | 中 | 拉起重试 + 可配超时（`FERRO_START_TIMEOUT`）；提供 `--selftest` 定位 |
| R5 | **模型内置导致更新成本高** | 低 | 高 | 接受；文档说明"换模型=换 exe" |
| R6 | **上游 crates.io 落后 / 未发布 cuda crate**（C6） | 中 | 确定 | 锁 git tag；适配层隔离 |
| R7 | **GPU 运行时增加体积与首次释放开销** | 低 | 中 | ORT dylib 内嵌但不落盘，**首用 GPU 时才释放**到 `runtime\<ver>\`（F-1.3b）；纯 CPU 用户永不触发 |
| R8 | **SDK 维护成本** | 低 | 低 | 只做 Python（P0）+ C#（P1），各 <300 行；老工具走 HTTP 片段，**不维护 SDK** |
| R9 | **上游 `panic="abort"` 导致整进程崩溃**（C5） | 高 | 确定 | 本项目 profile 覆盖为 `unwind` |
| R10 | **引擎非 `Sync` 导致池内存高**（C1） | 中 | 高 | 默认池大小 2（而非 4）；`FERRO_POOL_SIZE` 可调；空闲退出释放 |
| R11 | **`Global\` 互斥量在无权限时失败**（某些受限环境/远程桌面） | 中 | 中 | 先试 `Global\FerroOCR-v1`，失败退 `Local\FerroOCR-v1`；两者都失败则退化为"无单实例保护"并记 warn |
| R12 | **杀软误报**（自解压模型到 `%LOCALAPPDATA%` + 后台常驻，行为模式类似恶意软件） | **高**（用户无法使用） | **中高** | ①代码签名（R2 同一证书）；②向主流杀软提交白名单；③提供"不解压"模式（直接从 exe 内存映射读模型）；④文档说明行为 |
| R13 | **Named Pipe 在 WSL/容器内不可用** | 低 | 中 | 明确不支持，文档说明；仅支持原生 Windows 进程调用 |
| R14 | **依赖 `windows-sys` 0.61 的 API 稳定性** | 低 | 低 | pin 版本；封装在 `ferroocr-ipc` 单 crate 内，便于替换 |

---

## 11. 与上游的复用边界

| 能力 | 来自上游 | 我们做什么 |
|---|---|---|
| DBNet 检测 / CTC 识别 / 方向 / 去畸变 / 版面 / 阅读顺序 / 表格 | ✅ 直接用 | 只做参数映射与输出规范化 |
| 纯 Rust 推理运行时（`rusto-nn`） | ✅ 直接用 | 应用 `opt-level=3` profile 覆盖 |
| 模型文件 | ✅ 直接用（HuggingFace `byrizki/rusto-models`） | 构建期下载并 `include_bytes!` 嵌入 |
| CLI | ⚠️ 参考 | **不复用**：它是一次性的，无 IPC、无守护 |
| 模型下载器 | ❌ **不复用** | 它调外部 `curl`（C4），不可靠；自研进程内 HTTP |
| GPU 后端 | ❌ **不复用** | 只加速 GEMM（C3）；GPU 走 ORT 独立引擎，与 CPU 引擎共存于同一 exe |
| 多语言绑定（12 种） | ❌ **不复用** | 它们是 FFI 绑定（要分发 .dll），与"单 exe + IPC"路线相反 |

---

## 12. 开发前置条件（阻塞项）

| # | 事项 | 说明 |
|---|---|---|
| B1 | **Windows 开发/测试机** | 需要 Win10 与 Win11 各一台；GPU 路径还需一台有独显（N 或 AMD）的机器 |
| B2 | ~~代码签名证书~~ **已降级为非阻塞** | 见 §12.1：只有「从互联网下载」这一条分发路径会触发 SmartScreen；U 盘 / 共享盘分发不触发。不阻塞开发，发布前再决定 |
| B3 | **上游模型许可证确认** | Indra OCR 与 PaddleOCR 权重均 Apache-2.0（代码），权重再分发条款需确认——**模型内置进 exe 就属于再分发** |
| B4 | **crates.io 命名占用** | `ferroocr` 当前空闲（实测），发布前复查 |

### 12.1 SmartScreen 与代码签名：何时才会触发（Q2 已澄清）

**先把一件事说清楚**：Windows 拦不拦一个 exe，跟"谁开发的""有没有签名"不是第一位的，**第一位的是这个文件有没有 Mark of the Web（MOTW）**。

MOTW 是 NTFS 上的一个备用数据流 `Zone.Identifier`，内容形如：

```
[ZoneTransfer]
ZoneId=3
HostUrl=https://example.com/ferroocr.exe
```

- **浏览器下载、邮件附件、即时通讯接收** → 系统自动打上 `ZoneId=3`（Internet 区），运行前走 SmartScreen 检查；
- **U 盘拷贝、局域网共享、由别的程序 `File.Copy` 生成** → **不带标记**，直接运行，不弹任何提示；
- **FAT32 / exFAT 的 U 盘根本无法承载备用数据流** → 拷过去标记就没了（[来源](https://univik.com/blog/mark-of-the-web-network-share-onedrive-usb.html)）；
- 右键属性底部有「解除锁定」复选框 = 这个文件带着标记。

**这条差异能解释大多数"我发 exe 从来没被拦过"的经验**：如果 exe 一直是通过 U 盘、共享盘、网盘下载后解压、或由安装/释放程序生成的方式到别人机器上的，它就没走过"浏览器直接下载"这条路，自然不会触发提示。**问题不在 exe 本身，在分发路径。**

顺带一个常见误会：「未知发布者」和「Windows 已保护你的电脑」是**两种不同的提示**——前者是 UAC 的签名缺失提示（点"是"就过），后者才是 SmartScreen 拦截（要额外点"更多信息 → 仍要运行"）。前者可以靠自签名证书消掉，后者不能。

#### SmartScreen 到底看什么

微软官方文档写明，它评估**两个信号**（[SmartScreen 声誉文档](https://learn.microsoft.com/zh-cn/windows/apps/package-and-deploy/smartscreen-reputation)）：

1. **发布者声誉**——文件签名了吗？证书来自已知可信发布者吗？
2. **文件哈希声誉**——这个具体文件是否曾被其他用户下载且未表现恶意？

关键在于：**即使是新签名的二进制，在哈希/证书累积足够正面声誉之前，仍然会弹警告**。而**未签名文件每个新版本都要从零开始重新累积**，声誉不能跨版本转移——除非两个版本用的是同一个发布者身份签名。官方的表格也确认，OV/EV 证书**首次下载时同样告警**（标为"无法识别的应用"，但会显示已验证的发布者名）。

> ⚠️ **一个已经过时的流传说法**：早年"买 EV 证书可以立即跳过 SmartScreen"是有效的，**现在不行了**。微软文档明确写着 EV 证书不再绕过 SmartScreen。所以"花钱买 EV 就一劳永逸"这条路径已经不成立——花钱买到的只是"显示已验证的发布者名"而不是"不弹窗"。

#### 分层次的解决办法（按成本从 0 到高）

| 档 | 做法 | 成本 | 能解决什么 | 不能解决什么 |
|---|---|---|---|---|
| **0** | **改分发路径**：U 盘 / 内网共享盘 / 打包后由可信程序释放 | 0 | **完全绕开 SmartScreen**（文件不带 MOTW） | 需要控制分发渠道；走公网下载时无效 |
| **1** | **提供 SHA-256 + "右键解除锁定"一句话说明** | 0 | 用户知道文件没被篡改，且知道怎么点 | 提示本身还在，需要用户多点两下 |
| **2** | **自签名证书**（`New-SelfSignedCertificate` + `signtool`） | 0 | 消掉「未知发布者」UAC 提示；**内网可用 GPO 把根证书推到域内机器实现真正信任**；提供文件完整性 | **对公网 SmartScreen 完全无效**（微软不认自签根） |
| **3** | **OV 代码签名证书**（DigiCert/Sectigo/SSL.com 等） | 约 200–500 美元/年 + 硬件 token | 显示真实公司/个人名，声誉可跨版本累积 | 首次下载仍会弹窗，需靠下载量累积数周 |
| **4** | **Azure Trusted Signing**（原 Trusted Signing / Artifact Signing） | **约 10 美元/月** | 微软托管签名、每日短期证书、声誉绑定已验证身份；**目前最便宜的"正规签名"** | 组织需 3 年以上营业历史（个人身份也可申请，但需通过微软的身份验证）；中国大陆主体的可用性**需要实测确认** |
| **5** | **上架 Microsoft Store** | 一次性注册费 | **商店分发的应用由微软证书签名，永不触发 SmartScreen 下载警告** | 与 P3「绿色单 exe」直接冲突（要 MSIX 打包）——**本项目不采用** |

#### 落到 FerroOCR 上的建议

1. **开发期完全不需要证书。** 自己机器上跑、机器之间拷贝，都不会触发。
2. **分发优先走"档 0"**：面向熟人或内部小范围的场景，走 U 盘、网盘下载后打包成 zip、或让对方从共享盘直接拿。**这条路上零成本零提示**，也不需要任何证书。
3. **同时做"档 1"**：发布页给 SHA-256，附一句「若 Windows 提示，属性 → 勾选"解除锁定" → 确定」。这行字要写进 README。
4. **"档 2"是给企业客户的选项**：如果将来有客户要内网批量部署，给他一份自签名说明 + GPO 步骤，他自己把根证书推下去。这比买证书便宜得多。
5. **等有付费需求再考虑"档 3/4"。** 判断标准很简单：**一旦需要通过公网让陌生人下载这个 exe，就必须处理**；在那之前它不是阻塞项。

**因此 PRD 中 R2 从"阻塞项"降级为"非阻塞项"，B2 从"开发前置条件"降级为"发布前决策"。** 排期里不再为它留任何时间。

---

## 13. 待确认问题

| # | 问题 | 影响 | 建议默认 |
|---|---|---|---|
| Q1 | **首发 SDK 范围？** 已确认语言为 Python / 按键精灵 / 易语言 / aardio。建议**只做 Python 一个 SDK**，其余三种全走 L2 HTTP 与 L0 | 把 SDK 工作量从 2.5 周压到 1 周 | **是，只做 Python**；C# 视用户需要再补 |
| Q2 | **代码签名证书** —— 观察到「自行分发的 exe 从未触发过提示」 | 见 §12.1 的完整解释 | **已澄清，非阻塞**：只有「浏览器/邮件下载」这条路会触发 SmartScreen；U 盘、共享盘分发不触发。发布前再决定，开发排期不为它留时间 |
| Q3 | ~~单包体积上限~~ **已确认：60 MB 可接受**（放弃更小方案的体积评估见 §6） | 决定了 ORT dylib 必须内嵌 | **已确认**。CPU-only 档 ≤15 MB 保留为内部分发选项 |
| Q4 | **GPU 是否与 CPU 同批发布？** 现在两种引擎在同一个 exe 内（`--device` 切换） | 影响 M6 排期 | **建议同批**——分包会破坏 P3「只出一个包」；若压缩工期可先让 `--device gpu` 报「暂不支持」 |
| Q5 | **语言场景**：中文为主还是多语种？ | 决定内置哪个模型档与是否内置语言包 | 中文+英文（`ch`），其他语言包按需下载 |
| Q6 | **是否需要"自动更新"机制**？ | 附属工具的常见诉求，但会引入后台网络行为 | v1 不做；文档指引手动替换 exe |
| Q7 | **典型图片规格**？分辨率/行数/是否扫描件 | 影响性能目标与预设默认值 | 以 A4 300DPI 扫描件为基准 |
| Q8 | **调用方程序类型**？主要是什么语言/框架 | 决定 SDK 优先级 | 待确认（**已知含按键精灵等老工具**，故 L0/L1/L2 已提为 P0） |
| Q9 | ~~老工具技术栈清单~~ **已确认**：按键精灵 PC 版、易语言、aardio（另加 Python） | 验收 A19/A22 已按此四项写死 | 已确认 |
| Q10 | ~~L2 默认开还是关~~ **已确认：默认开，只出一个包**，功能差异用启动参数（§5.2.1）表达 | 取消双包方案；验收 A19 已覆盖「双击即用」 | 已确认 |
| Q11 | ~~是否需要屏幕区域识别~~ **已确认：不需要**——「工具本身不需要有这种自发性截图识别的能力，只要接受外界传入的图片来识别即可」 | 该项移入非目标 N11 | **已确认不做**：只接受调用方传入的图片路径；截屏交给调用方，它们各自的 API 更合适 |

---

## 14. 附：典型集成代码（目标形态）

```python
# Python —— 全部代码
from ferroocr import ocr
r = ocr(r"C:\scans\invoice.png")
for line in r.lines:
    print(f"{line.text}  ({line.score:.2f})")
```

```csharp
// C# (.NET) —— 全部代码
using FerroOCR;
var result = Ocr.Extract(@"C:\scans\invoice.png");
foreach (var line in result.Lines)
    Console.WriteLine($"{line.Text} ({line.Score:F2})");
```

```basic
' 按键精灵 / VBScript —— L2 本地 HTTP（双击过 ferroocr.exe 即可，端口默认开着）
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "POST", "http://127.0.0.1:1224/ocr", False
http.setRequestHeader "Content-Type", "application/json"
http.Send "{""image_path"":""C:\scans\invoice.png""}"
If http.Status = 200 Then TracePrint http.ResponseText
```

```vb
' 易语言 —— L2 本地 HTTP（用系统自带 COM，不需要任何模块或支持库）
.局部变量 http, 对象
http.创建 ("WinHttp.WinHttpRequest.5.1")
http.方法 ("Open", "POST", "http://127.0.0.1:1224/ocr", 假)
http.方法 ("SetRequestHeader", "Content-Type", "application/json")
http.方法 ("Send", "{""image_path"":""C:\scans\invoice.png""}")
调试输出 (http.读属性 ("ResponseText"))
```

```aardio
// aardio —— L2 本地 HTTP（标准库自带，无需安装任何包）
import inet.http;
import JSON;
var http = inet.http();
var res = http.post("http://127.0.0.1:1224/ocr",
    JSON.stringify({ image_path = "C:\scans\invoice.png" }));
console.log(res);
http.close();
```

```bash
# 任何语言 —— L0 一次性进程：调用 exe 后读结果文件，无需常驻
ferroocr.exe ocr --image "C:\scans\invoice.png" --out "C:\temp\res.json"
```

> Node.js / Go / Rust 首发不做 SDK（§5.5 F-5.3），它们直接用上面 L2 的 JSON 或 L0 的命令行即可，不需要额外适配层。

> **这六段代码是本项目的验收标准**：如果接入任何一门语言需要超过 5 行代码或任何额外配置，说明设计错了。
>
> **后四段尤其重要**：按键精灵、易语言、aardio 都**不需要 SDK、不需要安装任何东西、不需要连管道**，只要 exe 已经跑起来了。**「5 行代码」的标准对老工具同样适用**——三段老工具代码各 4 行，L0 那段 1 行。若为某个老工具接入需要写超过 5 行，该改进的是 exe，不是让对方多写代码。
>
> **前提只有一条**：`ferroocr.exe` 已经被启动过（双击即可，或由任意程序执行一次）。端口默认开着，**不需要传任何参数**。

---

**文档结束** · v1.3 · 待评审
