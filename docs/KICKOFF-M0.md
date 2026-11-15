# FerroOCR 开工提示词（M0 起步）

> 用法：新建对话后，把下面【短版】那段发出去即可——它会让新会话自己读本文件。
> 本文件（全文）自带全部前置事实，新会话不需要看历史记录。
> 最后更新：2026-11-15，对应 PRD v1.3、仓库根 `D:\Codes\FerroOCR`（已托管于 GitHub）。

---

## 【短版】直接复制粘贴这段

```
在仓库根目录 `D:\Codes\FerroOCR` 下
开始 FerroOCR 的正式开发，本轮只做 M0（一次性 CLI，L0）。

开工前先读这三个文件（按顺序），读完再动手：
1. docs/KICKOFF-M0.md        —— 本轮任务书，含全部前置事实与已知坑，优先按它执行
2. docs/PRD-FerroOCR.md      —— 需求权威，重点看 §5.2.1 启动参数、§5.3 协议与输出 schema
3. crates/ferroocr-cli/Cargo.toml —— 现有 manifest（src/ 还不存在，由 M0 补齐）

M0 目标：cargo build --release 产出 ferroocr.exe，能跑
  ferroocr.exe ocr --image spike\zh_test.png --out r.json
输出与我实测的上游 CLI 逐字一致（文本/顺序/score 完全相同），只去掉上游多出来的
frame 字段、补上 ok / elapsed_ms / image / text 外层；--help 列出全部启动参数。

硬约束（不要自己改）：
- 只改 crates/ferroocr-cli/，不要碰 spike/upstream/（上游源码，只读）
- 只出一个 exe；不引 tokio / tiny_http / interprocess / 任何 async runtime
- 本轮不做 HTTP、Named Pipe、常驻、模型内嵌、ORT/DirectML、任何 SDK
- ops_bash 是 GBK 控制台：写含中文的文件必须用 write 工具，别用 heredoc
- spike/upstream/target 已随仓库迁移丢弃，开工前先跑一次 cargo build 重建
  （约 1m50s；首次会联网补 getrandom 等缺失 crate）

完成后给我：--help 真实输出、一次真实识别的完整 JSON、exe 体积、clippy 结果。
```

> 若希望新会话**完全不读文件**，就把本文件全文粘过去（下面 §0 开始）。

---

## 0. 任务一句话

在仓库根目录下，**把 FerroOCR 的 M0 里程碑写出来**：让 `ferroocr.exe ocr --image x.png --out y.json` 能真正跑起来，输出与上游 CLI 逐字一致，且 `--help` 列出全部启动参数。**不要**在这一轮做 HTTP、管道、SDK、GPU。

---

## 1. 项目是什么

**FerroOCR**（中文名「铁识」）= 基于 [rusto-rs](https://github.com/byrizki/rusto-rs) 的**本地 OCR 附属工具**，一个 Windows 单 exe，让按键精灵、易语言、aardio、Python 这类语言/工具都能用上统一的 OCR。

它不是 OCR 算法项目，上游已经把识别做完了（纯 Rust 推理，OmniDocBench 精度 45.70%，优于 RapidOCR 44.84% 与 Tesseract 41.14%）。**我们做的是把上游包成一个「别的程序好调用」的东西。**

---

## 2. 四条定位（最高约束，冲突时按顺序满足）

| # | 定位 | 直接后果 |
|---|---|---|
| **P1** | **附属工具，不是豪华产品** | 接口极简、零配置、零依赖。宁可少 10 个功能，也不能让接入方多写 10 行代码 |
| **P2** | **只考虑 Windows** | 不用管 Linux/macOS；放心用 Named Pipe、互斥量、`CreateProcess` |
| **P3** | **产物尽可能少** | 目标是**一个 exe**（模型内置），不做安装包、不做 Docker、不做多文件运行时 |
| **P4** | **可手动启动，优先被其他程序静默拉起** | 核心是自动拉起 + 单实例 + 空闲自退；「双击运行」是次要路径 |

判断任何设计争议时，回到这四条。**最弱调用方（按键精灵）的能力决定下限。**

---

## 3. 已拍板的架构决策（不要重新讨论，除非发现硬伤）

### 3.1 四级接入阶梯 —— 同一个 exe 全支持

| 级别 | 方式 | 延迟 | 需常驻 | 给谁用 |
|---|---|---|---|---|
| **L0** | 一次性进程 `ferroocr.exe ocr --image a.png --out r.json` | ~1–2 s | ❌ | 任何语言，零依赖。**M0 就做这个** |
| **L1** | 同上，请求/结果走文件 `--in req.json --out res.json` | ~1–2 s | ❌ | 拿不到子进程 stdout 的老工具 |
| **L2** ⭐ | 本地 HTTP `127.0.0.1:1224`，**默认开启** | ~5–15 ms | ✅ | 按键精灵（`MSXML2.XMLHTTP`）、易语言（`WinHttp.WinHttpRequest.5.1`）、aardio（`inet.http`） |
| **L3** | Named Pipe `\\.\pipe\ferroocr-v1`，NDJSON | ~1–3 ms | ✅ | SDK 内部默认 |

**四级输出 JSON 结构完全一致**（同一 schema、同一错误码），换级不改业务代码。

### 3.2 单包 + 启动参数

**只出一个 `ferroocr.exe`**（含 CPU 引擎 + GPU 引擎 + 模型），功能差异靠启动参数：无参数双击 = 常驻 + L2 开 + L3 开。不做双包。

### 3.3 双引擎，同一 exe

- CPU：纯 Rust（`rusto-nn`），零外部 DLL
- GPU：`ort` + DirectML（`--device gpu`），**不需要装 CUDA**，Win 上所有 DX12 显卡通用，EP 自动回退 CPU

### 3.4 技术栈

- **不用 tokio**：OCR 是阻塞计算，async 无收益且体积大。用 `crossbeam-channel` + 原生线程
- **不用 tiny_http**：它最新版仍是 0.12.0（2022-10，4 年未更新）。L2 用 `std::net` 手写约 150 行
- **不引 `interprocess`**：L3 直接用 `windows-sys` 调 Named Pipe API（要 Win32 保真度）
- 序列化用 `serde_json`

### 3.5 输出 schema（唯一权威定义，四级共用）

```jsonc
{
  "id": "1",
  "ok": true,
  "elapsed_ms": 842,
  "image": { "width": 2480, "height": 3508 },
  "lines": [
    { "text": "中华人民共和国机动车驾驶证", "score": 0.9994985,
      "polygon": [[34.1, 40.4], [566.9, 40.4], [566.9, 96.6], [34.1, 96.6]] }
  ],
  "text": "中华人民共和国机动车驾驶证\n姓名：张三性\n…"
}
```

错误：`{"id":"1","ok":false,"code":"IMAGE_NOT_FOUND","message":"路径不存在: C:\\x.png"}`

**注意**：上游输出里每行还多一个 `frame: {width,height,top,left}` 字段，**我们的 schema 不要它**，只保留 `text` / `score` / `polygon`。

---

## 4. 仓库现状（2026-11-15 实测，请以此为准）

```
D:\Codes\FerroOCR\                # 仓库根（就是 ferroocr 本身，不是子目录）
├── .git/                          # 已在 GitHub：shanhaishi/FerroOCR，分支 main
├── .gitignore                     # 已忽略 target/、spike/upstream/、*.rten、*.exe
├── LICENSE                        # MIT
├── Cargo.toml                     # workspace，members=["crates/ferroocr-cli"]，exclude=["spike/upstream"]
├── README.md                      # 165 行
├── crates/ferroocr-cli/
│   └── Cargo.toml                 # 有 manifest，[[bin]] name="ferroocr" path="src/main.rs"
│                                  # ❌ 但 src/ 目录不存在 —— M0 只落了 manifest，由 M0 补齐
├── docs/PRD-FerroOCR.md           # 789 行，v1.3，需求唯一权威
├── docs/adr/                      # 五条架构决策记录（为什么这么定）
├── docs/archive/                  # 四份历史 PRD（v0.5/v1.0/v1.1/v1.2），只作考古
├── docs/KICKOFF-M0.md             # 本文件
├── research/01-rusto-rs-技术分析.md
├── research/02-竞品与生态调研.md
└── spike/                         # 前期验证，不是产品代码
    ├── models/ppocrv6-tiny_det.rten   1874240 B  sha256 前缀 ca1f0196d2a2da2b
    ├── models/ppocrv6-tiny_rec.rten   4485096 B  sha256 前缀 6c94c6b42c43a39d
    ├── models/ppocrv6_dict.txt          27156 B  sha256 前缀 c5cbe34ef40c29c4
    ├── zh_test.png                     测试图（6 行中文，机动车驾驶证）
    ├── bench.py / bench_mem.py / bench_result.json / bench_mem.json
    └── upstream/                       上游完整克隆（gitignored，245MB，不含 target）
```

**仓库已是 git 仓库并已推送到 GitHub**（`origin` = https://github.com/shanhaishi/FerroOCR.git，分支 `main`，两个提交）。
`.gitignore` 已就位：`spike/upstream/`、`**/target/`、`*.rten`/`*.mnn`/`*.onnx`、`*.exe`/`*.dll` 都不入库。

> ⚠️ **`spike/upstream/target/` 在迁移时被丢弃了**（旧产物内嵌了旧绝对路径，不可复用）。
> 开工第一件事是 `cd spike/upstream && cargo build --release --bin rusto`（约 1m50s，产出 2695680 字节的 `rusto.exe`），
> 否则 §5.5 的基线命令跑不起来。

### 4.1 现有 manifest 内容（已可用）

仓库根 `Cargo.toml`：

```toml
[workspace]
members = ["crates/ferroocr-cli"]
exclude = ["spike/upstream"]      # 必须保留，见 §5.1
resolver = "2"

[workspace.package]
version = "0.1.0"
edition = "2021"
license = "MIT"
authors = ["FerroOCR contributors"]

[profile.release]
opt-level = 3
lto = "fat"
codegen-units = 1
strip = "symbols"
panic = "unwind"
```

> 上游自己的 release profile 是 `opt-level="z"` + `panic="abort"`，我们作为外部依赖时以本 workspace 的 profile 为准，这是有意为之。

`crates/ferroocr-cli/Cargo.toml`：`[[bin]] name="ferroocr" path="src/main.rs"`，依赖 `rusto-rs = { path = "../../spike/upstream", default-features = false, features = ["parallel"] }`（发布前改为 git 依赖）。

### 4.2 上游克隆状态

- 路径：`spike/upstream/`（在仓库根下），分支 `main`
- HEAD = `731c3f44a5a6`，提交时间 `2026-11-14T11:57:39+07:00`
- **没有任何 git tag**（`git tag` 输出为空）→ 锁版本只能用 `rev = "731c3f44a5a6…"`，**不能写 `tag = "v0.4.1"`**
- 它自己的 `Cargo.toml` 里 version 已是 **0.4.1**（PRD 里写的 v0.4.0 是过时的）
- 已构建产物：`spike/upstream/target/release/rusto.exe` = **2695680 字节（2.57 MB）**——这是 Windows MSVC release 实测值，可直接当体积基准
- ⚠️ 该 `target/` 目录在迁移时已被删除，**需要先重新构建**（见 §4 末尾的警告）

---

## 5. 环境与已知坑（先看这一节，能省半天）

### 5.1 workspace 误吞上游（已修，别改回去）

`spike/upstream` 自带 `[workspace]`，如果父 workspace 不认识它，`cargo metadata` 会报：

```
error: multiple workspace roots found in the same workspace:
  .../ferroocr/spike/upstream 与 .../ferroocr
```

修法就是在仓库根 `Cargo.toml` 里加 `exclude = ["spike/upstream"]`（已加）。**不要为了"干净"删掉它。**

### 5.2 依赖缓存与网络

- `rustc 1.99.1` / `cargo 1.99.1`，toolchain `stable-x86_64-pc-windows-msvc`
- 本地 registry 缓存里**已有**：`clap 4.5.53`、`image 0.25.9`、`serde_json 1.0.154`（+ `itoa`/`memchr`/`serde_core`/`serde_derive`/`zmij`、`rten-* 0.27.0`、`windows-sys 0.48`、`thiserror 1.0.69`、`nanoserde`、`memmap2`）
- **没有**：`windows-sys 0.61`、`crossbeam-channel`、`interprocess` → M1/M2 第一次拉取需要联网 `cargo fetch`
- 实测网络：`index.crates.io` 200、`github.com` 200、`crates.io/api` 403（代理限制，不影响 cargo，走的是 sparse index）
- **实测结论：M0 的全部依赖都能离线编译**，但 `clap` 必须关掉默认 features：

  ```toml
  # ✅ 能离线（anstream/anstyle 没缓存，默认 features 会拉它们然后失败）
  clap = { version = "4.5", default-features = false,
           features = ["std", "derive", "help", "usage", "error-context"] }

  # ❌ 离线报 error: failed to download `anstream v0.6.21`
  # clap = { version = "4.5", features = ["derive"] }
  ```

  这与上游 `spike/upstream/Cargo.toml:29` 的写法完全一致，不是巧合——照抄即可。
  同理 `image` 建议 `default-features = false, features = ["png", "jpeg"]`。

- 已实测可编译的组合（`cargo check --offline` exit=0，16 秒）：

  ```toml
  rusto-rs = { path = "../../spike/upstream", default-features = false, features = ["parallel"] }
  serde_json = "1"
  clap = { version = "4.5", default-features = false, features = ["std", "derive", "help", "usage", "error-context"] }
  image = { version = "0.25", default-features = false, features = ["png", "jpeg"] }
  ```

### 5.3 Bash 工具的中文坑（务必遵守）

`ops_bash` 是 MSYS Bash，控制台代码页 GBK：

- **不要**用 heredoc 内联写含中文/emoji 的 Python 或 Rust 代码 —— 会 `SyntaxError` 或 `UnicodeEncodeError`
- **连 `print("中文")` 输出到终端也会报 `UnicodeEncodeError`**
- 正确做法：**用 `write` 工具写文件**，再用 `ops_bash` 执行 `python script.py`
- 读文件用 `ops_inspect_local_file`（带行号、UTF-8 安全），不要用 `cat`/`sed` 看中文内容
- 校验脚本跑完记得删掉，别把临时脚本留在仓里

### 5.4 本机硬件（基准可比性）

| 项 | 值 |
|---|---|
| CPU | **Intel Core i7-8750H**（6C12T，2.20 GHz） |
| 内存 | 15.9 GB |
| OS | Windows 10 专业版 10.0.19045 |
| 独显 | **NVIDIA GeForce GTX 1060**（驱动 32.0.15.8228，约 4 GB） |
| 核显 | Intel UHD Graphics 630（驱动较旧 24.20.100.6292） |
| 虚拟显示器 | MuMu Virtual Display Adapter、GameViewer Virtual Display Adapter（**测 GPU 时别选错设备**） |

> 注意：上游的性能数据是在 i7-1185G7 / Linux 上测的，**不能直接对比**。本机要用自己跑的基准。

### 5.5 已验证的基线（可复现，用来对齐输出）

```bash
cd spike
./upstream/target/release/rusto.exe ocr zh_test.png \
  --det-model models/ppocrv6-tiny_det.rten \
  --rec-model models/ppocrv6-tiny_rec.rten \
  --dict models/ppocrv6_dict.txt
```

输出单行 JSON 数组，`zh_test.png` 有 6 行，首行：

```json
{"text":"中华人民共和国机动车驾驶证","score":0.9994985,
 "box_points":[[34.114582,40.364582],[566.92706,40.364582],[566.92706,96.61458],[34.114582,96.61458]],
 "frame":{"width":532.8125,"height":56.249996,"top":40.364582,"left":34.114582}}
```

`spike/bench_result.json`（本机实测 median）：
- `example1.png` 48 行 → 0.619 s
- `invoice1.jpg` 27 行 → 0.661 s
- `idcard.jpg` 27 行 → 0.640 s
- `zh_test.png` 6 行 → 0.735 s

`spike/bench_mem.json`：`zh_test` 0.731 s / 203.5 MB；2400px 1.294 s / 286.1 MB；3600px 1.375 s / 315.4 MB。

---

## 6. 上游 API 怎么用（已核对签名，照抄即可）

```rust
use rusto::{DetectTextResult, ImageSource, InitializeConfig, OcrRunOptions, OutputGranularity, RustO, PPV6_MODEL_CONFIG};

// 1) 初始化：from_preset(preset, det, rec, dict)
//    签名是 from_preset<P: Into<PathBuf>>(preset, P, P, P) —— 三个参数同一个类型，
//    直接传 PathBuf，不是 Option（config.rs:875）
let mut config = InitializeConfig::from_preset(
    PPV6_MODEL_CONFIG,
    det_path,     // PathBuf
    rec_path,     // PathBuf
    dict_path,    // PathBuf（.txt 会被解析成 dict + 可选 meta 字段）
);
let mut ocr = RustO::initialize(config)?;          // engine.rs:766

// 2) 运行
let mut options = OcrRunOptions::default();        // types.rs:688-711，除 output 外全是 Option
options.output = OutputGranularity::Lines;
let result = ocr.detect_text(&ImageSource::Path(image_path.into()), &options)?;  // engine.rs:950

// 3) 取结果
match result {
    DetectTextResult::Structured(lines) => { /* Vec<TextResult>，见下 */ }
    DetectTextResult::Spatial(text)     => { /* 纯文本，output=Spatial 时用 */ }
}
```

`TextResult` 的完整字段（`crates/rusto-core/src/types.rs:518-527`）：

```rust
pub struct TextResult {
    pub text: String,
    pub score: f32,
    /// [左上, 右上, 右下, 左下]
    pub box_points: [(f32, f32); 4],
    /// 轴对齐包围盒 —— 我们的 schema **不要**这个
    pub frame: Frame,
    pub region_id: Option<i32>,       // 版面分析结果，M0 用不到
    pub layout_type: Option<LayoutType>,
}
```

→ 映射到我们的 `polygon` 时，`box_points` 是 `[(f32,f32);4]` 定长数组，直接 `.map(|(x,y)| [x,y])` 即可转成 `[[f32;2];4]`。

**图片宽高**：`detect_text` 的返回值里**不带**图像尺寸，我们的 `image:{width,height}` 得自己取。`image 0.25.9` 已在依赖树里（上游用），用 `image::image_dimensions(path)` 最省事——它只读文件头，不解码整张图。

上游自己的 CLI 就是这样用的，可直接对照 `spike/upstream/src/cli.rs`：
- `:394` `InitializeConfig::from_preset(...)`
- `:469` `let mut options = OcrRunOptions::default();`
- `:512` `let result = ocr.detect_text(&ImageSource::Path(image_path), &options)?;`

**关键点**：`OcrRunOptions` 各字段都是 `Option<T>`，`None` = 用引擎默认。管线开关有 `auto_unwarp` / `layout_reading` / `orientation_detection` / `line_classification` / `pre_detection` / `detection` / `post_detection` / `pre_recognition` / `recognition` / `post_recognition`。

**模型格式**：靠文件头 magic 自动识别（`slice.starts_with(b"RTEN")`，`crates/rusto-nn/src/model.rs:74`），`.rten` 不需要额外 feature。`rusto-rs` 默认 features 只有 `parallel`，实测够用。

---

## 7. 本轮（M0）具体做什么

### 7.1 要写的代码

建议先**只动 `crates/ferroocr-cli`**，把结构留到 M1 再拆 crate：

```
crates/ferroocr-cli/
├── Cargo.toml          # 加 serde_json、clap（记得 default-features=false，见 §5.2）、image
└── src/
    ├── main.rs         # 入口：解析参数 → 分派
    ├── args.rs         # 全部启动参数（对应 PRD §5.2.1）
    ├── engine.rs       # 唯一接触 upstream 的模块，封装 RustO
    ├── schema.rs       # 输出 JSON 结构（serde），把上游 → 我们的 schema
    └── run_once.rs     # L0/L1 一次性执行
```

`engine.rs` 的价值：**M1 之后要加 GPU 引擎（ORT），届时这里变成一个 trait 的两个实现。** 现在就把它写成「只暴露 `fn ocr(&mut self, path) -> Result<OcrOutput>`」的窄接口。

### 7.2 必须支持的参数（PRD §5.2.1）

M0 只需**解析并回显**，不要求全部实现行为：

```
无参数                          常驻 + L2 开 + L3 开（M0 还不能常驻，先打印提示并退出）
--no-http                       关 L2
--http                          幂等开
--port <n>                      默认 1224，占用顺延
--bind <ip>                     只允许回环，非回环直接拒绝启动
--pipe <name>                   默认 ferroocr-v1
--idle <sec>                    默认 300，0 = 永不退
--device <auto|cpu|gpu>         默认 auto
--pool <n>                      默认 min(4, cores/2)
--max-ram <mb>                  默认 2048
--log <level>                   默认 warn
--wait                          转发给已存在实例
--once                          处理完即退
--selftest
--version
--help
```

**优先级**：命令行 > 环境变量 > `endpoint.json` > 内置默认。
**环境变量**：`FERRO_HTTP_PORT`、`FERRO_IDLE`、`FERRO_DEVICE`、`FERRO_POOL`、`FERRO_MAX_RAM`、`FERRO_LOG`、`FERRO_PIPE`、`FERRO_ARGS`。
**`--help` 必须回显每个参数「生效值 + 来源」**，这是排查「为什么端口不是 1224」的唯一手段。

### 7.3 L0 命令的具体形态

```bash
# 形式一（主路径）
ferroocr.exe ocr --image "C:\scans\invoice.png" --out "C:\temp\res.json"
ferroocr.exe ocr --image "C:\scans\invoice.png" --lang ch --preset document --out res.json

# 形式二（L1：请求/结果都走文件）
ferroocr.exe ocr --in req.json --out res.json

# 模型默认从 exe 内置的释放目录找；M0 阶段允许用参数覆盖，方便调试
ferroocr.exe ocr --image a.png --out r.json \
  --det-model spike/models/ppocrv6-tiny_det.rten \
  --rec-model spike/models/ppocrv6-tiny_rec.rten \
  --dict spike/models/ppocrv6_dict.txt
```

**M0 不要求模型内置**（内置是 M3 的事）。M0 允许模型走显式路径 + `spike/models/` 兜底。

### 7.4 M0 完成标准（DoD）

1. `cargo build --release` 成功，产出 `target/release/ferroocr.exe`
2. 对 `spike/zh_test.png` 跑 `ocr --image ... --out ...`，**输出的 `lines[].text` 与上游 CLI 逐字一致**（顺序、文本、score 完全相同；只去掉 `frame` 字段、补上 `ok`/`elapsed_ms`/`image`/`text` 外层）
3. `--help` 能列出 §7.2 的全部参数，含默认值与来源说明
4. 所有参数错误（未知参数、`--bind 0.0.0.0`、`--port abc`）都返回非 0 退出码 + 可读错误，**不 panic**
5. 不存在的图片路径 → 输出 `{"ok":false,"code":"IMAGE_NOT_FOUND",...}` 到 `--out` 文件，退出码非 0
6. release exe 体积记录下来（期望 3–5 MB 量级，上游本体 2.57 MB + clap + serde_json）
7. `cargo clippy -- -D warnings` 无告警

### 7.5 本轮**不要**做的事

- ❌ HTTP / Named Pipe / 常驻进程 / 单实例互斥量（M1、M2）
- ❌ 模型 `include_bytes!` 嵌入（M3）
- ❌ ORT + DirectML（M6）
- ❌ Python / C# SDK（M4、M5）
- ❌ 任何多余依赖：不要 tokio、不要 tiny_http、不要 interprocess、不要任何 async runtime
- ❌ 不要改 `spike/upstream/` 里的任何文件（那是上游源码，只读）
- ❌ 不要"顺手"重构 PRD 里的架构

---

## 8. 工作方式约定

1. **先读文档再写代码**：`docs/PRD-FerroOCR.md` 的 §5.2.1（启动参数）、§5.3（协议与 schema）、§7（工程结构）、§8（里程碑）是本轮唯一依据。有冲突以 PRD 为准，PRD 明显过时则先告诉我再改。
2. **每步都要真跑**：不要写完就说"应该能工作"。编译、执行、贴出真实输出。
3. **中文文件用 `write` 工具写**，不要用 bash heredoc（见 §5.3）。
4. **改动要小步**：每次编译通过再往下走。M0 的目标是能跑通，不是架构漂亮。
5. **遇到与 PRD 冲突的地方**：先按 PRD 做，把分歧记下来最后一并告诉我，不要擅自扩大范围。
6. 完成后请给我：`--help` 的真实输出、一次真实识别的完整 JSON、exe 体积、以及 M1 的开工建议。

---

## 9. 开工前请回答我一个问题

读完 PRD §5.2.1 和本文件 §7.2 后，若发现**参数集与 M0 范围有不一致**（比如某个参数在 M0 就该有行为而不是只解析），先说清楚再动手。

否则直接开始，从 `crates/ferroocr-cli/src/main.rs` 写起。
