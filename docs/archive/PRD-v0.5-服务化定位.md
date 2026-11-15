# FerroOCR · 需求文档（PRD）

> 一个基于 RustO!（`rusto-rs`）的**本地 OCR 服务**，支持 CPU 与 GPU 推理，零外部 C++ 依赖、单二进制交付。

| 项 | 内容 |
|---|---|
| 文档版本 | v0.1（草案，待评审） |
| 日期 | 2026-11-10 |
| 状态 | Draft / 待确认项见 §14 |
| 上游基线 | [byrizki/rusto-rs](https://github.com/byrizki/rusto-rs) v0.4.0（MIT） |
| 目标平台 | Linux x86-64（主）、Windows x86-64、macOS arm64（次） |
| 关联文档 | [rusto-rs 技术分析](../../research/01-rusto-rs-技术分析.md)、[竞品与生态调研](../../research/02-竞品与生态调研.md) |

---

## 0. 摘要（TL;DR）

上游 `rusto-rs` 已经把一个**精度可用的纯 Rust OCR 引擎**（检测 DBNet + 识别 CTC/LineNet + 方向/去畸变/版面/阅读顺序）做到了「单二进制 2.1 MB、零 C++ 依赖、OmniDocBench 精度 45.70%（优于 PaddleOCR Tiny 45.33%）」的水平。但它**只有库、CLI 和多语言绑定，没有任何服务端形态**（全仓库 826 个文件中不存在 server/http/axum/grpc 相关代码）。

因此本项目 **FerroOCR** 的定位是：**把 RustO! 的引擎能力包成一个生产可用的本地 OCR 服务**，核心增量是

1. **HTTP 服务化**：同步 / 异步任务 / 批量 / 流式，RESTful + OpenAPI；
2. **CPU 与 GPU 双路调度**：设备探测、后端选择、自动回退、多实例池化、显存/内存治理；
3. **模型资产管理**：内置模型档位、离线导入、热切换、完整性校验、版本锁定；
4. **可运维性**：结构化日志、Prometheus 指标、健康检查、限流背压、优雅退出；
5. **性能与精度门禁**：把上游 benchmark 固化成 CI 回归，GPU 加速必须"用数据证明有效"才允许默认开启。

> ⚠️ **三条必须先知道的事实（前置风险，已逐项核实）**
>
> 1. **GPU 只加速 GEMM 一层**：`DeviceBackend` trait 只有 `gemm()` 一个方法，`conv.rs`/`depthwise.rs` 中 `active_backend` 出现 **0 次**；而 PP-OCRv6 rec 与 Indra LineNet 的算力主要在 depthwise（上游 `OPTIMIZATION_RESULTS.md` 自承"rec 里零个 3×3 标准卷积"）。CUDA 后端每次 GEMM 都做 H2D/D2H 全量拷贝（仅在 `m*n*k ≥ 65536` 时触发），且后端是**进程级全局单例**。
>
>    **关于"GPU 到底快不快"——务必看清测试硬件**（这是最容易误判的一点）：
>
>    上游唯一的 GPU 实测（`PURE_RUST_MNN_PERFORMANCE.md:46-52`）用的是 **Intel Iris Xe 核显（集显，共享 DDR4 内存）**：
>    - `example1.png` 47 行：CPU 316.06 ms / WebGPU 319.09 ms（略慢）
>    - `chronam-ad-page.png` 162 行密集页：CPU 1.44 s / **WebGPU 1.75 s（慢 21%）**
>    - `idcard.jpg` 24 行：CPU 205.62 ms / WebGPU 199.18 ms（快 3.1%，全表最好）
>
>    上游自己也写明原因（`PURE_RUST_MNN_PERFORMANCE.md:88-93`）：**核显 FP32 只有 1.5–2.0 TFLOPs，而 8 线程 AVX-512 CPU 有 0.8–1.0 TFLOPs，算力差仅 1.5–2×**，抵不掉驱动往返开销（单次 15–40 μs vs 计算 0.2 μs）。
>
>    → **但这是集显的结论，不能外推到独显**：RTX 4090 是 **83 TFLOPs**（约 CPU 的 80–100 倍），上游原文也承认"discrete server GPU … 40x faster than CPU"。**全仓库没有任何 N 卡独显或 CUDA 的端到端实测**。
>    → **业务方经验（CPU >5 s、GPU 2–3 s，约 2× 加速）与集显数据并不矛盾，是硬件档次不同**。
>
>    → **更进一步**：既然"只加速 GEMM"是收益上不去的根因，与其改造上游内核，**不如改用 `ort`（ONNX Runtime）跑 PaddleOCR ONNX 模型**——ORT 有完整成熟的 GPU kernel（conv/depthwise/pool/norm 全覆盖），且其 **DirectML EP 在 Windows 上覆盖 N/AMD/Intel 全部显卡、用户零安装**（dylib 随包）。这是**推荐的 GPU 路线**，详见 §9.4b。详见 §9.4b、§9.5、§11.3。
> 2. **上游没有发布到 crates.io**：仓库已到 v0.4.0，但 `crates.io` 上 `rusto-rs` 最新只有 **0.3.2**（2026-10-25），`rusto-nn-cuda` **完全未发布**。→ v1.0 必须用 `git = "...", tag = "v0.4.0"` 或 vendor 方式依赖，并在 `ferroocr-core` 做**唯一适配层**隔离升级风险（§12-R2）。
> 3. **多个"后端口子"是空壳**：`Cargo.toml` 里 `metal`、`coreml`、`vulkan`、`opencl`、`nnapi`、`hiai`、`qnn` **全部是空 feature stub**，打开不会有任何效果。真实可用的只有 CPU(SIMD) / RTen / MNN-C++ / CUDA / WebGPU 五个。
> 4. **GPU 后端在根包里根本没接线**：`src/lib.rs` 与 `src/cli.rs` 中 `cuda`/`webgpu` 命中 **0 次**，CLI 也没有 `--device` 参数——打开 feature 只是把 crate 编译进来，**没有任何代码调用 `register_backend()`**。这意味着"GPU 支持"在本项目里是**从零开始接的活**，不是"打开开关"。
> 5. **浏览器 WebGPU 被硬编码禁用**：`crates/rusto-nn-webgpu/src/lib.rs:304-310` 中 `is_available()` 在 `wasm32` 下直接 `return false`（注释说明：单线程 WASM 里 `rx.recv()` 会死锁）。所以 WebGPU 只在 native 端可用，不能拿"浏览器也能跑 GPU"做卖点。

---

## 1. 项目命名

### 1.1 推荐名称

> **FerroOCR** —— 读音 /ˈfɛroʊ oʊ siː ɑːr/，可简称 **Ferro**。

**命名逻辑**

| 维度 | 说明 |
|---|---|
| 词源 | 拉丁语 *ferrum*（铁）。Rust 的官方意象是「铁的氧化物 / 铁锈」，`ferro-` 与 Rust 生态的隐喻同源，却比 `rust-` 前缀更有辨识度 |
| 含义 | 「铁」→ 坚固、本地、离线、可嵌入；「OCR」→ 一眼说清做什么 |
| 检索性 | 全小写 `ferroocr` 在 crates.io / PyPI / npm **均未被占用**，GitHub 同名仓库 0 个（2026-11-10 实测，见 §1.3） |
| 品牌延展 | `ferro` 可做前缀族：`ferroocr-core`、`ferroocr-server`、`ferroocr-client`、`ferroocr-bench` |
| 中文名 | **铁识**（取「铁」+「识文」，宣传语：*铁识 · 让文档在本地被读懂*） |

**双层命名（可选，推荐采用）**：包名/组织名走 `ferroocr`（技术标识、可检索、可注册），**对外产品名用 `FerroGlance`**。`-glance`（一瞥）传达"秒读"与"扫描"的意象，比 `-OCR` 后缀更适合做品牌；`ferroglance` 在 crates.io / PyPI / npm / GitHub 四处同样**全部空闲**（2026-11-10 实测）。若只用一个名字，就用 `ferroocr`——零解释成本，`OCR` 后缀自带品类语义。

### 1.2 命名规范（包 / 二进制 / 镜像）

| 用途 | 名称 | 说明 |
|---|---|---|
| 仓库 | `ferroocr` | 单一 monorepo |
| 核心库 crate | `ferroocr-core` | 封装 `rusto-rs`，对外稳定 API |
| 服务 crate | `ferroocr-server` | HTTP/任务调度/设备管理 |
| 客户端 crate | `ferroocr-client` | Rust SDK，供其他 Rust 服务内嵌 |
| 二进制 | `ferroocr` | 主二进制（`ferroocr serve` / `ferroocr ocr` / `ferroocr models`） |
| 基准工具 | `ferroocr-bench` | CI 精度/性能门禁 |
| Docker 镜像 | `ghcr.io/<org>/ferroocr:VERSION` | `-cuda` / `-cpu` 两个变体标签 |
| 环境变量前缀 | `FERRO_*` | 如 `FERRO_DEVICE`、`FERRO_MODEL_DIR` |
| 配置文件名 | `ferroocr.toml` | |

### 1.3 备选名与占用核查

| 候选 | 含义 | crates.io | PyPI | npm | GitHub 同名仓库 | 结论 |
|---|---|---|---|---|---|---|
| **ferroocr** | 铁 + OCR | 空闲 | 空闲 | 空闲 | 0 | ✅ **首选** |
| **ferroglance** | 铁 + 一瞥 | 空闲 | 空闲 | 空闲 | 0 | ✅ 品牌名候选（与 ferroocr 组成双层命名） |
| **oxidocr** | oxide（氧化物）+ OCR | 空闲 | 空闲 | 空闲 | 0 | ✅ 次选（更"化学"，但 `oxid` 家族在 Rust 中过密） |
| ferriscan | Ferris（Rust 吉祥物）+ scan | 空闲 | 空闲 | 空闲 | 2 | ⚠️ 偏"扫描仪"，弱化 OCR |
| ferrite | 铁素体 | **占用** | **占用** | **占用** | 多 | ❌ 冲突 |
| sidero | 希腊语"铁" | 空闲 | 空闲 | 空闲 | 130+（Sidero Labs / Talos 生态） | ❌ 商标与检索冲突 |
| diopsis | 希腊语"双视" | 空闲 | 空闲 | **占用** | — | ⚠️ npm 冲突 |
| radeco | 无实义 | 空闲 | 空闲 | 空闲 | 0 | ⚠️ 无记忆点 |

> 核查方式：`crates.io/api/v1/crates/<name>`、`pypi.org/pypi/<name>/json`、`registry.npmjs.org/<name>` 的 HTTP 状态码，以及 GitHub 仓库名检索。Docker Hub 与域名可用性**未验证**，正式立项前需补做商标检索（建议检索类别：第 9 类软件、第 42 类 SaaS）。

---

## 2. 背景与问题

### 2.1 现状：上游强在引擎，弱在"被使用"

`rusto-rs` v0.4.0 的能力盘点（证据见 §7 与配套技术分析）：

- ✅ **纯 Rust 推理运行时** `rusto-nn`：自研 MNN 图执行器 + SIMD 内核（AVX-512 / AVX2+FMA / NEON / WASM SIMD128），GEMM、conv、depthwise 全部手写；
- ✅ **完整文档理解管线**：DBNet 检测、CTC/LineNet 识别、180° 行方向分类、整页 0/90/180/270° 方向、UVDoc 3D 去畸变、PicoDet 版面（CDLA 10 类 / PubLayNet 5 类）、神经图阅读顺序、表格结构；
- ✅ **模型档位齐全**：Indra OCR（5.4 MB，默认）、PP-OCRv6 Tiny/Small/Medium（6/30/134 MB，含 INT8）、PP-OCRv5/v4 多语言包，按 README 声明托管于 HuggingFace `byrizki/rusto-models`（本次网络受限未能直连验证，M0 需实测确认），首次运行自动下载到 `~/.cache/rusto/models`；
- ✅ **极致轻量**：CLI 2.1 MB、`.so` 304 KB、WASM 238 KB（gzip）、峰值 RSS 168–248 MB（PaddleOCR 为 695 MB）；
- ✅ **精度可用**：OmniDocBench 1-NED 45.70%，与 PaddleOCR Tiny 45.33%、RapidOCR 44.84% 同档或更好。

- ❌ **没有服务端**：无 HTTP/gRPC/任务队列/并发治理/观测性；仓库里唯一的并发是 `rayon` 参与识别批处理，且 `pipeline.rs` 里把识别并行度硬编码为 `rayon::current_num_threads().min(total_chunks).min(2)`（即**最多 2 个 worker**）；
- ❌ **引擎不是 `Sync`**：`InferenceSession: Send`，`detect_text(&mut self)`——一个引擎实例同时只能跑一个请求，服务化必须自己做实例池；
- ❌ **设备选择是进程级全局**：`rusto-nn` 的加速后端是一个 `static RwLock<Option<Arc<dyn DeviceBackend>>>`，一个进程只能挂一种加速后端，**无法在同一实例内按请求在 CPU/GPU 之间切换**；
- ❌ **GPU 覆盖极窄**：`DeviceBackend` 只有 `gemm()`；CUDA 每次调用 `clone_htod`/`memcpy_dtoh` 全量搬运；WebGPU 仅有 tiled GEMM 的 WGSL shader，且 `m*n*k >= 65536` 才触发；conv/depthwise 完全没有 GPU 分派；
- ❌ **无批处理/队列语义**：CLI 只能靠 `xargs -P 4` 这种外部手段并发，没有超时、取消、背压、优先级、重试。

### 2.1b 竞品格局与差异化定位（2026-11-10 核实）

**直接竞品：`deepseek-ocr.rs`（2188★，Apache-2.0，Rust + Candle）** —— 这是 Rust 生态里唯一"Rust 做 OCR 服务"的对手，必须正面回答。

| 维度 | deepseek-ocr.rs | **FerroOCR** |
|---|---|---|
| 模型 | VLM 大模型：DeepSeek-OCR 6.3 GB（运行 ~13 GB）、PaddleOCR-VL 4.7 GB（~9 GB）、DotsOCR 9 GB（高分辨率 30–50 GB） | 轻量检测识别流水线：**5.4 MB 起**，全量内存 190–250 MB |
| 协议 | **OpenAI 兼容** `/v1/responses`、`/v1/chat/completions` | 结构化 OCR JSON（+ v1.1 兼容层） |
| 硬件 | Metal / CUDA(**alpha**) / MKL | CPU SIMD / WebGPU / CUDA |
| 定位 | **重档文档解析**（复杂版面、公式、手写） | **轻量高吞吐**（批量发票/工单/扫描件） |
| 冷启动 | 需加载 GB 级权重 | 模型加载 0.45–1 ms |

> **差异化回答**：两者可共存，不是替代关系。FerroOCR 打的是"没有 GPU、内存有限、要跑 10 万张"的场景；`deepseek-ocr.rs` 打的是"单张复杂文档要最高精度"。**但 PRD 必须回答"为什么不用 OpenAI 兼容协议"**：因为我们的输出是带坐标/版面/表格的结构化 OCR 结果，不是对话文本；强行套 chat 协议会丢失 `box`/`score`/`layout_regions`。v1.1 可提供 `/v1/compat/openai` 适配层供 Agent 调用。

**其他方案的实际短板（→ 我们的机会）**：

| 方案 | 形态 | 关键短板 |
|---|---|---|
| PaddleOCR HubServing / PaddleX | Python + Flask | 重依赖（120 MB 二进制 + Python 环境 + 695 MB RSS）；PaddleX 默认**只处理 10 页** |
| RapidOCR / RapidOCRAPI | pip 库 / FastAPI 薄封装 | API 层是另一独立小项目；**作者自认"没考虑并发"** |
| Umi-OCR | Windows 桌面 GUI + 附属 HTTP | HTTP 依附 GUI 进程；无 GPU、无队列、无 Linux 服务形态 |
| Tesseract | CLI | 精度低（41.14% vs 45.70%）；依赖 leptonica |
| MinerU / Surya / chandra | Python + GPU 优先 | 重、需 GPU、部署复杂 |

> **产品缝隙**：**「单二进制零依赖 + HTTP 服务 + 异步任务 + 完整可观测」四者兼具的，市面上基本没有**。这是核心竞争力所在，不应去拼精度（我们与 PaddleOCR 同档，且绝对延迟更慢）。

### 2.2 用户真正的诉求

| 诉求 | 上游现状 | 本项目的回答 |
|---|---|---|
| "我要在**内网/离线**环境部署一个 OCR 接口给业务系统调" | 需要自己写一层 HTTP 包 CLI，或绑定 Python | 单二进制 + Docker，`POST /v1/ocr` |
| "我的机器有 GPU，想快一点" | 开 `--features cuda` 后只在 GEMM 上生效，且可能有拷贝开销倒挂 | 显式设备策略 + 设备自适配；**N 卡独显上必须真正跑出 2–3× 收益**（见 §8.1 实测目标） |
| "**客户的机器不能装 CUDA**" | 需确认：驱动 vs Toolkit 的区别 | **只要求 NVIDIA 驱动，不要求 CUDA Toolkit**——`cudarc` 默认 `dynamic-loading`，运行时用 `libloading` 找 `nvcuda.dll`/`libcuda.so`+`cublas`，零编译期依赖、零额外安装（详见 §9.5） |
| "批量 5000 张发票/PDF 要跑通宵" | `xargs` 拼凑，失败不可重试，进度不可见 | 异步任务 + 有界队列 + 进度/取消 + 断点续跑 |
| "结果要能塞进检索系统（带坐标、版面、表格）" | CLI 有 JSON/TSV/spatial/markdown，但无服务端 schema 与版本契约 | 稳定 JSON Schema + OpenAPI，输出契约版本化 |
| "我不能用带 C++/OpenCV 的东西" | ✅ 上游已做到 | 保持纯 Rust 交付，CI 禁止引入 C++ 运行时依赖（除可选 GPU 驱动） |

---

## 3. 目标与非目标

### 3.1 目标（v1.0 GA）

| # | 目标 | 可度量结论 |
|---|---|---|
| G1 | 提供一个**生产可用**的本地 OCR HTTP 服务，单二进制/单容器启动 | `ferroocr serve` 一条命令起服务；镜像 < 120 MB（CPU 版）；无外部动态库（除系统 libc/CUDA 驱动） |
| G2 | **CPU 与 GPU 双路支持**，且 **GPU 上必须比 CPU 快 ≥ 2×** | 同图同模型端到端：CPU 约 5 s 的场景，GPU ≤ 2–3 s（**≥2×**）。目标是**在任意主流显卡上成立**（不限于 N 卡），推荐经 `ort` + DirectML/CoreML/CUDA EP 链达成（§9.4b）；未达标的硬件自动回退 CPU（§11.3 G2b） |
| G2b | **客户端零安装要求** | 用户只需装 NVIDIA 显卡驱动，**不需要安装 CUDA Toolkit**；`ferroocr devices` 能明确报告"驱动是否就绪 / 缺什么"；非 N 卡环境自动回退 CPU 而不报错 |
| G3 | 服务能扛住并发，不 OOM、不雪崩 | 64 并发下 p99 稳定、RSS 增幅 < 30%、无未处理 panic；超载时返回 429 而非拖垮 |
| G4 | 模型资产可控（离线、可校验、可热切换） | 支持完全离线部署（零外网）；模型 SHA-256 校验；切换模型不重启（≤ 5 s 生效） |
| G5 | 精度不退化 | 相对上游 CLI 输出**逐字符一致率 100%**（同模型同参数）；CI 精度门禁 |
| G6 | 可观测、可诊断 | `/healthz`、`/readyz`、`/metrics`、`/v1/devices`、结构化 JSON 日志；一次请求可追踪（request_id 贯穿） |
| G7 | 文档与示例齐备 | OpenAPI 3.1 + curl/Python/Node/Go 示例 + Postman 集合 |

### 3.2 定位口径（对外宣传必须以这些数据为准）

| 维度 | 结论 | 依据 |
|---|---|---|
| 部署体积 / 依赖 | ✅ **显著领先**（2.1 MB 二进制、零 C++ 依赖 vs PaddleOCR 120 MB + Python 环境） | 上游 benchmark |
| 内存占用 | ✅ **显著领先**（190–248 MB vs PaddleOCR 695 MB / RapidOCR 397 MB） | 同上 |
| 精度 | ⚖️ **同档**（OmniDocBench 1-NED 45.70% vs PaddleOCR Tiny 45.33%、RapidOCR 44.84%） | 同上 |
| **绝对延迟** | ❌ **不如 PaddleOCR**（1 780 ms vs 731 ms 均值） | 同上 |
| GPU 收益 | ⚠️ **取决于后端与硬件**：rusto-nn 自研后端在核显上实测为负（-21%）；**改用 `ort` + DirectML 则走成熟 GPU kernel，覆盖全部 DX12 显卡**，业务实测经验 CPU 5 s → GPU 2–3 s | `PURE_RUST_MNN_PERFORMANCE.md:46-52` + §9.4b + 业务经验 |

> **宣传语必须分场景**：
> - **CPU / 通用场景**：「极致轻量的本地 OCR 服务：单二进制、零 C++ 依赖、内存安全、可离线、精度对齐 PaddleOCR」
> - **N 卡独显场景**：「GPU 加速，整页识别从 5 秒降到 2 秒级，且**无需安装 CUDA**」
>
> 两者不冲突：前者是部署优势，后者是性能优势。**唯一不能说错的是"核显也更快"——数据显示那是假的**（核显 FP32 仅比 CPU 快 1.5–2×，抵不掉驱动往返）。

### 3.3 非目标（v1.0 明确不做）

| # | 非目标 | 理由 |
|---|---|---|
| N1 | 不做 SaaS / 多租户 / 计费 | 定位是本地服务；多租户仅在 v1.x 以 API Key 粒度做隔离 |
| N2 | 不自研训练/微调能力 | 训练交给 PaddleOCR/Indra 生态，本项目只做推理与服务 |
| N3 | 不做 PDF 渲染引擎 | 默认只接图片；PDF 支持走"可选内置 rasterizer（纯 Rust `pdfium` 替身不现实）→ v1.1 用外部 `pdftoppm`/`mutool` 或纯 Rust `pdf` 栅格化方案，先留接口" |
| N4 | 不追求表格/公式/手写的 SOTA | 表格结构、版面是上游已有能力的"暴露"，不做算法改进 |
| N5 | 不做浏览器端 WASM 服务 | 上游 `rusto-wasm` 已覆盖，本项目只做 native 服务 |
| N6 | 不做分布式/多机调度 | 单机多进程为上限；横向扩展交给 K8s 副本 |
| N7 | 不改造上游推理图的算子融合/量化 | 只做**服务侧**优化；对上游的补丁以 fork/PR 方式最小化维护 |

---

## 4. 目标用户与典型场景

| 用户 | 场景 | 关键诉求 |
|---|---|---|
| **U1 内网业务系统开发者** | 发票/合同/工单 OCR，Java/Go 后端调用 | 稳定 HTTP 契约、可预测延迟、JSON 带坐标 |
| **U2 文档数字化团队** | 批量扫描件 → 可检索文本/Markdown | 批量任务、进度、失败重试、版面与阅读顺序 |
| **U3 边缘/嵌入式部署者** | 工控机/门店盒子，无 GPU、无 Python | 单二进制、小内存、离线模型、无 C++ 依赖 |
| **U4 GPU 工作站用户** | N 卡工作站/服务器（GTX 1660 ～ RTX 4090），批量 PDF 高吞吐 | **整页 ≥2× 加速**（5 s → 2–3 s）、**免装 CUDA**、批处理、多卡可选 |
| **U5 Rust 应用开发者** | 在自己的 Rust 服务里内嵌 OCR | `ferroocr-client` / 直接用 `ferroocr-core`，无网络跳转 |

**典型调用链路**

```
业务系统 ──HTTP──> FerroOCR(serve) ──> 引擎池(Engine Pool) ──> rusto-core Pipeline ──> rusto-nn(CPU SIMD / CUDA / WebGPU)
                                    └──> 任务调度（同步直通 / 异步队列）
                                    └──> 模型仓库（本地缓存 + 校验 + 热切换）
```

---

## 5. 术语

| 术语 | 含义 |
|---|---|
| **引擎实例（Engine）** | 一个 `rusto_core::engine::RustO`（`OcrPipeline`）实例，持有 det/rec/cls/orient/unwarp/layout/order 会话权重；**不 `Sync`**，需独占使用 |
| **引擎池** | N 个预热好的引擎实例 + 借用队列；请求级并发的基本单位 |
| **后端（Backend）** | CPU（AVX-512/AVX2/NEON）/ CUDA / WebGPU / RTen / MNN-C++ |
| **设备（Device）** | 物理计算单元：`cpu`、`cuda:0`、`cuda:1`、`webgpu:0` |
| **档位（Tier）** | 模型组合：`fast`（tiny）/ `balanced`（small）/ `accurate`（medium）/ `indra`（默认） |
| **阶段（Stage）** | 管线阶段：`preDet → det → postDet → preRec → rec → postRec`，外加 `cls/orient/unwarp/layout/order/table` |

---

## 6. 产品需求

> 优先级：**P0** = v1.0 必须；**P1** = v1.0 尽量 / v1.1 必做；**P2** = 后续。

### 6.1 部署形态（P0）

| ID | 需求 | 验收 |
|---|---|---|
| F-1.1 | 单二进制 `ferroocr`，子命令 `serve` / `ocr` / `models` / `devices` / `bench` / `config` | 6 个子命令均有 `--help` 与示例 |
| F-1.2 | 三种运行模式：**standalone 二进制**、**Docker 容器**（`-cpu` / `-cuda` 标签）、**库内嵌**（作为 crate 依赖） | 三种模式 CI 各有一个冒烟测试 |
| F-1.3 | 零配置启动：`ferroocr serve` 即监听 `127.0.0.1:8600`，缺模型自动下载（需联网）；离线环境用 `ferroocr models pull --bundle <dir>` | 冷启动 ≤ 5 s（模型已缓存） |
| F-1.4 | 配置优先级：CLI 参数 > 环境变量 `FERRO_*` > `ferroocr.toml` > 内置默认；`ferroocr config print` 打印生效配置与来源 | 覆盖矩阵测试 |
| F-1.5 | 优雅退出：收到 SIGTERM 后停止接新请求、等待在飞请求（可配 `shutdown_grace`，默认 30 s），清理临时文件 | 压测中 SIGTERM 无请求中断 |

### 6.2 HTTP API（P0）

**默认仅监听 `127.0.0.1`**，绑定 `0.0.0.0` 必须显式 `--bind` 且给出安全告警（见 F-9）。

| 方法 | 路径 | 说明 | 优先级 |
|---|---|---|---|
| `POST` | `/v1/ocr` | **同步识别**：单图，返回结构化结果 | P0 |
| `POST` | `/v1/ocr/batch` | 同步批量（≤ 32 张），返回逐项结果与错误 | P0 |
| `POST` | `/v1/jobs` | **创建异步任务**（图片数组 / 目录 / 压缩包），返回 `job_id` | P0 |
| `GET` | `/v1/jobs/{id}` | 任务状态、进度（已完成/总数）、失败项、统计 | P0 |
| `GET` | `/v1/jobs/{id}/results` | 分页拉取结果（`cursor`/`limit`，支持 `NDJSON` 流式） | P0 |
| `POST` | `/v1/jobs/{id}/cancel` | 取消任务（协作式，在批次边界生效） | P0 |
| `DELETE` | `/v1/jobs/{id}` | 删除任务与结果 | P1 |
| `GET` | `/v1/models` | 列出可用模型/档位、体积、语言、是否已缓存 | P0 |
| `POST` | `/v1/models/pull` | 下载/导入模型（支持 `--file` 本地 tar 包） | P0 |
| `POST` | `/v1/models/{id}/activate` | 热切换默认模型 | P0 |
| `GET` | `/v1/devices` | 设备清单：类型、名称、算力、显存、当前后端、是否可用、健康状态 | P0 |
| `POST` | `/v1/runtime/reload` | 重载配置（日志级别、并发、模型映射） | P1 |
| `GET` | `/healthz` `/readyz` `/metrics` | 存活 / 就绪 / Prometheus 指标 | P0 |
| `GET` | `/v1/version` | 版本、上游 `rusto-rs` 版本、后端能力矩阵 | P0 |
| `GET` | `/openapi.json` + `/docs` | OpenAPI 3.1 与交互文档 | P1 |
| `GET` | `/v1/params` | **参数自省**：返回所有可配参数的类型/默认值/枚举/说明（借鉴 Umi-OCR 的 `/api/ocr/get_options`） | P1 |
| `POST` | `/v1/ocr/stream` | 单图 + `NDJSON` 流式返回（大页边识别边吐） | P2 |

**F-2.1 `/v1/ocr` 请求契约**

```jsonc
{
  "image": "<base64 或 data-url>",   // 三选一：image / image_url / image_path（path 默认禁用）
  "image_url": "https://...",         // 默认禁用，需显式开启（SSRF 风险）
  "id": "trace-123",                  // 可选，回显
  "model": "indra",                   // 档位或模型 id，缺省取服务默认
  "device": "auto",                   // auto | cpu | cuda:0 | webgpu:0
  "pipeline": {                       // 覆盖默认管线开关
    "det": true, "rec": true,
    "cls": true,                      // 180° 行方向
    "orient": true,                   // 整页 0/90/180/270
    "unwarp": false,                  // UVDoc 3D 去畸变（贵）
    "layout": "cdla",                 // false | "cdla" | "publaynet"
    "reading_order": true,
    "table": false
  },
  "params": {                         // 透传上游 6 阶段参数（白名单）
    "pre_detection":  { "target_max_side": 1280, "descreen_strength": 0.5 },
    "detection":      { "limit_side_len": 960, "limit_type": "max" },
    "post_detection": { "box_threshold": 0.6, "unclip_ratio": 1.6 },
    "pre_recognition":{ "line_y_threshold": 0.5, "word_x_threshold": 0.4 },
    "recognition":    { "batch_size": 8 },
    "post_recognition":{ "score_threshold": 0.5 }
  },
  "output": {
    "format": "json",                 // json | text | markdown | tsv | spatial | hocr | alto
    "granularity": "lines",           // lines | words | spatial | tsv
    "include_image_size": true,
    "include_layout_regions": true,
    "include_polygon": true,
    "include_timing": true
  }
}
```

**F-2.2 响应契约（稳定，遵循语义化版本）**

```jsonc
{
  "id": "trace-123",
  "request_id": "01J...",
  "status": "ok",
  "model": { "id": "indra", "tier": "indra", "version": "0.4.0" },
  "device": { "used": "cpu", "backend": "avx2", "fallback": false },
  "image": { "width": 2480, "height": 3508, "format": "jpeg" },
  "page": { "orientation": 0, "unwarped": false },
  "lines": [
    {
      "text": "COFFEE SHOP",
      "score": 0.992,
      "polygon": [[95,30],[275,30],[275,55],[95,55]],
      "frame": { "left": 95, "top": 30, "width": 180, "height": 25 },
      "words": [ { "text": "COFFEE", "score": 0.99, "frame": {...} } ]
    }
  ],
  "layout_regions": [ { "type": "title", "frame": {...}, "line_ids": [0] } ],
  "tables": [ { "rows": 3, "cols": 4, "cells": [...] } ],
  "text": "COFFEE SHOP\nTotal $16.50",
  "timing_ms": { "preprocess": 12, "det": 312, "rec": 169, "post": 8, "total": 501 }
}
```

**F-2.3 语义约定**

| 项 | 要求 |
|---|---|
| 错误模型 | RFC 9457 `application/problem+json`：`{type,title,status,detail,instance,request_id}`，错误码枚举稳定（见 F-2.4） |
| 幂等 | `Idempotency-Key` 头支持；同 key 30 min 内返回同一响应 |
| 限流 | 令牌桶；超限 `429` + `Retry-After` |
| 超时 | 请求级 `timeout_ms`（默认 30 s），超时 `504` 并释放引擎实例 |
| 大请求 | 默认 body ≤ 20 MB；解码后像素 ≤ 8000×8000，超限 `413` |
| 版本 | 路径版本 `/v1`；breaking 变更走 `/v2`；响应加 `Ferro-Api-Version` 头 |
| 压缩 | 支持 `Accept-Encoding: gzip/zstd`（大量 box 数据体积敏感） |
| 流式 | `/v1/jobs/{id}/results` 支持 `NDJSON` chunked，边算边吐 |

**F-2.4 错误码**

| HTTP | code | 场景 |
|---|---|---|
| 400 | `invalid_request` | JSON 解析失败、字段越界 |
| 413 | `payload_too_large` | 图片过大 / 像素超限 |
| 415 | `unsupported_media_type` | 非 JPEG/PNG/WebP/BMP/TIFF/PDF(可选) |
| 422 | `decode_failed` | 图片损坏、EXIF 异常 |
| 429 | `rate_limited` / `queue_full` | 限流 / 队列满 |
| 503 | `no_device_available` | 设备全忙或后端不可用 |
| 504 | `timeout` | 请求超时 |
| 500 | `engine_error` / `model_error` | 上游 EngineError 映射（附 `upstream` 字段） |

### 6.3 设备与后端（P0，项目核心难点）

| ID | 需求 | 说明 / 验收 |
|---|---|---|
| F-3.1 | **设备探测** | 启动时枚举 CPU 特性（AVX-512/AVX2+FMA/NEON）、CUDA 驱动与设备（`cudarc` + `is_cuda_driver_available()`）、WebGPU 适配器；结果进 `/v1/devices` 与日志 |
| F-3.2 | **引擎与 EP 选择策略** | `--engine auto\|rusto\|ort`；`ort` 下 EP 链按平台自动排列：**Win** `DirectML→CPU`、**N 卡** `CUDA→TensorRT→DirectML→CPU`、**macOS** `CoreML→CPU`、**Linux** `CUDA→WebGPU→CPU`。可强制指定；策略可配（`prefer_gpu`、`allow_cpu_fallback`）。验收：在无任何 GPU 的机器上不报错、自动落到 CPU；在 AMD/Intel 显卡上也能启用 GPU（DirectML） |
| F-3.2b | **EP 注册失败可见** | 使用 `ort` 的 `.error_on_failure()` 控制：显式要求 GPU 时失败即报错；`auto` 模式则允许静默回退并记指标（`ferro_fallback_total{ep,reason}`）。验收：不再出现上游那种"静默回退、以为在用 GPU"的黑盒（对比 `gemm().is_ok()`） |
| F-3.3 | **自动回退** | GPU 初始化失败/推理失败/显存不足 → 该请求立即降级 CPU 并**记录度量**（`ferro_fallback_total{reason}`），连续失败 N 次熔断 GPU（半开恢复） |
| F-3.4 | **多设备 / 多卡** | 支持 `--devices dml:0,cuda:0,cpu`：`ort` 侧多 EP/多会话可同进程并存（比 rusto-nn 的全局单槽灵活）；`rusto-nn` 侧仍受进程级单例限制。路由支持 `least_loaded` / `round_robin` / `sticky` |
| F-3.5 | **引擎池** | 每设备 `--workers N`（默认 `min(4, cores/2)`）；实例预热（模型加载 + 1 次 warmup 推理）；借用超时后 `503` |
| F-3.6 | **CPU 亲和与线程治理** | 服务级 `rayon` 全局池线程数 = `--cpu-threads`（默认 `cores - 1`）；避免 `workers × 上游内部并行` 造成线程超卖（上游识别并行度自带 `min(2)` 上限，需实测校准） |
| F-3.7 | **GPU 内存治理** | 显存预算 `--gpu-mem-budget`；超阈值时拒绝新 GPU 任务并降级 CPU；不出现 OOM panic（`catch_unwind` 包裹后端调用，上游 CUDA 已做） |
| F-3.8 | **能力矩阵** | `/v1/version` 报告后端×算子覆盖（明确写：GPU 仅覆盖 GEMM，conv/depthwise 仍在 CPU），避免用户误解 |
| F-3.9 | **GPU 门禁开关** | 特性开关 `FERRO_GPU_EXPERIMENTAL=1` 才启用；CI 基线未达标前默认关闭 |

> **这条需求背后的硬事实**：上游 `rusto-nn` 的 GPU 分派只有一处——`kernels/gemm/mod.rs` 中 `active_backend()` 且 `m*n*k >= 65536`；`conv.rs`/`depthwise.rs` 中 `active_backend` 出现 0 次。Detection（DBNet 的 ResNet 主干）与 Recognition（PP-OCRv6 rec / Indra LineNet）的主要算力在 conv/depthwise 上，因此**当前 GPU 加速上限很低**。上游实测（Intel Iris Xe 核显，`benchmarks/PURE_RUST_MNN_PERFORMANCE.md:46-52`）印证了这一点：**密集页 GPU 比 CPU 慢 21%，最好的一例只快 3%**。要让"GPU 更快"在独显上成立，必须做 §9.4 列出的三项改造。这是本项目的**最大技术风险**，也是需求必须显式写明的验收门槛。

### 6.4 模型管理（P0）

| ID | 需求 | 验收 |
|---|---|---|
| F-4.1 | **内置档位** | `indra`(默认, 5.4 MB) / `fast`(ppocrv6-tiny, 6 MB, INT8 3.4 MB) / `balanced`(small, 30 MB) / `accurate`(medium, 134 MB) / `ppv5`·`ppv4` 多语言包；`GET /v1/models` 全量暴露 |
| F-4.2 | **语言** | 支持上游语言码：`ch, latin, rtl, world, ja, ko, ar, cyrillic, devanagari, chinese-cht`；请求级 `lang` 选择对应 rec + dict |
| F-4.3 | **离线 bundle** | `ferroocr models bundle --tiers fast,indra --out ./bundle` 产出可拷贝目录（含 sha256 清单 `models.lock.json`）；`--offline` 模式下缺模型直接报错、绝不联网 |
| F-4.3b | **下载实现必须自研（不能沿用上游）** | ⚠️ 上游 `src/download.rs:8-63` 用 `std::process::Command` 调**外部 `curl`/`wget`/`powershell`**——distroless/scratch 镜像里没有这些命令，会直接失败。本项目必须用 `reqwest`+`rustls` 在进程内下载（也才能做 TLS 校验、代理、限速、断点续传、SHA-256）。**验收**：在无 curl/wget 的 scratch 容器里完成模型下载并通过校验 |
| F-4.4 | **完整性校验** | 所有模型文件启动时校验 SHA-256；不匹配拒绝加载并给出明确错误 |
| F-4.5 | **热切换** | `POST /v1/models/{id}/activate`：新实例池蓝绿切换，旧池在 in-flight 结束后回收；切换期间服务不中断 |
| F-4.6 | **缓存与多副本共享** | 模型目录可配置（默认 `~/.cache/ferroocr/models`，容器内 `/models`）；支持只读挂载共享给多副本 |
| F-4.7 | **镜像预置** | Docker 镜像内置 `fast` + `indra` 两档，其余按需下载；提供 `-slim`（仅 indra）变体 |
| F-4.8 | **下载源可替换**：支持自定义镜像/代理（`FERRO_MODEL_BASE_URL`、`HTTPS_PROXY`），应对 HF 不可达场景 | 在隔离网络下用本地 HTTP 服务做源，下载成功 |

### 6.5 预处理与文档理解（P0/P1）

| ID | 需求 | 优先级 |
|---|---|---|
| F-5.1 | 上游全部管线开关暴露：`det`/`rec`/`cls`(180°)/`orient`(整页)/`unwarp`(UVDoc)/`layout`(cdla,publaynet)/`reading_order`/`table` | P0 |
| F-5.2 | 上游 6 阶段参数白名单透传 + 服务端校验（`limit_side_len`、`box_threshold`、`unclip_ratio`、`batch_size`、`score_threshold` 等） | P0 |
| F-5.3 | **预设（preset）**：`receipt`（开 spatial + 表格）、`document`（开 layout+order）、`scan`（开 orient+unwarp）、`plain`（只 det+rec），避免业务方逐个调参 | P0 |
| F-5.4 | 图片预处理：EXIF 旋转纠正（默认开）、灰度/白底化、超大图自动降采样（`max_side_len`）、极端长宽比切分 | P0 |
| F-5.5 | **PDF 输入**：多页 → 逐页任务（可选栅格化后端），v1 先支持"PDF 内嵌图片提取"，全页栅格化 v1.1。**必须走异步任务 + 轮询**（Umi/MinerU 的做法），不能一个长连接等到底 | P1 |
| F-5.6 | 局部识别：`bbox` 参数只识别指定区域（发票字段抽取加速 3–10×） | P1 |
| F-5.7 | 去畸变与方向仅在对检测置信度/图像质量启发式触发时启用（成本控制） | P1 |
| F-5.8 | **页数/张数上限可配**：默认单请求 ≤ 50 页/张，`FERRO_MAX_PAGES` 可解除（PaddleX 默认只处理 10 页且易踩坑，我们要显式可控） | P0 |
| F-5.9 | **段落还原**：输出带 `para_id` 或 `paragraph_break`，用于无损重建段落与换行（借鉴 Umi-OCR 的 `end` 语义） | P1 |

### 6.6 输出格式（P0/P1）

| 格式 | 优先级 | 说明 |
|---|---|---|
| `json`（默认） | P0 | 见 F-2.2，含 polygon/frame/score/layout/tables/timing |
| `text` / `text-ordered` | P0 | 纯文本 / 阅读顺序 |
| `markdown` | P0 | 版面块 → Markdown（标题/列表/表格尽力映射） |
| `tsv` | P0 | 与上游一致，便于 `awk` |
| `spatial` | P0 | 2D 空间对齐文本（票据/多栏） |
| `hocr` / `ALTO XML` | P1 | 对接扫描件归档/图书馆系统 |
| `searchable PDF` | P2 | 需要 PDF 写入层，暂不做 |
| 自定义字段映射 | P2 | 模板抽取（正则/坐标区域 → 业务字段） |

> **两条来自竞品的关键设计（建议直接采纳）**：
> 1. **段落级 `end` 语义**（Umi-OCR）：每行除 `box` 外附带 `end` 标记，用它可以**无损还原段落/换行**，比事后按几何猜段落可靠得多。建议在 JSON 中增加 `paragraph_break: bool` 或 `para_id`。
> 2. **行级最小公约数**：四家方言的行字段交集是 `{text, score, box}`（Paddle 叫 `confidence`/`text_region`，RapidOCR 叫 `rec_txt`/`dt_boxes`）。→ 自研 schema 用 `{text, score, polygon}`，兼容层只做字段改名，成本极低。

### 6.7 并发、队列与调度（P0）

| ID | 需求 | 验收 |
|---|---|---|
| F-7.1 | **有界队列**：同步路径进"直通池"，异步任务进"任务队列"（默认容量 1024），满则 `429 queue_full` | 压测下内存不随请求线性增长 |
| F-7.2 | **优先级**：`X-Priority: high/normal/low` 三级；高优先级独立小池，避免被批量任务饿死 | 高优 p95 在批量满载时仍达标 |
| F-7.3 | **公平性**：单客户端（API Key/IP）并发上限（默认 8），防单点打满 | 多客户端压测无明显偏斜 |
| F-7.4 | **取消与超时**：协作式取消（在 OCR 批次边界检查），释放引擎 | 取消后 500 ms 内实例归还池 |
| F-7.5 | **任务持久化**：任务元数据落盘（默认 `sqlite` 或 `sled` / 纯文件 + WAL），重启可恢复未完成任务；结果文件按 job 分片存储，支持断点续跑 | kill -9 后重启，任务从断点继续 |
| F-7.6 | **重试**：任务项可配 `max_retries`（默认 1），失败项与成功项分离输出 | 单张坏图不拖垮整批 |
| F-7.7 | **背压**：队列水位 > 80% 时新异步任务返回 429；同步路径按引擎池饱和返回 503 | — |

### 6.8 观测性与运维（P0）

| ID | 需求 |
|---|---|
| F-8.1 | 结构化日志（JSON），字段：`ts, level, request_id, job_id, device, model, stage, dur_ms, err`；支持 `RUST_LOG` 与热调级别 |
| F-8.2 | Prometheus 指标：`ferro_requests_total{route,status,device,model}`、`ferro_ocr_duration_seconds{stage}`（直方图）、`ferro_queue_depth`、`ferro_engine_pool_in_use`、`ferro_fallback_total{reason}`、`ferro_gpu_mem_bytes`、`ferro_model_load_seconds` |
| F-8.3 | `/readyz` 语义：模型已加载 + 至少 1 个引擎实例可用；`/healthz` 仅进程存活 |
| F-8.4 | 分布式追踪（可选）：`traceparent` 透传，OTLP 导出（v1.1） |
| F-8.5 | `ferroocr doctor`：自检（模型、权限、设备、端口、线程数、显存），输出可粘贴的诊断报告 |
| F-8.6 | 崩溃转储：panic 捕获 → 记录请求上下文 → 该请求 500，进程继续（`panic = "unwind"`，注意上游 `release` profile 是 `panic = "abort"`，服务端需覆盖为 unwind） |

> 注：上游 workspace 的 release profile 是 `panic="abort"` + `opt-level="z"` + `lto="fat"`——服务端需 **`opt-level=3` + `panic=unwind`**，这两点必须在本项目自己的 profile 里覆盖，否则既慢又无法隔离单请求崩溃。

### 6.9 安全（P0）

| ID | 需求 |
|---|---|
| F-9.1 | 默认 `127.0.0.1` 绑定；`0.0.0.0` 时必须显式 `--allow-remote` 且打印醒目告警 |
| F-9.2 | 可选 API Key（`Authorization: Bearer`，多 key + 每 key 配额）；key 仅从环境变量/文件读取，不进日志 |
| F-9.3 | `image_path` 与 `image_url` 默认**关闭**；开启时：路径白名单根目录（防穿越）、URL 协议白名单 + 私网地址拒绝（防 SSRF）、重定向次数上限、超时 |
| F-9.4 | 上传限制：size/像素/页数三重上限；解码在受限线程（防解压炸弹） |
| F-9.5 | 临时文件：`0600` 权限、进程退出清理、`--tmp-dir` 可指定；不把原图写入持久结果目录 |
| F-9.6 | 依赖合规：`cargo deny`（许可白名单）+ `cargo audit` 进 CI |
| F-9.7 | 容器：非 root、只读根文件系统、`no-new-privileges`；镜像不打包含 secret 的层 |

### 6.10 客户端与集成（P1）

| ID | 需求 |
|---|---|
| F-10.1 | `ferroocr-client`（Rust）：同步 + 异步（tokio），类型化响应 |
| F-10.2 | Python / Node / Go 薄封装示例（非 SDK 承诺），覆盖同步、批量、任务轮询 |
| F-10.3 | `docker compose` 示例：仅服务；服务 + Prometheus + Grafana 面板 JSON |
| F-10.4 | 与 Nginx/K8s Ingress 的部署示例（超时/大 body 配置注意事项） |

---

## 7. 上游能力盘点（复用 vs 需改造）

| 能力 | 上游对应 | 成熟度 | 复用方式 | 需改造点 |
|---|---|---|---|---|
| 文本检测（DBNet） | `rusto-core/src/det.rs`、`preprocess.rs` | 成熟 | 直接调用 | 无 |
| 文本识别（CTC/LineNet） | `rusto-core/src/rec.rs`、`ctc.rs` | 成熟 | 直接调用 | 批大小与并发需服务侧编排 |
| 180° 行方向 | `cls` 会话（`ppocr_cls`） | 成熟 | 开关 | 无 |
| 整页方向 0/90/180/270 | `rusto-core/src/orient.rs` | 成熟 | 开关 | 无 |
| 3D 去畸变（UVDoc） | `rusto-core/src/unwarp.rs` | 可用 | 开关（贵） | 触发策略 |
| 版面区域（PicoDet CDLA/PubLayNet） | `rusto-core/src/layout.rs` | 可用 | 开关 | 输出映射到 Markdown |
| 阅读顺序（神经图） | `rusto-core/src/order.rs` | 可用 | 开关 | 无 |
| 空间对齐 / TSV | `rusto-core/src/spatial.rs` | 成熟 | 直接调用 | 无 |
| 表格结构 | `rusto-core/src/table.rs` | 实验 | 开关 | 输出 schema 需稳定化 |
| 推理运行时 | `crates/rusto-nn`（MNN 图 + SIMD） | 成熟 | 默认后端 | 线程/内存治理 |
| RTen 后端 | `crates/rusto-rten` | 可用但**很慢**（OmniDocBench 12 s vs 1.8 s） | 备选 | 默认不启用 |
| MNN C++ 后端 | `crates/rusto-mnn` | 可用（需 C++ 工具链） | 可选特性 | 与"零 C++"目标冲突，仅作 perf 参考 |
| CUDA 后端 | `crates/rusto-nn-cuda` | **实验**（仅 GEMM、每次拷贝、全局单例） | 需改造 | 见 §9.4 |
| WebGPU 后端 | `crates/rusto-nn-webgpu` | **实验**（仅 GEMM、m*n*k≥65536） | 需改造 | 见 §9.4 |
| 模型下载 | `src/download.rs`（**调外部 curl/wget**）、HuggingFace | 可用但形态不利 | **不复用下载实现**，重写为进程内 HTTP | 外部命令依赖、无校验、无断点续传 |
| HTTP 服务 | **不存在** | — | **本项目新建** | 全部 |
| 并发/队列/持久化 | **不存在**（仅 `rayon`，且并行度 `min(2)`） | — | **本项目新建** | 全部 |
| 观测性 | 无 | — | **本项目新建** | 全部 |

---

## 8. 非功能需求（NFR）

### 8.1 性能（SLO，需在 M1 用实测基线校准，验收见 §11.2 / §11.3）

基准机定义：**8 vCPU（AVX2）/ 16 GB RAM / NVMe**，图片集：A4 300 DPI 扫描件（2480×3508，约 30–60 行）与 1600×993 文档。

| 指标 | CPU 目标（P0，**M1 后校准**） | **GPU 目标（P0，N 卡独显）** |
|---|---|---|
| 单页（A4 300DPI，40 行，`fast` 档，det+rec） p50 | ≤ 5 000 ms（业务经验值） | **≤ 2 500 ms（≥2×）** |
| 同上 p95 | ≤ 9 000 ms | ≤ 4 500 ms |
| 单页（同上，`indra` 档） p50 | ≤ 6 000 ms | ≤ 3 000 ms |
| 吞吐（4 worker，混合负载） | ≥ 1.2 页/s | **≥ 2.5 页/s** |
| **端到端加速比** | 1×（基线） | **≥ 2.0×**（业务目标 5 s → 2–3 s） |
| 冷启动（模型已缓存，1 worker） | ≤ 5 s | ≤ 8 s（含首次 CUDA 上下文初始化） |
| 模型切换（热切换生效） | ≤ 5 s | ≤ 8 s |
| 单请求内存增量 | ≤ 300 MB（大图） | ≤ 300 MB 主机内存 + ≤ 1.5 GB 显存 |
| 满压 RSS | ≤ 2.5 GB（4 worker × indra） | ≤ 4 GB + 显存 ≤ 4 GB |

> **CPU 5 s / GPU 2–3 s 是业务方给出的实测经验值，作为本项目的核心性能目标（P0，非"尽力而为"）**。这意味着 **§9.4 的 GPU 改造（A/B/C/D）不是可选项，而是达成目标的必经路径**——只做"接线"拿不到 2×。
>
> ⚠️ **引用上游数字时必须注意两个坑**（已在 §12-R11 记录）：
> 1. **上游 benchmark 不是用发布 profile 测的**：`benchmarks/BASELINE_LATENCY.md:6` 写明 `Profile: release (opt-level = 3, LTO enabled)`，但 `Cargo.toml:117` 的 release 实际是 `opt-level = "z"`（体积优先）。→ 用户 `cargo build --release` 出来的二进制**达不到文档宣称的速度**，本项目必须在自己的 profile 里覆盖为 `opt-level = 3`。
> 2. **上游各文档数字口径不一致**（同一 pipeline 在不同文档里差异可达数倍，MNN C++ 有 479 ms 与 39.7 ms 两种口径，疑似 pipeline / 纯推理混用）。→ **SLA 一律以自建 `ferroocr-bench` 复测为准，不直接引用上游数字**。
>
> **为什么本表的 CPU 值从 1 800 ms 上调到 5 000 ms**：早期草案引用的是上游 i7-1185G7 在 1600×993 小图上的 678 ms，再按面积外推到 A4——那是**乐观外推**。业务方在真实 A4 全页场景实测 CPU 需 **5 s 以上**，与"1600×993 的 5.5 倍面积"的实际表现吻合（面积外推低估了 rec 阶段随行数线性增长的成本）。**以实测经验值为准**。

### 8.2 可靠性

| 指标 | 要求 |
|---|---|
| 可用性 | 单实例连续 7×24 h 压测无内存泄漏（RSS 漂移 < 5%） |
| 故障隔离 | 单请求 panic/OOM 不得杀死进程；单图失败不影响批内其他项 |
| 恢复 | 进程崩溃重启后异步任务可续跑（已落盘的项不重算） |
| 降级 | GPU 不可用 → CPU；模型缺失 → 明确错误而非崩溃；磁盘满 → 停止接任务而非丢数据 |

### 8.3 精度

| 要求 | 度量 |
|---|---|
| 与上游 CLI 一致 | 同模型同参数下，`text` 逐字符一致率 **100%**；`score` 差 < 1e-4 |
| 精度回归门禁 | `ferroocr-bench` 在固定 200 张样本集上的 1-NED 不得比基线差 > 0.5 个百分点 |
| 图像变换回归 | EXIF 纠正/降采样不得改变文本内容（除尺度相关坐标） |

### 8.4 可移植性与交付

| 项 | 要求 |
|---|---|
| 目标平台 | Linux x86-64（主）、Windows x86-64、macOS arm64；Android/iOS 不在范围 |
| 构建 | `cargo build --release` 单命令；提供 `--features cuda` / `--features webgpu` |
| 依赖 | CPU 版镜像无 C++ 运行时依赖；GPU 版仅依赖 NVIDIA 驱动（CUDA runtime 由 `cudarc` 动态加载，不需要编译期 CUDA SDK） |
| 镜像体积 | `-slim` ≤ 80 MB、`-cpu` ≤ 120 MB、`-cuda` ≤ 1.2 GB |
| 交叉编译 | 提供 `aarch64-unknown-linux-gnu` 与 `x86_64-pc-windows-msvc` 构建脚本 |

### 8.5 兼容性

| 项 | 要求 |
|---|---|
| 上游版本 | 锁定 `rusto-rs = "=0.4.x"`；升级需过精度+性能门禁 |
| API 兼容 | `/v1` 内只增不改；废弃字段保留一个 minor 版本并给 `Deprecation` 头 |
| 模型格式 | `.mnn` / `.rten` / `.onnx` 均支持（能力差异见能力矩阵） |
| 配置兼容 | 配置项只增不删；未知字段告警不报错 |

---

## 9. 技术方案要点（供架构设计参考，非最终实现）

### 9.1 建议 crate 划分

```
ferroocr/                     # workspace
├── crates/
│   ├── ferroocr-core/        # 引擎抽象层：OcrEngine trait、参数映射、结果规范化
│   │   ├── engine-rusto/     #   实现 A：rusto-nn（纯 Rust CPU，2.1 MB）
│   │   └── engine-ort/       #   实现 B：ort（DirectML/CUDA/CoreML/WebGPU GPU 加速）
│   ├── ferroocr-device/      # 设备探测、EP 链选择、能力矩阵、回退与熔断
│   ├── ferroocr-pool/        # 引擎实例池（借用/预热/健康检查/重建）
│   ├── ferroocr-queue/       # 任务队列 + 持久化 + 进度 + 取消
│   ├── ferroocr-http/        # axum 路由、DTO、错误映射、OpenAPI
│   ├── ferroocr-server/      # 组装：CLI serve、配置、指标、优雅退出
│   ├── ferroocr-client/      # Rust SDK
│   └── ferroocr-bench/       # 精度/性能门禁
└── src/bin/ferroocr.rs       # 多子命令二进制
```

> **双引擎是关键设计**（§9.4b）：`OcrEngine` trait 下挂两个实现，CPU 用纯 Rust 的 rusto-nn（保留轻量卖点），GPU 用 ort（拿到成熟 GPU 加速与零安装体验）。两条路径输出统一成同一份结果 schema。

### 9.2 关键选型建议

| 关注点 | 建议 | 理由 |
|---|---|---|
| Web 框架 | `axum` + `hyper` + `tower`（`tower-http` 中间件） | 生态成熟、tower 中间件可组合（限流/超时/压缩/追踪） |
| 异步运行时 | `tokio`（多线程） | 与 axum 一致；OCR 是 CPU 密集 → 用 `spawn_blocking` 或专用线程池，不占 async worker |
| 阻塞计算隔离 | 独立 `rayon` 线程池（每设备一个）或专用 `std::thread` 池 | 避免 async worker 被 OCR 阻塞；同时控制线程超卖 |
| 队列持久化 | `sqlite`（`rusqlite` bundled）或 `sled` | sqlite 便于人工排查/导出；结果大文件走文件系统 |
| 序列化 | `serde` + `serde_json`；`simd-json` 可选 | 大量 box 数据时吞吐敏感 |
| 指标 | `metrics` + `prometheus` exporter | 与 tower 层结合方便 |
| 日志 | `tracing` + `tracing-subscriber`（JSON） | request_id 贯穿 |
| 配置 | `figment` 或手写 `serde` 叠加（CLI/env/file） | 需打印来源，手写更可控 |
| 压缩 | `zstd`（大 JSON）+ gzip | 响应体积可降 5–8× |
| OpenAPI | `utoipa` | 注解生成，避免手写漂移 |
| **GPU 推理（方案 O）** | **`ort 2.0.0-rc.15`** + features `directml` / `cuda` / `coreml` / `webgpu` | ORT 2.x 有完整 EP 抽象与**逐算子自动回退**；DirectML 覆盖 Win 全显卡且零安装；RapidOCR/`retto`/`oar-ocr` 已验证 PaddleOCR→ONNX→ORT 路线 |
| **GPU 推理（方案 R）** | `rusto-nn-cuda`（`cudarc` 动态加载）+ `rusto-nn-webgpu` | 保持纯 Rust，但只有 GEMM 分派，需自行补 conv/depthwise kernel |
| 模型格式 | **ONNX 为主**（GPU 路径必须）；`.rten`/`.mnn` 供 rusto 引擎 | ORT 只吃 ONNX；PaddleOCR 官方有 ONNX 导出路径 |
| 容器 | **先 `debian-slim`**（ORT 的 dylib 需要 glibc，不宜 scratch）；`-slim` 纯 Rust 版可退 distroless | GPU 版含 ORT+DirectML，约 40–60 MB；纯 Rust 版 ~60 MB slim |

### 9.3 必须提前想清楚的三个"上游约束"

1. **引擎不 `Sync`** → 用**实例池**。`InferenceSession::try_clone()` 存在（`engine.rs:124/507/753`，`MnnSession` 实现了 `Clone`），因此可以在池内克隆会话构建同构引擎，代价是**每实例一份权重内存**（indra 约 190–250 MB/实例）。故默认 `workers = 4` 时 RSS ≈ 1 GB，需要 `--shared-models`（mmap 共享权重）作为 v1.1 优化项。上游 `rusto-nn` 依赖 `memmap2`，有做 mmap 共享的基础。
2. **后端是进程级全局单例**（`static ACTIVE_BACKEND: RwLock<Option<Arc<dyn DeviceBackend>>>`）→ **同一进程无法同时用 CPU 与 GPU 跑不同请求**。方案：①v1 单进程单后端（`--device` 全局选择，与上游一致，最简单）；②v1.1 多设备 → **每设备一个子进程**（`ferroocr serve --role worker --device cuda:0`），主进程做路由与生命周期管理（也天然隔离崩溃与内存）。建议 v1 采用 ①，把 ② 作为架构预留。
3. **`rayon` 全局池被上游内部使用**（识别批并行，`min(2)` 上限）→ 服务侧多 worker 会与上游并行叠加。需实测 `workers × cpu_threads` 的最优组合，并支持 `RAYON_NUM_THREADS` 注入；默认建议 `workers=4, cpu_threads=cores-1`，并压测校准。

### 9.4 GPU 真正生效需要做的三件事（工程风险最高）

| # | 改造 | 说明 | 预期收益 |
|---|---|---|---|
| A | **把 conv/depthwise 纳入 GPU 分派** | 上游 `conv2d` 已在 1x1 与 im2col 路径调用 `gemm`，`gemm` 已能走后端；但 `depthwise_conv2d` 是独立 SIMD 实现，**需要新增 `depthwise` 的 DeviceBackend 方法或改写为 group-GEMM** | 覆盖 DBNet 主干与 rec 骨干的主要算力 |
| B | **张量驻留（device-resident tensor）** | 当前 CUDA 后端每次 `gemm` 都 `clone_htod` + `memcpy_dtoh`，中小矩阵下拷贝开销 > 计算收益（这解释了为什么 `m*n*k >= 65536` 的门槛存在）。需要"会话级显存分配器 + 权重常驻" | 去掉每算子拷贝，才可能真正加速 |
| C | **算子融合与批处理** | 把逐层 launch 合并；识别阶段按 batch（上游已有 `rec_batch_num`，默认 6/8）喂给 GPU。上游在 162 行密集页上把 `rec_batch_num` 提到 16，forward pass 从 27 次降到 10 次 | 提高 GPU 利用率，降低 launch 开销 |
| D | **提高 GEMM 门槛 / 按硬件自适应阈值** | 上游固定门槛 `m*n*k ≥ 65536`，是按核显（~1.5–2 TFLOPs）调的。独显（RTX 4090 达 83 TFLOPs）应下调门槛让更多算子下沉 GPU | 让独显真正吃到负载 |

> **上游自己给出的诊断（`PURE_RUST_MNN_PERFORMANCE.md` §4）值得直接采纳**：单次 1×1 卷积的 GPU 驱动往返约 **15–40 μs**，而 CPU AVX-512 在 L1 里只需 **< 0.2 μs**——驱动开销是计算量的 **125 倍**。这解释了为什么核显上 GPU 不划算，也指明了"只有当张量足够大（上游估计 `M×N×K ≥ 262144`）才该下沉 GPU"。
>
> **取舍**：A/B/C/D 属于**对上游的侵入式改造**（预计 6–10 人周，且需要 N 卡 CI）。因为业务方明确提出「GPU 要达到 2–3 s 而 CPU 5 s+」的诉求，**A/B/C/D 从 v1.1 提前纳入 v1.0 的 M3**（见 §10 修订）——否则只接线不改造，在核显上就是负收益，在独显上也拿不到 2×。

---

### 9.4b 关键架构决策：ONNX Runtime（DirectML/OpenVINO）vs 自研 GPU 后端

> 业务方提问：「不是有 ONNX Runtime DirectML 之类吗？不装 CUDA 也能用 GPU 识别？」——**这个思路完全正确，而且可能是达成"免装 CUDA + 2–3 s"更快、更稳的路径**。本节把它与自研路线摊开对比。

#### 9.4b.1 `ort` 的 EP（执行提供者）矩阵（已核实）

`ort = 2.0.0-rc.15`（2026-10-19），crates.io features 中明确包含：

| feature | 依赖 | 硬件覆盖 | 是否需用户额外安装 |
|---|---|---|---|
| `directml` | **DirectML.dll**（随 ORT 包分发的 helper dylib） | **Windows 任意 DX12 显卡**：N 卡 / AMD / **Intel 核显**全覆盖 | ❌ **不需要**（随应用分发 dylib） |
| `cuda` | CUDA + cuDNN | 仅 N 卡 | ✅ 需要（这是 `ort` 文档明写的："Make sure compatible versions of CUDA and cuDNN are installed and available on the `PATH`"） |
| `tensorrt` | TensorRT + CUDA | 仅 N 卡 | ✅ 需要 |
| `openvino` | OpenVINO Runtime | **Intel CPU/iGPU/dGPU** 最佳 | ⚠️ 需 OpenVINO runtime |
| `coreml` | 系统自带 | macOS（ANE/GPU） | ❌ 不需要 |
| `webgpu` | 系统 WebGPU | Win/macOS/Linux 任意 GPU | ❌ 不需要（但 **ort 标注为 experimental**，"may produce incorrect results/crashes"） |

`ort` 文档原文（`docs/content/perf/execution-providers.mdx`）：

> "Certain EPs like CUDA and TensorRT use a separate interface that require them to be compiled as dynamic libraries... **The DirectML and WebGPU EP do not use this interface, but do require helper dylibs.**"

> "Ort will register all EPs specified, in order. **If an EP does not support a certain operator in a graph, it will fall back to the next successfully registered EP, or to the CPU if all else fails.**"

**这两段是关键**：①DirectML 只需随包带一个 dylib，用户零安装；②EP 可以链式注册并逐算子回退——**正好满足"有 GPU 用 GPU、没有就静默走 CPU"**。

#### 9.4b.2 两条路线的硬对比

| 维度 | **路线 R：rusto-nn + 自研 GPU 后端**（当前 PRD 方案） | **路线 O：PaddleOCR ONNX + `ort`（DirectML/OpenVINO/CUDA 链）** |
|---|---|---|
| GPU 覆盖 | 仅 N 卡（CUDA）+ WebGPU 兜底 | **DirectML 覆盖 Win 上全部显卡**（N/AMD/Intel）+ CUDA/TRT 提性能 + CoreML(mac) + WebGPU(Linux) |
| 用户安装 | N 卡只需驱动，但**缺 cuBLAS 会破功**（§9.5.3） | **DirectML 零安装**（dylib 随包） |
| 算子覆盖 | **仅 GEMM**，conv/depthwise 要自己写（§9.4 A/B/C/D，6–10 人周） | **ORT 全覆盖**，conv/depthwise/pool/norm 全有成熟 GPU kernel |
| 工程风险 | 高：要改上游推理内核 + 调 CUDA kernel | **低**：ORT 是微软/Intel 生产级后端，DirectML 已在大量产品中使用 |
| 2–3 s 目标 | 需赌内核改造效果 | **大概率直接达标**（ORT 的 GPU 加速是成熟路径） |
| 交付体积 | 2.1 MB 单二进制（优势极大） | + `libonnxruntime`（约 10–15 MB）+ DirectML.dll（约 10–20 MB）+ Paddle 模型（det/rec ~20–30 MB） |
| 纯 Rust 程度 | **100% 纯 Rust**（无 C++ 依赖） | 引入 C++ 动态库（ORT 是 C++） |
| 精度 | 与上游一致 | PaddleOCR ONNX 是官方模型导出，精度同档；需与 rusto 输出做对齐验证 |
| 已有先例 | 无（我们要做第一个） | **成熟**：RapidOCR 就是 PaddleOCR→ONNX→ORT；`retto`、`oar-ocr` 也是 Rust+ORT 跑 PaddleOCR |

#### 9.4b.3 建议：**两条路线并存，ORT 作为 GPU 首选实现**

考虑到业务目标是"**CPU 和 GPU 都要支持，且 GPU 要快 2–3 倍、客户免装 CUDA**"，建议调整架构为：

```
                    ┌─ CPU      → rusto-nn（纯 Rust SIMD，零依赖，2.1 MB）
FerroOCR 引擎抽象 ──┼─ GPU(Win)  → ort + DirectML（N/AMD/Intel 全支持，零安装）
                    ├─ GPU(N卡)  → ort + CUDA/TensorRT（追求极致性能，可选）
                    ├─ GPU(mac) → ort + CoreML
                    └─ GPU(Linux)→ ort + CUDA / WebGPU
```

即：**保留 `rusto-*` 作为 CPU 与"纯 Rust/极致轻量"发行版的引擎；引入 `ort` 作为 GPU 加速引擎**。因为本项目 `ferroocr-core` 本来就要做引擎适配层，接入第二个引擎的成本远低于改造 rusto-nn 的推理内核。

**具体做法**：
1. `ferroocr-core` 定义 `OcrEngine` trait（det/rec 两组会话的封装），下面挂 `RustoEngine` 与 `OrtEngine` 两个实现。
2. 模型侧统一走 **ONNX**（PaddleOCR 官方有导出路径，RapidOCR 已验证）；`.rten`/`.mnn` 仅供 RustoEngine 用。
3. `--engine auto|cpu|gpu`：`cpu` 用 rusto-nn；`gpu` 按平台选 EP 链（Win: DirectML→CPU；N卡: CUDA→DirectML→CPU；mac: CoreML→CPU；Linux: CUDA→WebGPU→CPU）。
4. `ort` 的 `EP...build().error_on_failure()` 可精确控制"必须用 GPU 还是允许回退"，配合 §11.3 的设备自检。

#### 9.4b.4 这个选择对 PRD 的影响（需业务方确认）

| 影响项 | 若采纳路线 O |
|---|---|
| **交付物** | 从"单一 2.1 MB 纯 Rust 二进制"变成"**双引擎**"：`-slim`（纯 Rust CPU，2–5 MB）与 `-gpu`（含 ORT+DirectML，约 40–60 MB）两个发行版。**"纯 Rust 零依赖"作为 CPU 版的卖点仍然成立** |
| **里程碑** | M3 从"改内核 5–7 周"变成"**接 ort + DirectML 2–3 周**"，且 2–3 s 目标风险大幅下降 |
| **总工期** | 从 14–18 人周降到 **10–13 人周** |
| **风险** | 从"能不能做出 2×"变成"ORT 的 Paddle 模型导出与精度对齐"（这是已知有解的问题，RapidOCR 已跑通） |
| **依赖许可** | ORT 是 MIT；DirectML 随 ORT 分发（需核对微软许可条款）；PaddleOCR 权重 Apache-2.0 |
| **§9.4 的 A/B/C/D** | **不再是 v1.0 必做项**，降为"若要做纯 Rust GPU 后端时的长期课题"，可放 v2.0 |

> **一句话结论**：你说的这条路（ORT + DirectML）在"免装 CUDA + 覆盖各种显卡 + 快速达标 2–3 秒"三个维度上**都优于**我上一轮设计的"自研 CUDA 后端"。上一轮的方案把"纯 Rust"当成了不可让渡的约束，反而把工程风险推到了最高。**建议改为双引擎：CPU 走纯 Rust（保留轻量卖点），GPU 走 ort+DirectML。** 这正是需要你拍板的架构决策（§14 Q11）。

---

### 9.5 "不需要客户装 CUDA" —— 若走 CUDA 路线的边界说明

业务方明确要求：**使用 GPU 识别时，客户设备不需要安装 CUDA**。这一条在技术上成立，但要把边界说清楚。

#### 9.5.1 关键区分：驱动 ≠ Toolkit

| | NVIDIA **驱动**（Display Driver） | **CUDA Toolkit**（SDK） |
|---|---|---|
| 是否必须 | ✅ **必须**（没有驱动 GPU 不可用） | ❌ **不需要** |
| 体积 | 通常随显卡/系统已装（几百 MB，绝大多数 N 卡机器自带） | 2–3 GB，含 `nvcc` 编译器、头文件、开发库 |
| 装了什么 | `nvcuda.dll`(Win) / `libcuda.so`(Linux) = **驱动 API** | 额外带 `cublas`、`cudnn`、`nvrtc` 等**开发期库** |

**「不装 CUDA」在用户视角通常指"不装那 2–3 GB 的开发包"**——这个完全可以做到。

#### 9.5.2 本项目采用的技术路线（`cudarc` 动态加载）

上游 `rusto-nn-cuda` 用的 `cudarc 0.19.10` 配置（`crates/rusto-nn-cuda/Cargo.toml`）正好是我们要的：

```toml
cudarc = { version = "0.19.10", default-features = false,
           features = ["std", "driver", "cublas",
                       "fallback-dynamic-loading", "cuda-12000"] }
```

- **`dynamic-loading` 是 cudarc 的默认模式**，官方 README 原文：*"By default we use `-F dynamic-loading`, which will not require any libraries to be present at build time."*
- 运行时通过 `libloading` 动态查找 `nvcuda.dll` / `libcuda.so.1` / `libcuda.so`，**不需要编译期 CUDA SDK，也不需要用户装 Toolkit**。
- 上游 `is_cuda_driver_available()` 就是干这个的（`crates/rusto-nn-cuda/src/lib.rs:16-29`，试 4 个候选库名）。

#### 9.5.3 但有两个坑必须处理（否则"零安装"会破功）

| # | 风险 | 说明 | 对策 |
|---|---|---|---|
| 1 | **`cublas` 不在驱动里** | `libcuda.so` 是驱动 API（驱动自带），但 **`cublas` 属于 Toolkit 的一部分**。`dynamic-loading` 只是"编译期不需要"，**运行期仍要能找到 `cublas64_12.dll` / `libcublas.so.12`**。干净的 Windows 机器只有驱动时，可能**没有 cublas**。 | ①启动时用 `libloading` 探测 `cublas` 是否存在，缺失时给出**明确可操作提示**（"检测到 NVIDIA 驱动但缺少 cuBLAS，请安装 CUDA Toolkit 的 runtime 部分或使用 -cuda-bundled 镜像"）；②提供 **`-cuda-bundled` 镜像/发行包**，把 cuBLAS/cuDNN 的 runtime 库随包分发（NVIDIA 允许随应用分发这些 runtime，需遵守 EULA）；③**v1 的 GEMM 也可改用纯 Rust 的 CUDA kernel 或 WebGPU 兜底**，绕开 cuBLAS 依赖 |
| 2 | **核显/AMD/Intel 机器没有 CUDA** | 不能假设客户都有 N 卡 | `device: auto` 探测顺序 CUDA → WebGPU → CPU；**无 N 卡时静默走 CPU，不报错**（G2b） |

> **结论**：`cudarc` 让我们做到了「编译零依赖 + 用户不装 Toolkit 也能跑（前提是机器有驱动+cublas）」。但 **cuBLAS 的运行期存在性必须显式探测**，这是"零安装体验"能否真正成立的关键，列为 M3 的 P0 验收项（§11.3 G6）。

---

## 10. 里程碑与版本规划

| 里程碑 | 内容 | 出口标准（DoD） |
|---|---|---|
| **M0 · 骨架（1 周）** | workspace 拆分、`ferroocr-core` 封装上游、`ferroocr ocr`（等值于上游 CLI）、CI（fmt/clippy/test/deny/audit） | 单图 OCR 结果与上游 CLI **逐字符一致** |
| **M1 · CPU 服务 MVP（2 周）** | `serve`（axum）+ `/v1/ocr` 同步 + 引擎池 + 配置/日志/健康检查 + 错误模型 + **跑基线并回填 §8.1** + Dockerfile(cpu) | 4 并发稳定跑通；p50 达成 §8.1 定值（以本机基线为准）；镜像 ≤ 120 MB；OpenAPI 可访问；**基线报告归档** |
| **M2 · 批量与任务（2 周）** | `/v1/jobs` 异步 + 持久化 + 进度/取消/重试 + batch 接口 + NDJSON 流式 + 限流背压 | 1 万张批量任务跑完无泄漏；kill -9 可续跑；取消 500 ms 内生效 |
| **M3 · GPU 双路支持（P0）** | **见下方两套方案，取决于 §14 Q11 的架构决策** | **N 卡独显/任意 DX12 显卡上 A4 整页端到端 ≥2× 加速（CPU 5 s → GPU ≤2.5 s）**；客户零安装；无卡自动回退；精度逐字符一致率 ≥99.9% |
| **M4 · 文档理解与输出（2 周）** | layout/order/unwarp/table 暴露 + preset + markdown/hocr/alto + 表格 schema 稳定 | 版面+阅读顺序在样本集上优于"纯 1D 排序"（对齐上游 57%→64% 的结论） |
| **M5 · 生产化（2 周）** | 指标面板、doctor、多 key 鉴权、镜像加固、K8s/Compose 示例、压测报告、发布 v1.0.0 | 7×24 h 压测通过；安全清单全绿；文档齐备 |
| **v1.1（规划）** | 多设备多进程（CPU+GPU 同机）、PDF 全页、mmap 共享权重、OTLP | — |
| **v2.0（远期）** | 纯 Rust GPU 后端（§9.4 A/B/C/D）、分布式、模板抽取、searchable PDF | — |

**M3 的两套方案（推荐 O）**

| | **方案 O（推荐）：`ort` + DirectML** | **方案 R：rusto-nn 自研 GPU 后端** |
|---|---|---|
| 内容 | ①`ferroocr-core` 抽象出 `OcrEngine` trait；②引入 `ort`，PaddleOCR 模型走 ONNX；③EP 链：Win `DirectML→CPU`、N卡 `CUDA→DirectML→CPU`、mac `CoreML→CPU`、Linux `CUDA→WebGPU→CPU`；④设备探测 + `--engine auto/cpu/gpu` + 回退；⑤`-gpu` Dockerfile | ①从零接线（上游根包无 `register_backend()`）；②设备探测 + `--device`；③**§9.4 A/B/C/D 四项内核改造**；④cuBLAS 探测；⑤`-cuda` Dockerfile |
| 工期 | **2–3 周** | **5–7 周**（含 4–5 周内核调优） |
| 风险 | 低（ORT/DirectML 是生产级成熟后端，RapidOCR 已验证 Paddle→ONNX→ORT 路线） | 高（要改上游推理内核 + 调 CUDA kernel） |
| 硬件覆盖 | Win 上**全部 DX12 显卡**（N/AMD/Intel）+ mac + Linux | 仅 N 卡 + WebGPU 兜底 |
| 用户安装 | **零安装**（DirectML dylib 随包） | N 卡需驱动 + cuBLAS（可能需补装，见 §9.5.3） |
| 代价 | 引入 C++ 动态库（ORT ~10–15 MB + DirectML ~10–20 MB）；失去"单文件 2.1 MB"的极致体积 | 保持 100% 纯 Rust |
| 应对体积 | 发两个版本：**`-slim`（纯 Rust CPU，2–5 MB）** 与 **`-gpu`（含 ORT，约 40–60 MB）** | 单一版本 |

**估算**：
- 若采纳**方案 O**：总计约 **10–13 人周**
- 若坚持**方案 R**：总计约 **14–18 人周**，且需 N 卡开发机 + CUDA kernel 经验

> ⚠️ **方案 R 的排期风险**：M3 的 A/B/C/D 属于**改上游推理内核**（`rusto-nn` 的 kernels 与 backend），不是"接个 API"。若团队没有 CUDA kernel 优化经验，建议拆成 M3a（接线 + 实测基线，1–2 周）与 M3b（性能改造，4–5 周），M3a 结束先出「当前 GPU 到底几倍」的实测报告，再决定 M3b 投多少。
>
> **方案 O 的风险点**：PaddleOCR → ONNX 的模型导出与精度对齐（含 `rec` 的字典/CTC 解码一致性）、以及 rusto 与 ort 两条路径的输出对齐。这部分有 RapidOCR / `retto` / `oar-ocr` 的成熟先例可参考。

---

## 11. 验收标准（可直接转测试用例）

### 11.1 功能验收（v1.0）

| # | 用例 | 通过条件 |
|---|---|---|
| A1 | `ferroocr models pull --tier indra --offline` 在无网环境 | 缺模型时立即报错（exit 2，含缺失清单），不尝试联网 |
| A2 | `POST /v1/ocr` 传 A4 JPEG | 200，`lines` 数量与上游 CLI 一致，`text` 逐字符一致 |
| A3 | 传 30 MB 图 | 413 `payload_too_large`，进程存活 |
| A4 | 传损坏 JPEG | 422 `decode_failed`，含 `request_id` |
| A5 | `device: "cuda:0"` 但无显卡 | 400/503 且 `detail` 说明原因；不静默降级为 cpu（除非 `fallback=true`） |
| A6 | 并发 64 个 `/v1/ocr` | 无 5xx（除超限 429/503）；无 panic；RSS 增幅 < 30% |
| A7 | 提交 1000 图任务 → 中途 cancel | 状态 `cancelled`，已完成项可读取，未完成项标 `skipped` |
| A8 | 任务执行中 `kill -9` 后重启 | 任务从断点继续，重复项不重复计费/计数 |
| A9 | `/v1/models/{id}/activate` 切到 `fast` | 后续请求生效，切换窗口内请求 100% 成功 |
| A10 | 篡改模型文件一字节 | 启动/加载失败，错误信息包含期望/实际 SHA-256 |
| A11 | `--bind 0.0.0.0` 无 `--allow-remote` | 拒绝启动并提示 |
| A12 | `image_url` 指向 `http://127.0.0.1:22` | 请求被拒（SSRF 白名单拦截） |

### 11.2 性能验收（v1.0，基准机见 §8.1）

| # | 指标 | 通过条件 |
|---|---|---|
| P1 | 单页 A4/40 行 `indra` p50 | ≤ §8.1 表定值（M1 校准后的值），连续 200 次，首 20 次预热剔除 |
| P2 | 吞吐 | 4 worker ≥ §8.1 表定值 |
| P3 | 24 h 压测 | RSS 漂移 < 5%，无泄漏，无未归还实例 |
| P4 | 冷启动 | ≤ 5 s（模型缓存命中） |
| P5 | 内存 | 200 张大图连续处理 RSS ≤ 2.5 GB |
| P6 | **GPU 加速比**（N 卡独显） | 同图同模型，A4 整页 p50：CPU ≈5 s → **GPU ≤2.5 s（≥2×）**，连续 100 次，剔除首次（CUDA 上下文初始化） |

### 11.3 GPU 门禁（M3，必须出报告）

| # | 指标 | 通过条件 |
|---|---|---|
| G1 | 设备探测正确性 | `/v1/devices` 与 `nvidia-smi` 一致；无卡时报告为空且服务可用 |
| G2 | 回退可靠性 | 注入 GPU 失败（`CUDA_VISIBLE_DEVICES=""` / 强制 mock 错误）后 100% 走 CPU，`ferro_fallback_total` 有计数 |
| G2b | **自动判废** | 启动时跑 5 张基准图做 GPU/CPU 对照，若 GPU 中位数慢于 CPU，**自动禁用 GPU 并记录原因**（避免在核显上白白变慢 21%） |
| G3 | **性能对照** | 同机同图同模型，GPU vs CPU 端到端 p50/p95 与吞吐实测值。**基准集必须包含至少一张密集页（≥150 行）**——上游正是在这类图上海 GPU 输了 21%。报告不做硬门槛，但结论必须写清适用边界（核显/独显分别给结论） |
| G4 | 精度等价 | GPU 与 CPU 输出文本逐字符一致率 ≥ 99.9%（浮点差异可容忍，需给出 diff 统计） |
| G5 | 显存 | 峰值显存 ≤ `--gpu-mem-budget`；超限时优雅降级而非 OOM |
| G6 | **零安装体验** | 在**只装了 NVIDIA 驱动、未装 CUDA Toolkit** 的干净机器上：①有 cublas 时能直接用 GPU；②缺 cublas 时给出明确可操作提示（而非隐晦 panic）；③无 N 卡时静默回退 CPU 且功能完整 |
| G7 | **独显加速达标** | N 卡独显（≥ GTX 1660 级别）上，A4 300DPI 整页端到端：CPU ≈5 s 的场景 GPU ≤2–3 s（**≥2×**）；达不到则必须在发布说明中如实标注适用硬件 |

---

## 12. 风险与对策

| ID | 风险 | 影响 | 概率 | 对策 |
|---|---|---|---|---|
| R1 | **GPU 拿不到 2× 加速**：rusto-nn 自研后端只加速 GEMM，核显实测 -21%。**改用 `ort` + DirectML 可基本规避此风险**（ORT 的 GPU kernel 全覆盖） | 高（核心需求落空） | **低（若走方案 O）/ 中（若走方案 R）** | **优先方案 O**（§9.4b）；若走方案 R 则 A/B/C/D 改造是必经路径，且需 M3a 先出实测报告 |
| R1c | **cuBLAS 运行期缺失**导致"零安装"破功（仅方案 R 的 CUDA 路径）：`dynamic-loading` 只解决编译期，运行期仍需 `cublas64_12.dll`，而它不在驱动里 | 中 | 中（方案 R）/ **不适用（方案 O 的 DirectML）** | **DirectML 路线天然规避**；若走 CUDA 则启动探测 + 明确提示（§11.3 G6）或随包分发 |
| R1d | **方案 O 引入 C++ 依赖**，失去"纯 Rust 单文件 2.1 MB"卖点；且 ORT/directml dylib 需随包分发，容器不能再用 scratch（需 glibc） | 中 | 高（若选方案 O） | 发双版本：`-slim`（纯 Rust，2–5 MB）与 `-gpu`（含 ORT，40–60 MB）；文档明确差异；CPU 版继续保留纯 Rust 卖点 |
| R1e | **方案 O 的模型对齐风险**：PaddleOCR → ONNX 导出后，rec 的字典/CTC 解码需与 rusto 路径输出对齐，否则两条路径结果不一致 | 中 | 中 | 用 `ferroocr-bench` 做双引擎差分测试（同图同模型，逐字符比对）；参考 RapidOCR 的导出脚本 |
| R1b | **"GPU 支持"在本项目是从零接线**：`src/lib.rs`/`src/cli.rs` 无 cuda/webgpu 命中，无 `register_backend()` 调用，CLI 无 `--device` | 中（工期误判） | **确定发生** | M3 工期按"新接一个后端"而非"打开开关"估算；先做 CPU 打通再接线 |
| R2 | 上游 `rusto-rs` 迭代快（v0.3→v0.4 破坏性变更），**且 crates.io 只到 0.3.2、仓库已到 0.4.0** | 高 | **高** | 初期用 `git tag = "v0.4.0"` 或 vendor；`ferroocr-core` 做**唯一适配层**（只有它接触上游 API）；升级跑双门禁（精度+性能）；向上游提 PR 减少自己 fork |
| R2b | `rusto-nn-cuda` **未发布到 crates.io**，GPU 后端未走发布验证 | 中 | 高 | 用 path/git 依赖并自建构建流水线；GPU 版单独发布标签；在 README 明确标注 experimental |
| R3 | 模型许可/商用边界不明（PaddleOCR 模型、Indra OCR、MNN 格式） | 高（法务） | 中 | 立项前做许可尽调：PaddleOCR 为 Apache-2.0、Indra OCR 需核实、模型权重二次分发需确认；文档中给出"各档位许可矩阵"，提供替换通道 |
| R4 | 引擎不 `Sync` + 权重每实例一份 → 内存随 worker 线性增长 | 中 | 高 | 池大小可配；`--max-ram` 预算；v1.1 mmap 共享权重（`memmap2` 已在依赖中） |
| R5 | `rayon` 全局池被并发请求争抢，p99 抖动 | 中 | 高 | 专用运行时/线程池隔离；请求准入控制；实测校准 `workers×threads` |
| R6 | 大批量任务结果占满磁盘 | 中 | 中 | 结果 TTL + 容量配额 + 分片；磁盘阈值保护 |
| R7 | 精度对特定业务（票据/手写）不够 | 中 | 中 | 明确"擅长印刷体文档"；提供多档位与字段级二次校验建议；不承诺 SOTA |
| R8 | CUDA 驱动版本碎片化（`cudarc` 动态加载失败） | 中 | 中 | 启动自检 + 明确报错 + 回退；文档列出驱动最低版本 |
| R9 | 服务端 `panic=abort` 导致整进程崩溃，且**上游自己的 CUDA 初始化依赖 `catch_unwind`**（`rusto-nn-cuda/src/lib.rs:45`），在 abort 下该保护失效 | 高 | **确定发生** | 本项目 profile 覆盖为 `panic="unwind"`；关键路径 `catch_unwind`；加 `--profile release-server` |
| R10 | 团队不熟 Rust 异步/推理调优，进度超期 | 中 | 中 | M0/M1 先交付可运行闭环；GPU 后置且可裁剪 |
| R11 | **上游 benchmark 口径不可信**：①`BASELINE_LATENCY.md:6` 用 `opt-level=3` 测，而 `Cargo.toml:117` release 是 `opt-level="z"`；②各文档同一 pipeline 数字差异数倍（MNN C++ 479 ms vs 39.7 ms，疑 pipeline/纯推理混用） | 中（SLA 定错） | **确定发生** | SLA 全部改为自建 `ferroocr-bench` 复测口径（§8.1）；文档引用上游数字必须标注来源文件与测试 profile；对外不引用未复测数字 |
| R12 | WebGPU 在浏览器端被硬编码禁用（`rusto-nn-webgpu/src/lib.rs:304-310`），"浏览器也能 GPU 加速"不可宣传 | 低 | **确定发生** | 只宣传 native GPU；wasm 场景说明仅 CPU SIMD |
| R13 | **上游下载器依赖外部命令**（`src/download.rs:8-63` 调 `curl`/`wget`/`powershell`），而目标镜像是 distroless/scratch，无这些命令 → 首次启动拉模型必失败 | 中 | **确定发生** | 下载模块自研（`reqwest`+`rustls`）；镜像策略二选一：①预置模型+`--offline`；②带 curl 的精简基础镜像 |
| R14 | 上游**无 SHA-256 校验、无断点续传、无代理配置**（`download.rs` 全文无 `sha256/checksum/digest` 命中） | 中（供应链） | **确定发生** | 本项目 F-4.4 自建校验 + `models.lock.json`；加代理与断点续传 |

---

## 13. 依赖与许可

| 依赖 | 许可 | 用途 | 注意 |
|---|---|---|---|
| `rusto-rs` / `rusto-core` / `rusto-nn` | MIT | 引擎（CPU 路径） | 需在 NOTICE 中保留版权与致谢（Indra/PaddleOCR/RTen/MNN） |
| **`ort`**（ONNX Runtime bindings） | **MIT** | **GPU 路径（推荐）** | ORT 本体 MIT；但**随包的 DirectML.dll 需核对微软许可**（DirectML 为微软产品，允许随应用分发，具体条款需法务确认）；ORT 二进制体积约 10–15 MB |
| **DirectML.dll** | 微软专有（可再分发） | Windows 全显卡 GPU 加速 | ⚠️ **必须核对 EULA 的再分发条款**，这是方案 O 唯一需要法务确认的点 |
| `rusto-nn-cuda`（`cudarc`） | MIT/Apache-2.0 | CUDA 后端（方案 R） | CUDA 运行时为 NVIDIA 专有，分发镜像需符合 NVIDIA EULA；建议镜像**不带** CUDA runtime，由驱动提供 |
| `wgpu` | MIT/Apache-2.0 | WebGPU | 跨平台 GPU 兜底。⚠️ **上游锁定 `wgpu = "24"`（`rusto-nn-webgpu/Cargo.toml`），当前最新为 30.0.1** —— 若本项目其他依赖需要 wgpu 30 会与上游冲突（同一 workspace 只能有一个 wgpu 大版本）。立项时需确认：接受 wgpu 24，或推动上游升级 |
| `axum`/`tokio`/`tower` | MIT | 服务框架 | — |
| 模型权重 | **待核实** | PP-OCRv6/v5/v4（PaddleOCR 系）、Indra OCR | 必须逐档给出许可与商用结论；参考竞品做法：镜像不内置争议模型，改为首次运行下载 |
| `sqlite`（bundled） | Public Domain | 任务持久化 | — |

**交付物合规清单**：`LICENSE`(MIT) + `NOTICE`（上游致谢）+ `MODELS.md`（模型许可矩阵）+ `SBOM`（`cargo cyclonedx`）+ `cargo deny` 配置。

---

## 14. 待确认问题（需业务方拍板）

| # | 问题 | 影响 | 建议默认 |
|---|---|---|---|
| Q1 | **主要语言场景**？中文为主 / 多语种 / 仅英文？ | 决定默认模型档位与语言包体积 | 中文+英文（`ch` 默认，`world` 可选） |
| Q2 | **GPU 目标硬件**？已确认要支持 GPU 且"客户免装 CUDA"。剩余需确认：①客户机器主要是 N 卡独显（GTX 1660/RTX 系列）还是含大量核显/AMD？②最低支持到哪一代 N 卡？③是否需要多卡？④能否提供一台 N 卡开发机做 kernel 调优？ | 决定 M3 的投入与验收基准；无真卡无法验证 2× 目标 | N 卡独显优先（CUDA），WebGPU 兜底 AMD/Intel/Apple；开发机必须落实 |
| Q2b | **"不需要装 CUDA"的边界**：可接受"用户需有 NVIDIA 驱动 + 我们随包分发 cuBLAS runtime"，还是连这个都不允许（那必须改用 WebGPU 或自研 kernel 绕开 cuBLAS）？ | 决定是否做 `-cuda-bundled` 分发、EULA 合规、以及是否要自研 GEMM kernel | 随包分发 cuBLAS runtime（业界通行做法），同时提供 WebGPU 兜底 |
| Q3 | **部署形态**？单机二进制 / Docker / K8s / 嵌入自家 Rust 服务？ | 决定配置与交付优先级 | Docker 优先 + 单二进制 |
| Q4 | **PDF 是否必须**？渲染引擎选型（外部依赖 vs 纯 Rust）？ | 影响 v1 范围 | v1 支持 PDF 内嵌图片；v1.1 全页栅格化 |
| Q5 | **吞吐量级**？日均张数 / 峰值 QPS / 单次最大批量？ | 决定队列、池大小、是否需要分布式 | 峰值 10 QPS、单批 ≤ 1 万 |
| Q6 | **精度验收集**？是否有业务标注集（发票/合同/工单）？ | 决定门禁能否落地 | 先用 200 张自建样本 + OmniDocBench 子集 |
| Q7 | **是否允许 fork 上游**做 GPU 改造？ | 决定 R1 的解法 | 允许，但改动通过 PR 回流，保持 diff 最小 |
| Q8 | **商用与开源策略**？本项目是否开源（MIT/Apache-2.0）？ | 影响命名/商标/许可 | 开源（Apache-2.0 更利于企业采用） |
| Q9 | **API 契约兼容目标**？调研发现三套互不兼容的"事实标准"：PaddleOCR HubServing（`{status:"0",msg,results:[[{text,confidence,text_region}]]}`）、Umi-OCR（`{code:100,data:[{text,score,box,end}],time}`）、RapidOCRAPI（**无信封**，顶层 `{"0":{rec_txt,dt_boxes,score}}`）。要不要做兼容层？ | 决定是否加兼容层与工作量 | 自研 schema 为主 + v1.1 提供 `/v1/compat/paddleocr` 与 `/v1/compat/umi` 两个转换层（`end` 字段可无损还原段落，值得借鉴） |
| Q10 | **是否要 Web 可视化调试台**（上传图片看框选结果）？ | 影响工作量 | v1.1 提供单页静态 HTML 调试台 |
| **Q11** | **【架构决策·最关键】GPU 走哪条路线？** 方案 O：`ort` + DirectML（零安装、覆盖 N/AMD/Intel 全显卡、2–3 周）vs 方案 R：rusto-nn 自研 CUDA 后端（纯 Rust、5–7 周、仅 N 卡、可能还要补 cuBLAS）。代价是方案 O 会引入 C++ 动态库（约 20–35 MB），失去"单文件 2.1 MB"的极致体积。 | **决定 M3 的性质、总工期（10–13 vs 14–18 人周）与整个产品形态** | **方案 O**（双引擎：CPU 纯 Rust + GPU 走 ort/DirectML），并保留 `-slim` 纯 Rust 发行版 |
| Q12 | **是否接受"双发行版"**（`-slim` 纯 Rust 2–5 MB / `-gpu` 含 ORT 40–60 MB）？还是必须单一二进制？ | 影响打包、文档、用户选择成本 | 接受双发行版；`-gpu` 为默认推荐，`-slim` 供嵌入式/极致体积场景 |

---

## 15. 附：与上游的最小使用示例（供实现参考）

```rust
// ferroocr-core 内部的封装示意
use rusto::{DetectTextBuilderExt, ImageSource, InitializeConfig, RustO, OcrRunOptions};

pub struct Engine { inner: RustO }

impl Engine {
    pub fn new(models: &ModelPaths) -> anyhow::Result<Self> {
        let cfg = InitializeConfig::indra(&models.det, &models.rec, &models.dict);
        Ok(Self { inner: RustO::initialize(cfg)? })
    }

    /// 注意：`&mut self` —— 引擎非 Sync，必须独占使用（由池保证）
    pub fn run(&mut self, img: ImageSource, opts: OcrRunOptions) -> anyhow::Result<DetectTextResult> {
        self.inner
            .detect_text_builder(img)
            .with_options(opts)
            .run()
            .map_err(Into::into)
    }
}
```

```bash
# 服务
ferroocr serve --bind 127.0.0.1:8600 --device auto --workers 4 --model indra
# 识别
curl -sS -X POST http://127.0.0.1:8600/v1/ocr \
  -H 'content-type: application/json' \
  -d '{"image_url":"file:///samples/invoice.jpg","preset":"receipt","output":{"format":"json","granularity":"lines"}}' | jq .
# 设备
curl -sS http://127.0.0.1:8600/v1/devices | jq .
```

---

**文档结束** · 版本 v0.1 · 待评审后冻结为 v1.0-rc
