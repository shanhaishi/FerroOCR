# rusto-rs 技术分析（FerroOCR 需求文档输入）

> 对象：[byrizki/rusto-rs](https://github.com/byrizki/rusto-rs) `main`，v0.4.0，MIT，127 stars
> 方法：GitHub Trees API（826 blob，`truncated=false`）+ raw/Contents API 逐文件直读，未克隆
> 规则：每条结论标注来源文件；没读到的写「未见相关实现」，不推测
> 复核日期：2026-11-10

---

## 0. 结论摘要（<200 字）

可直接复用的是一条约 12 000 行的**纯 Rust OCR 管线**（DBNet 检测 + CTC/LineNet 识别 + 方向/去畸变/版面/阅读顺序/表格）与一个**自研 MNN 图执行器**（`rusto-nn`，40 个算子、AVX-512/AVX2/NEON/WASM SIMD 全手写）；公开 API 是"一次初始化 + 每请求 Builder"形态，另有 12 种语言绑定。

**缺的是服务化的一切**：全仓库 0 行 HTTP/队列/并发池代码。GPU 更关键——`DeviceBackend` trait **只有 `gemm()` 一个方法**，conv/depthwise 零分派；上游 WebGPU 实测（Intel Iris Xe **核显**，算力仅 CPU 的 1.5–2×）**最好快 3%、最差慢 21%**；**N 卡独显与 CUDA 路径零端到端数据**；浏览器端 WebGPU 被硬编码禁用。

---

## 1. 仓库形态与构建参数

| 项 | 值 | 来源 |
|---|---|---|
| 版本 / 许可 | 0.4.0 / MIT | `Cargo.toml:6,9` |
| workspace 成员 | 10 个：`.`、`rusto-core`、`rusto-cpp`、`rusto-nn`、`rusto-nn-webgpu`、`rusto-nn-cuda`、`rusto-rten`、`rusto-mnn`、`rusto-wasm`、`rusto-node` | `Cargo.toml:2` |
| 根包产物 | `lib`（`cdylib`+`staticlib`+`rlib`），7 个 bin（`rusto`/`rusto-rten`/`rusto-mnn`/`rten-direct`/`mnn-direct`/`uniffi-bindgen`/`generate-c-headers`） | `Cargo.toml:22-82` |
| release profile | `opt-level="z"`、`lto="fat"`、`codegen-units=1`、`strip=true`、`panic="abort"` | `Cargo.toml:116-121` |
| HTTP 服务端依赖 | **无**（11 个 `Cargo.toml` 全量 grep `axum/actix/tokio/hyper/warp/tonic/rocket/poem/salvo/tide/tiny_http/tower`，命中 0） | 自建 grep |

**两个必须改的构建参数**（对服务致命）：
1. `panic = "abort"`（`Cargo.toml:121`）——一个 panic 打死整个服务进程，无法 `catch_unwind` 隔离单请求。上游自己的 CUDA 初始化就依赖 `catch_unwind`（`crates/rusto-nn-cuda/src/lib.rs:45`），在 abort 下失效。
2. `opt-level = "z"`（`Cargo.toml:117`）——体积优先而非速度优先，与 `benchmarks/` 里宣称的 `opt-level = 3`（`benchmarks/BASELINE_LATENCY.md:6`、`OPTIMIZATION_RESULTS.md:22`）不一致。**基准数字不是用发布 profile 测的**。

### 1.1 Feature 门控（`Cargo.toml:88-108`）

```toml
rten = ["dep:rusto-rten"]; mnn-cpp = ["dep:rusto-mnn"]
webgpu = ["dep:rusto-nn-webgpu"]; cuda = ["dep:rusto-nn-cuda"]
build-from-source = []; metal = []; coreml = []; opencl = []
vulkan = []; nnapi = []; hiai = []; qnn = []   # ← 全部空 stub
```

`metal/coreml/opencl/vulkan/nnapi/hiai/qnn` 是**空 feature**，打开零效果。写需求文档时不能用"rusto-rs 支持 Vulkan/Metal"这类表述。

---

## 2. 引擎抽象（`crates/rusto-core/src/engine.rs`，1424 行）

### 2.1 核心 trait

```rust
pub trait InferenceSession: Send {                    // :79 —— 注意：只有 Send，没有 Sync
    fn run(&mut self, input: ArrayD<f32>) -> Result<ArrayD<f32>, CoreError>;
    fn run_all(&mut self, input: ArrayD<f32>) -> Result<HashMap<String, ArrayD<f32>>, CoreError>;
    fn run_with_inputs(&mut self, inputs: HashMap<String, ArrayD<f32>>) -> ...;
    fn input_channels(&self) -> usize { 3 }
    fn is_linenet(&self) -> bool { self.input_channels() == 1 }
    fn run_linenet_rec(&mut self, _x: Array4<f32>, _widths: Vec<i32>) -> ... { Err(...) }
    fn try_clone(&self) -> Option<Box<dyn InferenceSession>> { None }   // :124 默认 None
}
```

`try_clone` 是唯一的"共享权重开新会话"机制，默认返回 `None`。`MnnSession` 实现了它（`engine.rs:506`，靠 `rusto_nn::Session` 的 `Clone`——克隆时新建 `GraphContext`、共享 `Arc<Model>`，见 `crates/rusto-nn/src/session/mod.rs:41`）。

### 2.2 后端选择机制（关键限制）

```rust
pub enum EngineBackend { MnnPure = 0, Rten = 1, MnnCpp = 2 }          // :309
static CURRENT_BACKEND: AtomicU8 = AtomicU8::new(0);                  // :315 进程级全局
pub fn set_active_backend(backend: EngineBackend);                    // :317
pub type SessionLoader = fn(&Path, &EngineConfig) -> Result<Box<dyn InferenceSession>, CoreError>;
static SESSION_LOADER: AtomicPtr<()>;                                 // :330 进程级单槽
pub fn register_session_loader(loader: SessionLoader);                // :334
```

`EngineSession::from_path`（`:346`）的解析顺序是**硬编码**的：
1. 若注册了 `SESSION_LOADER` → 先问它；它返回 `Err("UNSUPPORTED_FORMAT")` 才继续往下；
2. 否则退回 `MnnSession`（纯 Rust `rusto-nn`）。

**四个结论**：
- **后端只能在进程启动时选一次**，没有 per-request / per-tenant 切换能力。服务端要"CPU + GPU 共存"，只能多进程或多实例。
- **注册顺序决定一切**：`rusto_rten::register()` 与 `rusto_mnn::register()` 都调 `set_active_backend` + `register_session_loader`（`crates/rusto-rten/src/lib.rs:357`、`crates/rusto-mnn/src/lib.rs:16`），后注册的覆盖先注册的。根包 `src/lib.rs:278` 的 `init()` 就是固定顺序 `rten → mnn-cpp`。
- **`MnnSession` 名字有误导性**：它调 `rusto_nn::Model::from_file`，而后者按**文件头魔数**嗅探格式（`crates/rusto-nn/src/model.rs:69-95`：`RTEN` → rten loader；首字节 `0x08` → onnx loader；否则 mnn）。所以默认"纯 MNN 后端"实际能吃 `.mnn`/`.rten`/`.onnx` 三种格式。
- `EngineSession` 只有 `Mnn(Box<MnnSession>)` 和 `Custom(Box<dyn InferenceSession>)` 两个变体（`:340`）——RTen / MNN-C++ / 用户自定义全部走 `Custom`，没有类型级区分。

### 2.3 顶层门面

| 方法 | 位置 | 说明 |
|---|---|---|
| `RustO::initialize(InitializeConfig)` | `:765` | 加载 det+rec，构造 `OcrPipeline` |
| `with_engine_sessions(config, det, rec)` | `:772` | 注入已构造 session（做池化的入口） |
| `with_custom_engines(config, det, rec)` | `:827` | 注入自定义 `InferenceSession` |
| `detect_text(&mut self, &ImageSource, &OcrRunOptions)` | `:943` | 主入口，**`&mut self`** |
| `detect_text_rgb(pixels, w, h, stride, opts)` | `:975` | 零拷贝 RGB8 直入（服务端解码器直喂用） |
| `run_on_mat_with_options(&Mat, opts)` | `:1014` | 跳过解码 |

> 代码味道：`engine.rs:760` 定义 `pub struct RustO { pub pipeline: OcrPipeline }`，而 `pipeline.rs:169` 又定义 `pub type RustO = OcrPipeline;`，`core/lib.rs` 同时 `pub use engine::*` 与 `pub use pipeline::*`。同名导出靠 glob 优先级消解，属于历史遗留，接入时注意用限定路径。

---

## 3. 配置体系（`crates/rusto-core/src/config.rs`，1497 行 + `types.rs`）

### 3.1 预设（`ModelPreset`，`config.rs:10-91`）

| 常量 | det_limit_side | limit_type | det_thresh | box_thresh | unclip | dilation | rec_shape | **rec_batch_num** |
|---|---|---|---|---|---|---|---|---|
| `INDRA_MODEL_CONFIG`（默认） | 1024 | max | 0.5 | 0.0 | 2.0 | true | `[1,96,320]` | **1** |
| `PPV6_MODEL_CONFIG` | 736 | min | 0.3 | 0.6 | 2.0 | true | `[3,48,320]` | 6 |
| `PPV5_MODEL_CONFIG` | 736 | min | 0.3 | 0.5 | 2.0 | true | `[3,48,320]` | 6 |
| `PPV4_MODEL_CONFIG` | 960 | max | 0.3 | 0.6 | 1.5 | false | `[3,48,320]` | 6 |
| `PPV3_MODEL_CONFIG` | 960 | max | 0.3 | 0.6 | 1.5 | false | `[3,48,320]` | 6 |

`InitializeConfig::default()` = Indra + `"det.rten"/"rec.rten"/"meta.json"`（`config.rs:107-111`）。

### 3.2 `InitializeConfig`（`config.rs:95`）

```rust
pub struct InitializeConfig {
    pub det: DetConfig, pub rec: RecConfig, pub global: GlobalConfig,
    pub orient: Option<OrientConfig>, pub unwarp: Option<UnwarpConfig>,
    pub cls: Option<ClsConfig>, pub layout: Option<LayoutConfig>,
    pub calibration: Option<CalibrationOptions>, pub optimization: Option<OptimizationOptions>,
}
```

构造：`new/from_preset/ppv6/ppv5/ppv4/ppv3/indra`（`:834-947`）、`from_json/to_json`（`:951,1217`，nanoserde，camelCase **与** snake_case 双写兼容，见 `:113-173` 的 `_camel`/`_snake` 双字段）。另有约 40 个 `with_*` 链式 setter（`:1244-1473`）。

### 3.3 唯一与"运行时"相关的字段

```rust
pub struct EngineConfig {                    // types.rs:71
    pub intra_op_num_threads: i32,           // 默认 4
    pub inter_op_num_threads: i32,           // 默认 1
    pub enable_cpu_mem_arena: bool,          // 默认 true
}
```

- **只有线程数，没有 device / backend / GPU 字段**。`EngineType` 枚举只有 `OnnxRuntime` 一个变体（`types.rs:51`），`ModelType` 只有 `Mobile`（`:56`）——纯占位，不参与任何逻辑。
- `intra_op_num_threads` 只被 RTen 后端真正消费（`crates/rusto-rten/src/lib.rs:348` 传给 `RtenSession::from_file`）；`MnnSession::from_path` 显式忽略它（`engine.rs:522` 参数名写作 `_engine_cfg`），**纯 Rust 后端完全不受此参数控制**，只能靠全局 `RAYON_NUM_THREADS`。
- 全仓库 `rusto-nn` 中 `ThreadPoolBuilder` / `num_threads` 命中 0 → 无自建线程池，全部吃 rayon 全局池。

`GlobalConfig`（`types.rs:433`）关键字段：`text_score`、`use_det/use_cls/use_rec/use_orient/use_unwarp/use_layout`、`reading_order` + `reading_order_model`、`debug_images`、`min_height`、`width_height_ratio`、`max_side_len`、`min_side_len`、`return_word_box`、`return_single_char_box`、`y/x_threshold_multiplier`、`calibration`、`optimization`。

`OptimizationOptions`（`calibration.rs:82`）只有 4 个字段：`enabled`、`target_max_side`、`crop_padding_x`、`crop_padding_y` —— 即"优化"仅指预缩放与裁切 padding，与推理性能无关。

---

## 4. 可复用能力清单

| 能力 | 对应 crate + 文件 | 成熟度 | 备注 |
|---|---|---|---|
| 文本检测 DBNet | `rusto-core/src/det.rs`、`preprocess.rs`、`postprocess.rs`、`contours.rs` | 成熟 | 上游测 det 单次 312 ms（1600×993） |
| 文本识别 CTC | `rusto-core/src/rec.rs`、`ctc.rs` | 成熟 | 贪心解码，`CtcDecoder::from_keys_file` / `from_model_character_list` |
| 识别 LineNet（Indra） | `rec.rs` + `engine.rs:113 run_linenet_rec` | 可用 | 1 通道灰度 + 动态宽度多输入，Indra 专用 |
| 180° 行方向分类 | `cls` 会话（`ppocr_cls`） | 成熟 | `GlobalConfig.use_cls` |
| 整页 0/90/180/270° 方向 | `rusto-core/src/orient.rs` | 成熟 | 双形态：1 通道 768×768（Indra）/ 3 通道 224×224（PP），见 `pipeline.rs:320-338` |
| 3D 去畸变 UVDoc | `rusto-core/src/unwarp.rs` | 可用 | 模型 30 MB，成本高 |
| 版面区域 PicoDet | `rusto-core/src/layout.rs` | 可用 | CDLA 10 类 / PubLayNet 5 类 |
| 神经阅读顺序 | `rusto-core/src/order.rs`、`order_blocks.rs` | 可用 | 精度 55%→64%（上游口径） |
| 表格结构 | `rusto-core/src/table.rs` | 实验 | 有 `to_html`/`to_markdown`，但无 CLI/API 暴露入口 |
| 空间对齐文本 / TSV | `rusto-core/src/spatial.rs` | 成熟 | `to_spatial_text`/`to_tsv` |
| 多列 unwarp / 去畸变后重排序 | `pipeline.rs`、`resample.rs` | 可用 | 与 layout 联动 |
| MNN 图执行器 | `crates/rusto-nn` | 成熟 | 40 算子，SIMD 手写 |
| 模型下载 + 缓存 | `src/download.rs`、`rusto-core/src/models.rs` | 可用 | **无 sha256 校验**、无断点续传 |
| RTen 后端 | `crates/rusto-rten` | 可用但慢 | OmniDocBench 12.0 s vs 默认 1.8 s |
| MNN C++ 后端 | `crates/rusto-mnn` | 可用 | 需 CMake + C++17 + `vendor/alibaba-mnn` 子模块 |
| CUDA 后端 | `crates/rusto-nn-cuda` | **实验** | 仅 GEMM，无 host 张量驻留 |
| WebGPU 后端 | `crates/rusto-nn-webgpu` | **实验** | 仅 tiled GEMM；wasm 上被禁用 |
| WASM 绑定 | `crates/rusto-wasm` | 可用 | `RtenModel` / `RtenOcrPipeline` |
| 多语言绑定 | `crates/rusto-node`、`packages/*`（12 套）、根包 UniFFI/C FFI | 可用 | — |
| **HTTP 服务 / 队列 / 并发池 / 可观测性** | —— | **不存在** | 本项目新建 |

---

## 5. CPU 推理现状

### 5.1 算子与内核覆盖（`crates/rusto-nn/src/kernels/`）

`kernels/mod.rs` 共 12 个模块：`activation/binary/classification/conv/deconv/depthwise/gemm/interp/norm/pool/reduction/tensor_ops/unary`。

`OpKind`（`graph/ir.rs:77-130`）共 **40 个枚举变体**，覆盖 Conv/Deconv/Pool/Binary/Unary/Relu/Relu6/Reduction/Interp/Reshape/Permute/Concat/Slice/StridedSlice/Squeeze/Unsqueeze/Scale/DepthToSpace/Flatten/Softmax/LogSoftmax/ArgMax/TopKV2/Pad/Shape/Rank/Range/Select/Gather/Cast/GridSample/MatMul/PRelu/ConstantOfShape/LayerNorm/Expand/ScatterND/ConvertTensor。

SIMD 分层：

| 文件 | 实现 | 备注 |
|---|---|---|
| `gemm/avx512.rs` | AVX-512 GEMM | 运行时 `is_x86_feature_detected!("avx512f")` |
| `gemm/avx2.rs` | AVX2+FMA GEMM | 含 B 矩阵预打包（`PACKED_B_CACHE`） |
| `gemm/neon.rs` | ARM NEON | `cfg(aarch64)` |
| `gemm/wasm.rs` | WASM SIMD128 | `cfg(all(wasm32, target_feature="simd128"))` |
| `gemm/fallback.rs` | 标量 | 兜底 + 单测基准 |
| `conv.rs`（790 行） | 1×1 直接走 GEMM；其余 im2col / im2col_strided / im2col_tile | `:96` 1×1 fast path |
| `depthwise.rs`（1877 行） | AVX2 手写 3×3 s1、3×3 s2、5×5 s1、5×5 s2、5×1、1d；NEON 3×3 s1；WASM 3×3 s1 / 1d | 逐 kernel 特化 |

**GPU 可达性（关键）**：
- `conv.rs` 有 5 处调用 `gemm(...)`（`:139,167,217,276,351`）→ **1×1 pointwise 卷积可被 GPU 加速**。
- `depthwise.rs` 中 `gemm` 出现 **0 次**、`active_backend` 出现 **0 次** → **depthwise 永远走 CPU**。
- MobileNet/LCNet 类骨干（PP-OCRv6 rec、Indra LineNet）**主要算力在 depthwise**（`benchmarks/OPTIMIZATION_RESULTS.md:58-61` 明确写 "PP-OCRv6 Tiny rec 有零个 3×3 标准卷积，全是 1×1 pointwise + 3×3/5×5 depthwise"）。这就是 GPU 收益被锁死的根因。

### 5.2 线程并行

| 位置 | 机制 | 上限 |
|---|---|---|
| 裁切（`pipeline.rs:602`） | `boxes.par_iter()`，仅当 `boxes.len() >= 4` | rayon 全局池 |
| 识别批（`pipeline.rs:811`） | `rayon::current_num_threads().min(total_batches).min(2).max(1)` | **硬编码 2** |
| LineNet 批（`pipeline.rs:711`） | 同上 `.min(2)` | **硬编码 2** |
| 识别 worker 会话 | `self.rec.try_clone()` 逐个克隆，克隆失败即停止（`:819-824`） | 受 `.min(2)` 约束 |

**服务化含义**：单张图内 rec 最多 2 路并行，`rec_batch_num=6`（PP）或 `=1`（Indra）决定了 batch 粒度。**吞吐只能靠"多引擎实例 + 进程/线程池"从外部堆**，而这正是上游文档给出的方案（`docs/content/en/02.how-to/00.use-rust.md:267-269`："Because `RustO` instances hold mutable session state, create one instance per worker thread"）。

### 5.3 内存与并发安全性

- `rusto_nn::Session` 内含 `RefCell<GraphContext>`（`session/mod.rs:36`）→ **`!Sync`**；`InferenceSession: Send` 而非 `Sync`（`engine.rs:79`）→ 一个引擎实例**只能被一个线程可变借用**。
- `Model` 用 `memmap2` 映射权重（`model.rs:26,38`），默认**加载后丢弃 mmap**（`from_storage(..., false)`），注释写 "drops mmap after graph load to release memory"。→ **多实例不共享权重页**，N 个 worker = N 份模型内存。
- 有 `clear_tensor_buffer_pool()`（`rusto-nn/src/lib.rs:14`）与 thread-local `TENSOR_BUFFER_POOL`（`PURE_RUST_MNN_PERFORMANCE.md:180`）。

### 5.4 实测数字

见 §8 性能基线表。一句话概括：**精度有优势（45.70% vs PaddleOCR 45.33%）、内存有优势（190 MB vs 695 MB）、延迟是劣势（1 827 ms vs 731 ms）**。

---

## 6. GPU 推理现状

### 6.1 抽象层只有 GEMM

```rust
// crates/rusto-nn/src/backend.rs:7
pub trait DeviceBackend: Send + Sync {
    fn name(&self) -> &'static str;
    fn is_available(&self) -> bool { true }
    fn gemm(&self, m, n, k, a: &[f32], b: &[f32], c: &mut [f32],
            alpha: f32, beta: f32, bias: Option<&[f32]>, act: FusedActivation)
        -> Result<(), MnnError>;
}
static ACTIVE_BACKEND: RwLock<Option<Arc<dyn DeviceBackend>>>;   // :33 进程级单例
```

**只有一个方法**。没有 conv / depthwise / pool / norm / elementwise / attention 的接口。同时 `rusto_nn::register_backend` 是**全局单槽**（`：36`），后注册覆盖先注册 → 一个进程只能挂一种加速器。

分派门槛（`kernels/gemm/mod.rs:38-44`）：

```rust
if let Some(backend) = crate::backend::active_backend() {
    if backend.is_available() && m * n * k >= 65536 {
        if backend.gemm(...).is_ok() { return; }        // 失败静默退回 CPU
    }
}
```

- 阈值 **65 536 FLOPs**（约 64³）才走 GPU，比 `PURE_RUST_MNN_PERFORMANCE.md:93` 声称的 262 144 低，文档与代码不一致。
- 失败**静默回退** CPU：`is_ok()` 判空，无日志、无指标、无熔断。服务端会"以为在用 GPU，其实一直在 CPU"，**这是必须自己补可观测性的地方**。

### 6.2 CUDA（`crates/rusto-nn-cuda`，160 行 lib.rs）

| 项 | 事实 |
|---|---|
| 依赖 | `cudarc 0.19.10`（`driver`+`cublas`+`fallback-dynamic-loading`+`cuda-12000`）、`libloading 0.9`（`Cargo.toml:13-14`） |
| 驱动探测 | `is_cuda_driver_available()` 试 `libcuda.so.1`/`libcuda.so`/`nvcuda.dll`/`libcuda.dylib`（`:16-29`） |
| 初始化 | `CudaBackend::init()` → `with_device(0)`；`catch_unwind` 包裹；失败返回 `BackendError`（`:33-65`） |
| 注册 | `backend.register()` → `rusto_nn::register_backend(Arc::new(self))`（`:68`） |
| gemm 实现 | `clone_htod(a)` → `clone_htod(b)` → （`beta≠0` 时）`clone_htod(c)` → cuBLAS（列主序转置技巧，`:110-126`）→ `memcpy_dtoh(c)` → **host 端标量循环做 bias+activation**（`:132-144`） |
| 显存管理 | **无**。每个 GEMM 现分配、现释放，无 buffer pool、无张量驻留 |
| 测试 | `tests/cuda_test.rs` 只测 16×16×16，无 GPU 时断言 `active_backend().is_none()`——**即 CI 里从不真跑 GPU** |

**判定：壳代码偏多，功能可用但性能未证实。** 每次 GEMM 两次 H2D + 一次 D2H，往返开销 15–40 μs 量级（`PURE_RUST_MNN_PERFORMANCE.md:79`），在 65 536 FLOPs 门槛下必然倒挂。**全仓库没有任何 CUDA 的端到端实测数字**——`benchmarks/` 里所有 GPU 列都是 WebGPU（Intel Iris Xe）。

### 6.3 WebGPU（`crates/rusto-nn-webgpu`，496 行 lib.rs）

| 项 | 事实 |
|---|---|
| 依赖 | `wgpu 24`（`wgsl`,`webgpu`,`fragile-send-sync-non-atomic-wasm`）、`pollster 0.4` |
| shader | 仅 `shaders/gemm.wgsl`：16×16 tile 共享内存 GEMM，`@workgroup_size(16,16)`，uniform 参数块 + 4 storage buffer |
| 分派 | `dispatch_workgroups((n+15)/16, (m+15)/16, 1)`（`:453-455`） |
| 显存管理 | **有** buffer pool：`a_buf/b_buf/bias_buf/c_buf/staging_buf` 按需几何扩容 + `cached_bind_group` 复用（`:337-392`） |
| 回读 | `copy_buffer_to_buffer` → `map_async` → `device.poll(wait_indefinitely)`（`:458-479`） |
| **WASM 硬编码禁用** | `is_available()` 在 `cfg(target_arch="wasm32")` 下 **直接 `return false`**（`:304-310`），注释理由是单线程浏览器里 `rx.recv()` 会死锁。→ **浏览器 WebGPU 实际不可用**，尽管 `rusto-wasm` 提供了 `wasm_init_webgpu()` 并会 `register()`（`crates/rusto-wasm/src/lib.rs:52-70`），注册后 `is_available()=false`，`gemm` 门槛判断直接跳过 |
| 测试 | `tests/backend_test.rs` 只测 16×16×16 单次 GEMM，`if let Ok(backend)` 包着——**拿不到 device 就跳过** |

**判定：桌面端可用（能跑），但只有 GEMM 一层，且算子覆盖决定了收益上限。**

### 6.4 CPU vs GPU：上游**有**实测数据（重要更正）

`benchmarks/PURE_RUST_MNN_PERFORMANCE.md:46-52` 有 WebGPU 列（硬件：Intel Core AVX-512 + AVX2 + FMA，8 线程，**Intel Iris Xe 核显 Vulkan**）：

| 图片 | 模型 | 行数 | RTen | 纯 Rust CPU(AVX-512) | **纯 Rust WebGPU** | MNN C++ |
|---|---|---|---|---|---|---|
| `example1.png` | PP-OCRv6 Tiny | 47 | 320.56 ms | 316.06 ms | **319.09 ms** | 311.98 ms |
| `ktp.jpeg` | PP-OCRv6 Tiny | 28 | 483.11 ms | 476.45 ms | **474.63 ms** | 431.06 ms |
| `chronam-ad-page.png` | PP-OCRv6 Tiny | 162 | 1.51 s | 1.44 s | **1.75 s** | 1.38 s |
| `example1.png` | Indra (LineNet) | 45 | 267.37 ms | 217.10 ms | **216.80 ms** | 172.03 ms |
| `idcard.jpg` | Indra (LineNet) | 24 | 207.06 ms | 205.62 ms | **199.18 ms** | 169.28 ms |

**读法**：GPU 最好情况快 3%（199.18 vs 205.62），最坏情况慢 21%（1.75 s vs 1.44 s），密度越高越亏。上游自己的解释（`：63-93`）：微张量下驱动往返 25 μs vs 计算 0.2 μs，开销 125×；核显 FP32 只有 CPU 的 1.5–2.0×，抵不掉往返。**这组数据是在核显上测的，独显（如 4090，83 TFLOPS）结论未测**——这是需求文档里唯一可以合理主张"GPU 可能有用"的空白区，但必须自己测。

### 6.5 根包里根本没接线（必须知道）

全量 grep `src/lib.rs`、`src/cli.rs`：`rusto_nn_webgpu` / `rusto_nn_cuda` / `cuda` / `webgpu` 命中 **0 次**。`src/lib.rs:278` 的 `init()` 只注册 rten 与 mnn-cpp。

→ **打开 `--features cuda` 只是把 crate 编进来，没有任何代码去 `register()`。** CLI 也没有任何 `--device` / `--gpu` 参数（`src/cli.rs` 全文 grep 为空）。要用 GPU，必须由调用方手写：

```rust
let b = rusto_nn_cuda::CudaBackend::init()?; b.register();   // 或 rusto_nn_webgpu::init_and_register()
```

### 6.6 结论

| 维度 | 判定 |
|---|---|
| GPU 是否"支持" | 接口层支持，GPU 只加速 **1×1 pointwise 卷积背后的 GEMM** |
| GPU 是否"更快" | **取决于硬件档次**：核显（Iris Xe，1.5–2 TFLOPs）基本持平、密集页更慢 21%；**独显（83 TFLOPs）从未测过** |
| 是否有批处理 | 有 `rec_batch_num`（单图内识别批），**无跨请求 batch** |
| 是否有显存管理 | WebGPU 有 buffer pool；CUDA **无** |
| 是否有回退 | 有，但**静默**（`gemm().is_ok()`），不可观测 |
| 是否可多设备共存 | **不可**（`ACTIVE_BACKEND` 全局单槽） |

**对需求文档的要求**：GPU 应写成"**必须达标的加速路径**"（业务目标：N 卡独显 ≥2×，CPU 5 s → GPU 2–3 s），并把"CPU vs GPU 实测对照"列为核心验收项。

> ⚠️ **不要把核显结论外推到独显**：上游唯一 GPU 实测是 Intel Iris Xe 核显，其 FP32 仅 1.5–2.0 TFLOPs，而 8 线程 AVX-512 CPU 有 0.8–1.0 TFLOPs——**算力差只有 1.5–2×**，抵不掉驱动往返（`PURE_RUST_MNN_PERFORMANCE.md:88-93` 原文）。而 RTX 4090 是 83 TFLOPs（约 CPU 的 80–100×），上游自己也承认 "discrete server GPU 40x faster than CPU"。**"GPU 更快"在独显上完全可能成立，前提是完成 conv/depthwise 分派与张量驻留改造**（见 §12 缺口清单 B 组）。

---

## 7. 服务化判定（关键结论）

> **本项目只提供库 / CLI / 12 种语言绑定，不提供服务端。**

证据：
1. **无服务端代码路径**：826 个 blob 中不存在 `server/`、`http/`、`api/`、`grpc/`、`daemon/` 目录或文件；`grep -Ei 'serve|listen'` 命中 0。
2. **无 HTTP 依赖**：11 个 `Cargo.toml` 全量 grep `axum|actix|tokio|hyper|warp|tonic|rocket|poem|salvo|tide|tiny_http|tower` → 0。连 `serde_json` 都没有（用 `nanoserde`）。
3. **唯一的"批量"是 shell 管道**：`docs/content/en/02.how-to/06.use-cli.md:251-258` 官方示范 `find ... | xargs -I {} -P 4 rusto {} --format spatial`。CLI 本身 `OcrArgs.image: Option<PathBuf>`（`src/cli.rs:38`）——**一次一张图**，无 `--dir`、无 `--glob`、无并发控制。
4. **`.github/workflows/`** 只有 `build/ci/benchmark/docs-ci/docs/deploy-docs/publish/publish-models/publish-models/snyk`，无任何服务部署产物。

服务化必须自建的部分：HTTP 层、引擎池、有界队列、背压、任务持久化、设备管理、指标、鉴权、限流。

---

## 8. 性能基线表

> ⚠️ 三个前置警告：(1) 上游 benchmark 用 `opt-level=3`（`BASELINE_LATENCY.md:6`），发布 profile 是 `opt-level="z"`；(2) 各文件之间对同一配置给出的数字互相矛盾（见 8.4）；(3) 除 8.2 外全部无 GPU 数据。

### 8.1 OmniDocBench（30 篇多栏文档；i7-1185G7 / 4C8T / 32 GB / Linux）
来源：`benchmarks/README.md:23-34`、`docs/content/en/03.advance-guide/07.performance-and-benchmarks.md:31-42`

| 引擎 / 运行时 | 二进制 | 权重 | 均值延迟 | P95 | 峰值 RSS | 1-NED |
|---|---|---|---|---|---|---|
| PaddleOCR + PP-OCRv6 Tiny | 120.1 MB | 6.1 MB | **730.7 ms** | 1 697.6 ms | 694.8 MB | 45.33% |
| RapidOCR（C++ ORT） | 22.7 MB | 6.1 MB | 1 170.7 ms | 2 306.2 ms | 397.1 MB | 44.84% |
| **RustO NN + Indra** | **2.1 MB** | 5.4 MB | 1 827.2 ms | 5 084.8 ms | **190.5 MB** | **45.70%** |
| RustO NN + PP-OCRv6 Tiny | 2.1 MB | 5.9 MB | 2 522.0 ms | 5 096.6 ms | 247.8 MB | 45.44% |
| RustO + RTen | 4.3 MB | 6.1 MB | 12 020.1 ms | 28 214.7 ms | 169.0 MB | 45.40% |
| RustO + MNN C++ | 4.7 MB | 5.9 MB | 2 032.2 ms | 5 146.0 ms | 168.1 MB | **39.57%** |
| Tesseract v5 (PSM 6) | 4.2 MB | 3.9 MB | 1 959.2 ms | 7 053.8 ms | 174.4 MB | 41.14% |
| ocrs (原版 RTen) | 12.2 MB | 11.6 MB | 2 372.2 ms | 5 573.1 ms | 1 271.7 MB | 44.33% |

全管线（含版面/方向/阅读顺序）：RustO Indra Full **1 780.2 ms / 193.9 MB / 63.90%**；Indra 官方 Python 3 111.6 ms / 63.76%；Tesseract PSM3 1 985.8 ms / 60.29%；PaddleOCR+PP-Structure 20 676.5 ms / 57.18%；RapidOCR+RapidLayout 1 561.7 ms / 53.85%；RustO CDLA 4 837.3 ms / 55.03%。

### 8.2 单图端到端（`benchmarks/PURE_RUST_MNN_PERFORMANCE.md:46-52`）
硬件：AVX-512 + AVX2 + FMA，8 线程，**Intel Iris Xe（Vulkan）**；测试：`cargo test --features "mnn-cpp webgpu" -- --test-threads=1`

| 图片 | 管线 | 行数 | RTen | CPU AVX-512 | WebGPU | MNN C++ | 转写一致 |
|---|---|---|---|---|---|---|---|
| `example1.png` | PP-OCRv6 Tiny | 47 | 320.56 ms | 316.06 ms | 319.09 ms | 311.98 ms | 100% |
| `ktp.jpeg` | PP-OCRv6 Tiny | 28 | 483.11 ms | 476.45 ms | 474.63 ms | 431.06 ms | 100% |
| `chronam-ad-page.png` | PP-OCRv6 Tiny | 162 | 1.51 s | **1.44 s** | 1.75 s | 1.38 s | 100% |
| `example1.png` | Indra (LineNet) | 45 | 267.37 ms | 217.10 ms | 216.80 ms | 172.03 ms | 100% |
| `idcard.jpg` | Indra (LineNet) | 24 | 207.06 ms | 205.62 ms | 199.18 ms | 169.28 ms | 100% |

单算子延迟（同文件 `:17-22`，20 次迭代 / 5 次预热）：

| 模型 | 输入 | 纯 Rust 最小/中位 | RTen(ONNX) | MNN C++ 最小/中位 |
|---|---|---|---|---|
| PP-OCRv6 det | `[1,3,640,640]` | 104.0 / 128.8 ms | 124.0 ms | 96.9 / 172.8 ms |
| PP-OCRv6 rec | `[1,3,48,320]` | 15.5 / 23.0 ms | 22.0 ms | 6.4 / 31.1 ms |
| Indra det | `[1,1,640,640]` | 57.6 / 72.3 ms | 64.6 ms | 30.4 / 96.4 ms |
| Indra rec | `[1,1,96,160]` | 11.6 / 15.3 ms | 18.2 ms | 2.1 / 5.3 ms |

冷加载：det **0.45–1.01 ms**、rec **2.39–3.68 ms**（RTen 需 9.2–17.5 ms，ORT 需 6.5–38.8 ms）。→ **模型加载极快，池化预热成本低，可以"按需建实例"。**

### 8.3 分阶段拆解

`benchmarks/BASELINE_LATENCY.md`（`chronam-ad-page.png`，282/179 box，RTen 0.27.0，8 核 Linux）：

| 阶段 | PP-OCRv6 Tiny | Indra |
|---|---|---|
| PNG 解码 | 3.59 ms (0.2%) | 3.62 ms (0.3%) |
| 检测（DBNet/LineNet） | 184.93 ms (9.5%) | 64.58 ms (5.5%) |
| 裁切（282/179 次仿射） | 22.21 ms | 36.99 ms |
| **识别（47/30 批 N=6）** | **1 729.60 ms (89.1%)** | **1 071.06 ms (91.0%)** |
| **合计** | **1 940.77 ms** | **1 176.68 ms** |
| 单 box 识别 | 6.133 ms | 5.984 ms |

`benchmarks/BASE_PIPELINE_COMPARISON.md`（`ktp.jpeg` 1600×993，27 行，2 预热 5 次）：

| 引擎 | 总计 | 最小 | 最大 | 其中神经推理 | 图像+后处理 |
|---|---|---|---|---|---|
| EngineSession（原生） | 705.13 ms | 668.13 | 752.47 | 520.98 | 184.16 |
| Rust `ort` | **447.83 ms** | 444.41 | 450.71 | 282.79 | 165.04 |
| RTen (.onnx) | 689.94 ms | 654.44 | 745.16 | 496.37 | 193.57 |
| RTen (.rten) | 678.08 ms | 592.31 | 777.92 | 481.39 | 196.68 |

→ **recognition 是绝对瓶颈（89–91%）**，detection 只占 5–10%。任何服务端优化优先级：rec batch/并发 > det。

### 8.4 上游数据的三处内部矛盾（写需求时必须知道）

1. **MNN C++ 端到端**：`COMPARISON_RESULT.md:9` 说 478.99 ms，`ORT_RTEN_MNN_COMPARISON.md:16` 说 479.07 ms，但同表 `:15` 的"总推理"是 39.7 ms——**"pipeline" 与 "inference" 口径混用**。
2. **RustO 自身**：`BASE_PIPELINE_COMPARISON.md:14` 原生 705.13 ms，`PURE_RUST_MNN_PERFORMANCE.md:49` 同一张 `ktp.jpeg` 是 476.45 ms，`ORT_RTEN_MNN_COMPARISON.md` 是 479.07 ms。差异来自 `rec_batch_num` 与是否单次 pass（`PURE_RUST_MNN_PERFORMANCE.md:144-149` 自述早期"跑了两遍"导致虚高）。
3. **"优化"反而变慢**：`OPTIMIZATION_RESULTS.md:37` 明写优化后 2 149.99 ms vs 基线 1 940.77 ms（BufferPool 命中率 >94%、depthwise 单算子 6.098→0.089 ms 都没能翻盘）。

**结论**：**不要直接引用上游数字做 SLA**，必须自己复测一套（`ferroocr-bench`），并把复测口径（batch、迭代数、profile、图片集）写进验收标准。

---

## 9. 公开 API / CLI / 输出形态

### 9.1 对外使用形态

```rust
// 初始化（一次）
let mut ocr = RustO::initialize(InitializeConfig::default())?;          // 自动下载 Indra
// 或 RustO::initialize(InitializeConfig::indra(det, rec, dict))?       // 指定路径

// 每请求（Builder，6 阶段 + 4 开关 + 输出粒度）
let result = ocr
    .detect_text_builder(ImageSource::Path("doc.jpg".into()))
    .with_auto_unwarp(true)              // UVDoc 去畸变
    .with_layout_reading(true)           // PicoDet 版面 + 神经阅读顺序
    .with_orientation_detection(true)    // 0/90/180/270
    .with_line_classification(true)      // 180° 行翻转
    .with_pre_detection(PreDetectionOptions { descreen_strength, target_max_side, crop_padding_x, crop_padding_y })
    .with_detection(DetectionRunOptions { limit_side_len, limit_type, mean, std })
    .with_post_detection(PostDetectionOptions { threshold, box_threshold, unclip_ratio, use_dilation, score_mode, max_candidates, min_height, min_side_len, max_side_len, width_height_ratio })
    .with_pre_recognition(PreRecognitionOptions { line_y_threshold, word_x_threshold })
    .with_recognition(RecognitionRunOptions { batch_size })
    .with_post_recognition(PostRecognitionOptions { score_threshold })
    .as_lines()                          // or as_words()/as_spatial()/as_tsv()
    .run()?;
```

出处：`src/lib.rs:97-244`（`DetectTextBuilder`）、`docs/content/en/04.api-reference/02.ocr-run-options.md:42-83`。

**输出数据类型**（`types.rs:510`）：
```rust
pub struct TextResult {
    pub text: String, pub score: f32,
    pub box_points: [(f32,f32); 4],   // TL,TR,BR,BL
    pub frame: Frame,                  // left/top/width/height
    pub region_id: Option<i32>,        // 仅 layout 开启
    pub layout_type: Option<LayoutType>, // 10 类：text/title/figure/figureCaption/table/tableCaption/header/footer/reference/equation
}
pub enum DetectTextResult { Structured(Vec<TextResult>), Spatial(String) }
```
`PipelineOutput`（`pipeline.rs:48`）额外带 `elapse_det/elapse_rec/elapse_orient/elapse_unwarp`（`f64` 秒）——**上游已有分阶段计时，服务端指标可以直接接这个，不用自己打点**。

### 9.2 CLI（`src/cli.rs`，`rusto` 2.1 MB）

子命令：`rusto <IMAGE>` / `rusto ocr <IMAGE>` / `rusto download`。
参数（`:38-131`）：`-m/--model`（默认 `indraocr`）、`-l/--lang`、`--model-path`、`--det-model`、`--rec-model`、`--dict`、`--cache-dir`、`--download-only`、`-f/--format`、`--enable-cls/--cls-model`、`--enable-orient/--orient-model`、`--enable-unwarp/--unwarp-model`、`--enable-layout/--layout-variant/--layout-model`、`--reading-order`、`--heuristic-reading-order`、`--order-model`、`--det-limit`、`--rec-batch-num`（alias `--batch-num`）、`--score-threshold`、`--max-side-len`。

格式枚举（`:196-209`）：`json`（默认）、`text`、`tsv`、`markdown`、`text-ordered`、`spatial`。

**没有的**：`--device` / `--gpu` / `--batch` / `--dir` / `--watch` / `--output-dir` / `--workers` / `--timeout`。

### 9.3 多语言与档位

`models.rs:44-96` 的 `resolve_language_assets()` 硬编码 15 个语言 key：`latin/japan/korean/arabic/cyrillic/devanagari/chinese_cht/el/eslav/ta/te/th/kannada` + Indra 的 `cjk/rtl/world`。档位见 §10。

---

## 10. 模型与资产管理

### 10.1 来源与路径

| 项 | 事实 | 来源 |
|---|---|---|
| Host | `https://huggingface.co/byrizki/rusto-models/resolve/main` | `models.rs:6`（`DEFAULT_HF_BASE_URL`） |
| 缓存目录 | `~/.cache/rusto/models`（Linux）；`dirs::cache_dir()` 派生；wasm/无 cache 时 `.cache/rusto/models` | `models.rs:179-198` |
| 解析顺序 | ① 显式路径 → ② 自定义 model dir → ③ 缓存（含 `.mnn/.rten/.onnx` 同 stem 扩展名回退，顺序随 `active_backend()` 变）→ ④ 本地仓库 `models/*` 候选 | `models.rs:395-464` |
| 下载实现 | **`std::process::Command` 调外部 `curl`**（`-fsSL`），失败退 `wget`（unix）/`powershell WebClient`（windows） | `src/download.rs:8-63` |
| 原子性 | `.part` 临时文件 → 长度校验 → `fs::rename` | `src/download.rs:87-124` |
| **完整性校验** | **无 SHA-256、无签名**。`docs/.../01.models-and-tiers.md:210-216` 提到 release 带 `SHA256SUMS`，但代码里**没有验证逻辑** | 自建 grep `sha256\|checksum\|digest` → 0 |
| 断点续传 | 无 | `src/download.rs` 全文 |
| 离线模式 | 只有 `--download-only` 预取；**无强制 `--offline`** | `src/cli.rs:47` |

### 10.2 格式

`.mnn`（默认，FlatBuffers）、`.rten`（FlatBuffers）、`.onnx`（经 `rten-onnx` feature）。格式由文件头嗅探（`model.rs:69-95`），**与扩展名无关**。`resolve_model_path()`（`engine.rs:211`）会尝试 `path.with_extension("mnn"/"rten"/"onnx")`。

### 10.3 档位与体积（`docs/content/en/03.advance-guide/01.models-and-tiers.md:21-32,157-206`）

| 档位 | 体积 | 用途 |
|---|---|---|
| **`indraocr`（默认）** | ~5.5 MB（det 396 KB + rec 4.9 MB + meta 207 KB） | 卡片/票据/拉丁，延迟最优 |
| `ppocrv6-tiny` | ~6.1 MB | 移动端、WASM |
| `ppocrv6-tiny-int8` | ~3.4 MB | IoT/嵌入式 |
| `ppocrv6-small` | ~30.5 MB | 桌面、密集扫描 |
| `ppocrv6-small-int8` | ~16.7 MB | 服务端吞吐 |
| `ppocrv6-medium-int8` | ~77 MB | 高精度批处理 |
| `ppocrv5-mobile` / `v5-server` | ~21 MB / ~54 MB | 多语言基线 |
| `ppocrv4-mobile` / `v4-server` | ~21 MB / ~65 MB | CJK |

辅助资产：`uvdoc.rten` **30 MB**、`layout_cdla.rten` 5.8 MB、`layout_publaynet.rten` 5.2 MB、`indra_order.rten` 2.9 MB、`indra_orient.rten` 370 KB（`ppocr_orient` 6.4 MB）、`ppocr_cls.rten` 585 KB。**全开 = 约 50 MB 权重。**（`07.performance-and-benchmarks.md:60` 的 "Model Weights 49.8 MB" 可交叉验证。）

### 10.4 自定义 / 微调模型

- **支持**：`InitializeConfig::indra(det, rec, dict)` 可传任意路径；`with_custom_engines()` / `with_engine_sessions()` / `register_session_loader()` 可注入自定义 `InferenceSession`（`engine.rs:772,827,334`）；`tests/custom_engine_test.rs` 有现成用例。
- **不支持**：没有任何训练/微调/量化工具。INT8 量化是离线脚本 `scripts/quantize_ppocrv6_int8.py`（Python），不属运行时能力。
- 字典：`dict.txt` 或 `meta.json`（Indra 用后者，`get_character_list("character")`，`engine.rs:782`）。

---

## 11. 跨平台与绑定

| 目标 | 产物 | 来源 |
|---|---|---|
| Rust | `rusto-rs` crate（lib + 7 bin） | `Cargo.toml` |
| Node | `@rustors/node`（napi-rs 2.16，`crate-type=["cdylib"]`） | `crates/rusto-node/Cargo.toml` |
| Python | `rusto`（UniFFI wheel） | `packages/python/` |
| Go / Java / .NET / Dart / Flutter / Swift / Kotlin | `packages/*` 各自 FFI 包装 | `packages/` |
| C | `librusto.{so,dylib,dll}` + `include/rusto.h` | 根包 `crate-type` + `cbindgen` |
| Web | `@rustors/web`（wasm-bindgen，`rusto-wasm`） | `crates/rusto-wasm/` |

**CI 构建矩阵**（`.github/workflows/build.yml`）：

| Job | target | runner |
|---|---|---|
| `build-ios-libs` | `aarch64-apple-ios`、`aarch64-apple-ios-sim`、`x86_64-apple-ios` | macos-15 |
| `build-android-libs` | `aarch64-linux-android`、`armv7-linux-androideabi`、`i686-linux-android`、`x86_64-linux-android` | ubuntu-22.04 |
| `build-dotnet` | `x86_64-unknown-linux-gnu`、`x86_64-apple-darwin`、`aarch64-apple-darwin`、`x86_64-pc-windows-msvc`（均 `--features ffi`） | ubuntu/macos/windows-2022 |

**CI 中无 CUDA、无 WebGPU、无 benchmark 门禁**：全文件 grep `cuda|webgpu` → 0 命中。GPU 代码路径**从未在 CI 里被编译验证过**（`cuda_test.rs` 只在开发机手动跑，且拿不到卡就跳过）。

注意：`macos-15` 与 `windows-2022` 说明服务端要支持的 Linux x86_64 gnu 属于一等公民，但 **Windows MSVC 的 CUDA 路径完全未验证**。

---

## 12. 缺口清单（→ 需求文档功能需求输入）

按"做 CPU + GPU 本地 OCR 服务"的目标逐条列。**P0 = 不上线就不能用；P1 = 上线后第一批补；P2 = 有余力**。

### A. 服务面（全部新建，上游 0 行）

| # | 缺口 | 证据 / 为什么 | 优先级 |
|---|---|---|---|
| A1 | HTTP/REST API（同步 + 异步任务 + 取消 + 结果查询） | 无任何 HTTP 依赖 | P0 |
| A2 | 请求级超时与隔离 | `panic="abort"`（`Cargo.toml:121`）会打死进程 | P0 |
| A3 | 引擎池（`RustO` 是 `&mut self`，`InferenceSession: Send` 非 `Sync`） | `engine.rs:79,943` | P0 |
| A4 | 有界队列 + 背压 + 公平性 | 全无 | P0 |
| A5 | 批处理调度（**跨请求** batch，非单图 `rec_batch_num`） | 只有单图内批（`pipeline.rs:805`） | P1 |
| A6 | 任务持久化 / 断点续跑 / 重试 | 全无 | P1 |
| A7 | 鉴权 / 限流 / 配额 | 全无 | P1 |
| A8 | 结构化日志 + Prometheus 指标 + trace | 全无（但 `PipelineOutput.elapse_*` 可直接转指标） | P0 |
| A9 | 错误模型（错误码枚举 + RFC 9457） | `EngineError` 是内部枚举（`engine.rs:30`），不适合作 wire format | P0 |
| A10 | 输入安全（像素上限 / EXIF 剥离 / 解压炸弹 / 路径白名单） | 无 | P0 |
| A11 | 优雅停机 / 健康检查 / 就绪探针 | 无 | P0 |
| A12 | 服务端构建 profile（`opt-level=3`、`panic=unwind`） | `Cargo.toml:116-121` | P0 |

### B. GPU 面

| # | 缺口 | 证据 | 优先级 |
|---|---|---|---|
| B1 | **GPU 收益不确定，必须先建"实测门禁"** | `PURE_RUST_MNN_PERFORMANCE.md:46-52`：核显上持平甚至更慢 | P0 |
| B2 | 设备探测 + 后端选择（CLI/配置无入口） | `src/cli.rs` grep `gpu/device` → 0 | P0 |
| B3 | 根包接线（`cuda`/`webgpu` feature 打开后无人 `register()`） | `src/lib.rs:278` 只注册 rten/mnn-cpp | P0 |
| B4 | GPU 失败回退的**可观测性** | `gemm().is_ok()` 静默吞错（`gemm/mod.rs:40`） | P0 |
| B5 | 后端熔断（GPU 连续失败转 CPU 且告警） | 无 | P0 |
| B6 | 单进程多设备（CPU+GPU 共存） | `ACTIVE_BACKEND` 全局单槽（`backend.rs:33`）→ 只能多进程 | P1 |
| B7 | 显存管理（CUDA 每次 GEMM 全量 H2D/D2H） | `rusto-nn-cuda/src/lib.rs:95-129` | P1 |
| B8 | 算子覆盖扩展：conv/depthwise 上 GPU | `depthwise.rs` 中 `active_backend` 命中 0 | P1（需改上游或 fork） |
| B9 | 浏览器 WebGPU 可用（当前硬编码 false） | `rusto-nn-webgpu/src/lib.rs:304-310` | P2 |
| B10 | CUDA 后端发布化（未上 crates.io，只能 git/path 依赖） | `crates.io` 无 `rusto-nn-cuda` | P1 |

### C. 模型资产面

| # | 缺口 | 证据 | 优先级 |
|---|---|---|---|
| C1 | **SHA-256 校验 + 模型 lockfile** | `src/download.rs` 无校验；`models.rs` 无 manifest 校验 | P0 |
| C2 | 强制离线模式（`--offline`，绝不联网） | 只有 `--download-only` | P0 |
| C3 | 模型预热 / 常驻（避免首请求冷启） | 冷加载虽快（0.45–1.01 ms），但 30 MB uvdoc 例外 | P1 |
| C4 | 模型热切换（蓝绿池） | 无 | P1 |
| C5 | 多档位共存与按请求选档（只能按进程选） | `InitializeConfig` 在 initialize 时固定 | P1 |
| C6 | 模型许可矩阵 | 上游未给；Indra/PaddleOCR 均 Apache-2.0，但权重再分发条款需法务 | P0（非技术） |

### D. 功能面

| # | 缺口 | 证据 | 优先级 |
|---|---|---|---|
| D1 | PDF 输入 | 无 | P1 |
| D2 | 区域局部识别（bbox ROI） | 无 API（只有整图） | P1 |
| D3 | 表格结构对外暴露 | `table.rs` 有 `to_html/to_markdown`，但 `PipelineOutput` **不带**表格字段（`pipeline.rs:48-64`）→ 虽实现但不可达 | P1 |
| D4 | hOCR / ALTO 输出 | 只有 json/text/tsv/markdown/spatial | P1 |
| D5 | 流式/分块返回 | 无 | P2 |
| D6 | 多页文档（TIFF / 多帧） | 无 | P2 |

### E. 工程面

| # | 缺口 | 证据 | 优先级 |
|---|---|---|---|
| E1 | 基准复测套件（上游数字口径混乱，见 §8.4） | — | P0 |
| E2 | 精度回归门禁（防换模型掉点） | 无 CI | P0 |
| E3 | 权重共享降低多实例内存（`memmap` 被主动丢弃） | `model.rs:38` `from_storage(..., false)` | P1 |
| E4 | 线程治理（全局 rayon 池 vs 多实例的相互干扰） | `EngineConfig.intra_op_num_threads` 对纯 Rust 后端无效 | P0 |
| E5 | 上游版本策略（crates.io 落后于仓库） | 需 git tag 或 vendor | P0 |

---

## 13. 对需求文档的 6 条硬约束

1. **定位**：卖点写「纯 Rust / 单二进制 2.1 MB / 零 C++ 依赖 / 峰值内存 190 MB / 内存安全 / 可完全离线」，**不写"比 PaddleOCR 快"**——1 827 ms vs 731 ms，慢 2.4×。
2. **GPU**：业务目标是 **N 卡独显 ≥2×**（CPU 5 s → GPU 2–3 s），且客户免装 CUDA。当前上限是"只加速 GEMM"，**核显实测无收益（-21%）、独显从未测过**。达成目标必须做 conv/depthwise 分派 + 张量驻留 + 批处理 + 自适应阈值四项改造。
3. **架构**：v1 只能「单进程单加速器 + 多引擎实例池」；CPU 与 GPU 同进程共存需多进程，放 v1.1。
4. **并发**：单图内并行被硬编码 `.min(2)`（`pipeline.rs:811`），吞吐必须靠实例池，池大小需实测标定（内存 190 MB/实例）。
5. **构建**：必须 fork 或 patch 覆盖 `panic="abort"` 与 `opt-level="z"`，否则服务端 panic 即全挂、性能也对不上 benchmark。
6. **数字**：不引用上游任何延迟数字做 SLA，自建 `ferroocr-bench` 复测（口径：batch、迭代、profile、数据集全部固定）。

---

## 附录 A：本次未读到 / 无法确认的项

| 项 | 状态 |
|---|---|
| `benchmarks/omnidocbench/*.md`（含 `HYBRID_OCR_RESEARCH_REPORT.md`） | 未逐文件读，仅通过 `benchmarks/README.md` 摘要引用 |
| `benchmarks/engine_comparison/src/main.rs` | 未读（结论通过其产出的 3 份 md 交叉验证） |
| `crates/rusto-node/src/lib.rs` 全部 API | 仅读 README + Cargo.toml |
| `packages/*` 12 套绑定的实现细节 | 仅确认存在与形态 |
| `models/indra/meta.json` 等真实权重文件 | 未下载（仓库内 `models/` 只有 `dict.txt` 与 `meta.json` 等文本，权重托管在 HF） |
| HuggingFace 可达性与实际文件大小 | 未直连验证，体积数字来自 `docs/.../01.models-and-tiers.md` 表格 |
| 上游 issue / PR 中关于 CUDA 的进展 | 未查（本次范围仅代码与文档） |

## 附录 B：关键文件索引

```
Cargo.toml                                     workspace / features / profile
crates/rusto-core/src/engine.rs                 InferenceSession / EngineSession / RustO
crates/rusto-core/src/pipeline.rs               OcrPipeline / 6 阶段 / rayon
crates/rusto-core/src/config.rs                 ModelPreset / InitializeConfig
crates/rusto-core/src/types.rs                  EngineConfig / GlobalConfig / TextResult
crates/rusto-core/src/models.rs                 HF URL / 缓存 / 语言包映射
crates/rusto-nn/src/backend.rs                  DeviceBackend（只有 gemm）
crates/rusto-nn/src/kernels/gemm/mod.rs         SIMD 分派 + GPU 门槛
crates/rusto-nn/src/model.rs                    格式嗅探 + memmap
crates/rusto-nn-cuda/src/lib.rs                 CUDA（全量拷贝）
crates/rusto-nn-webgpu/src/lib.rs               WebGPU（buffer pool + wasm 禁用）
crates/rusto-wasm/src/lib.rs                    wasm_init_webgpu
src/cli.rs                                      CLI 全部参数
src/download.rs                                 外部 curl/wget/powershell
src/lib.rs                                      init() 只注册 rten/mnn-cpp
```
