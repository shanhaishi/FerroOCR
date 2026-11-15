# ADR-003 · GPU 走 ORT + DirectML 双引擎，不自研 CUDA 后端

| 项 | 内容 |
|---|---|
| 状态 | **已采纳**（2026-11-11），推翻同日更早的自研 CUDA 方案 |
| 影响范围 | PRD §3 G2/G6、§5.6 设备需求、§5.8、§6 体积预算、§7.1 工程结构、§11 复用边界 |
| 关联 | [ADR-002](ADR-002-l2-http-默认开启与单包.md)（单包体积由此决定） |

## 背景

需求里有两条并存的约束：

1. CPU 识别在 A4 全页场景要 5 秒以上，GPU 要能到 2–3 秒量级，所以 GPU 必须支持；
2. **客户设备不能要求安装 CUDA。** 这一条把"零安装"从体验问题变成了硬约束——如果用户为了用 GPU 要先装几个 GB 的 Toolkit，那这条路等于不存在。

上游 `rusto-rs` 的现状（逐文件核实）：

- `crates/rusto-nn/src/backend.rs:33` 是 `static ACTIVE_BACKEND: RwLock<Option<Arc<dyn DeviceBackend>>>`，一个**进程级单例**；而 `DeviceBackend` trait 只有 `gemm()` 一个方法。
- `crates/rusto-nn/src/kernels/conv.rs` 与 `depthwise.rs` 里 `active_backend` 出现 **0 次**——卷积完全没接后端。
- `kernels/gemm/mod.rs:38-44` 的 GPU 分派门槛是 `m*n*k >= 65536`，且 `backend.gemm(...).is_ok()` 失败时**静默回退**，无日志。

也就是说：即便把上游的 CUDA 后端接上，能加速的也只有满足门槛的 GEMM，卷积仍在 CPU 上跑，且真上 GPU 的设备利用率很低。

## 曾考虑的方案：自研 CUDA 后端（已放弃）

沿上游的 `DeviceBackend` 扩展，把 conv/depthwise 也搬到 GPU。

**放弃的两条原因：**

1. **运行期依赖仍在。** `cudarc 0.19.10` 默认启用 `dynamic-loading`，确实做到了「编译期不需要任何库存在」（其 README 原文：*By default we use `-F dynamic-loading`, which will not require any libraries to be present at build time*）。但 **cuBLAS 属于 CUDA Toolkit 的运行时，不在显卡驱动里**。缺 `cublas64_*.dll` 时这条路直接破功——「不用装 CUDA」的承诺是假的。
2. **要动上游内核。** 把 conv/depthwise 接上 GPU 需要改 `rusto-nn` 的 kernel 分发、绕开进程级单例、并重写 `gemm` 之外的部分，估算 5–7 人周，且需要 N 卡 CI 才能验证。

## 决定

**双引擎，同一个 exe 内共存，用 `--device` 切换：**

| 引擎 | 实现 | 适用 | 依赖 |
|---|---|---|---|
| `engine-rusto` | 上游纯 Rust 计算，CPU | 默认；无独显或不想用 GPU 时 | 无 |
| `engine-ort` | ONNX Runtime + DirectML EP | `--device gpu` | 内嵌的 `onnxruntime.dll` + `DirectML.dll` |

关键事实（已核实 `ort 2.0.0-rc.15`）：features 里含 `directml = ["ort-sys/directml"]`，另有 `cuda` / `tensorrt` / `openvino` / `coreml` / `webgpu`。官方文档写明 DirectML 与 WebGPU **不需要 CUDA Toolkit**（只有 helper dylib），并明确 EP 链的行为：*"Ort will register all EPs specified, in order. If an EP does not support a certain operator... it will fall back to the next successfully registered EP, or to the CPU"*——**算子不支持时自动回退 CPU**，我们不必自己写回退逻辑。

## 为什么这个方案更优

- **覆盖面更大**：DirectML 走 DX12，Windows 上所有 DX12 显卡都能用，包括 N 卡、A 卡、Intel 核显，不止 CUDA 生态。
- **真正零安装**：ORT 与 DirectML 的 dylib 内嵌进 exe，首次用 GPU 时才释放到 `%LOCALAPPDATA%\FerroOCR\runtime\<ver>\`（带 SHA-256 校验）。用户侧不需要装任何东西。
- **不碰上游内核**：ORT 使用 ONNX 模型独立执行，与上游的 `ACTIVE_BACKEND` 单例互不干扰——**这正是「一个 exe 能同时具备两条路径」的技术前提**。
- **工期更短**：方案估算 2–3 人周，总工期 10–13 人周；自研路线 5–7 人周、总工期 14–18 人周。

## 后果

- **单包体积从约 15 MB 涨到 ≤60 MB**，这是 GPU 支持的代价（ORT 主 dylib ~25–35 MB + `DirectML.dll` ~5–8 MB + ONNX 模型 5–10 MB）。PRD §6 因此拆成 CPU 档 / GPU 档两张体积表；CPU-only ≤15 MB 作为内部分发选项保留。
- 两条引擎路径**必须给出可核对的一致性**，验收标准里因此有一条：同一张图 `--device cpu` 与 `--device gpu` 的识别结果必须一致。否则双引擎会变成两套产品。
- PRD §11 的复用边界改为：**GPU 后端不复用上游**，与 CPU 引擎共存于同一 exe。
- 上游的 WebGPU 实测数据（`benchmarks/PURE_RUST_MNN_PERFORMANCE.md:46-52`，Intel Iris Xe 核显）显示核显上 GPU 反而更慢（162 行页面 CPU 1.44 s / GPU 1.75 s）。这解释了为什么 `--device` 默认值是 `auto` 而非 `gpu`：**核显场景应当留在 CPU 路径**。

## 教训

**不要为了保住一个技术标签（"纯 Rust"）而把工程风险推到最高。** 最初把"纯 Rust 后端"当成了不可让渡的约束，反而选了一条运行期依赖更重、覆盖面更窄、工期更长的路。约束应当来自用户能否零安装地用起来，而不是来自实现语言的纯度。
