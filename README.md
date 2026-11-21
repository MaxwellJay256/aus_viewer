# AUS Viewer

A mode 4 channel 超声录制数据预处理 & 可视化工具

用于查看 `AUS-EMG-data` 下超声 A 超记录的原始数据：一次加载一个记录文件夹，同时显示 4 个通道的 M 模式（瀑布流）与当前帧 A 超曲线。**本工具只读取数据，不做任何预处理或写回。**

## 环境

已创建独立 conda 环境 `us-process`（Python 3.12，不含 torch 等 ML 库）：

| 包 | 用途 |
|---|---|
| numpy / scipy | 数据处理（scipy 供后续预处理阶段使用） |
| pyqtgraph + PyQt5 | 交互式界面与图像渲染 |
| matplotlib / pillow | 后续静态对比图与截图存档 |

安装命令（pip 全局源已指向 `https://mirrors.osa.moe/pypi/web/simple`）：

```bash
"D:/Maxwe/miniconda3/envs/us-process/python.exe" -m pip install -r aus_viewer/requirements.txt
```

若某台机器的 pip 未配置镜像，追加 `-i https://mirrors.osa.moe/pypi/web/simple`。

## 运行

```bash
# 方式一：直接指定解释器
"D:/Maxwe/miniconda3/envs/us-process/python.exe" aus_viewer/mview.py
"D:/Maxwe/miniconda3/envs/us-process/python.exe" aus_viewer/mview.py "AUS-EMG-data/gnx/gnx_ankle_15_1_20260914_200147"

# 方式二：先激活环境
conda activate us-process
python aus_viewer/mview.py [记录文件夹]

# 方式三：双击 aus_viewer/run_viewer.bat
```

不带参数时自动打开 `AUS-EMG-data/gnx` 下的第一个记录；启动后可通过顶部的“同级记录”下拉框快速切换同一目录下的全部记录。

单独检查数据读取（打印帧数、深度、时长、各通道统计）：

```bash
"D:/Maxwe/miniconda3/envs/us-process/python.exe" aus_viewer/aus_data.py "AUS-EMG-data/gnx/gnx_ankle_15_1_20260914_200147"
```

## 界面说明

- **顶栏**：打开文件夹、受试者下拉框、记录下拉框、记录信息（受试者/关节/角速度/批次/帧数/时长/实际帧率）。
  - “打开文件夹”可以选**记录文件夹**、**受试者文件夹**（如 `AUS-EMG-data/hjh`）或**总数据目录**（如 `AUS-EMG-data`），程序会自动定位到具体记录。
  - 选中受试者后，“记录”下拉框会列出该受试者下的全部记录，切换即加载。
- **显示控制**
  - `伪彩`：灰度 / 绿色 / 热色 / 蓝色（手写锚点 LUT，不依赖 matplotlib）。
  - `色阶`：逐通道或全局；默认取当前范围内的 1% / 99% 分位。
  - `gamma`：亮度 gamma，折进 LUT，不重新传图像。
  - `深度`：显示用的采样点区间（起止）。
  - `时间(帧)`：显示用的帧区间。
  - `重置范围`：恢复全览。
  - `导出截图`：把整个窗口保存为 PNG（默认存到 `aus_viewer/screenshots/`）。
  - `预处理`：勾选后显示预处理结果，取消勾选显示原始数据（一键切换对比）。
  - `预处理参数…`：打开独立的参数窗口，修改实时生效。
- **2×2 M 模式**：CH1–CH4，共享时间轴，橙线为当前帧播放头；鼠标悬停时状态栏显示该点的通道/深度/帧/数值。
- **底部 A 超**：当前帧 4 通道曲线，深度范围与上方一致；上方的 `CH1`–`CH4` 勾选框可单独显示/隐藏通道（颜色与各通道一致，图例同步更新），便于聚焦观察某几个通道；勾选 `叠加原始` 后，预处理模式下会用虚线叠加同一帧的原始 A 超（按帧归一化到当前显示范围，仅用于比较形态，纵向幅度不具可比性）。
- **回放条**：播放/暂停、进度滑块、帧号与时间、回放帧率、循环。

### 快捷操作

| 操作 | 效果 |
|---|---|
| 左键点击 / 按住拖动瀑布流 | 跳转到对应帧（自动暂停播放） |
| 滚轮（在瀑布流上） | 以光标位置为中心缩放时间窗口 |
| 空格 | 播放 / 暂停 |
| ← / → | 上一帧 / 下一帧 |
| Home / End | 跳到时间窗口首帧 / 末帧 |

## 预处理

处理顺序固定为：**带通滤波 → TGC → 希尔伯特包络 → 对数压缩（可选）→ 标准化 → 归一化 0–1（可选）**。每一步都可以在参数窗口里单独勾选开关，但相对顺序不变；全部关闭时输出等同原始数据。

| 步骤 | 参数 | 默认 | 说明 |
|---|---|---|---|
| 带通滤波 | 低截止 MHz、高截止 MHz、阶数 | 3 / 7 / 4 阶 | 巴特沃斯零相位带通（`sosfiltfilt`），ADC 采样率 20 MHz，Nyquist 10 MHz；四通道共用一组参数 |
| TGC | 增益强度（每 cm） | 0.5 /cm | `g(d) = exp(强度 × 深度cm)`；按 1540 m/s、20 MHz 换算，单点 ≈ 0.00385 cm（1000 点 ≈ 3.85 cm） |
| 希尔伯特包络 | 无 | 开 | 取解析信号模长 |
| 对数压缩 | 压缩系数 k | 关，0.1 | `log10(1 + k × 包络)` |
| 标准化 | 无 | 开 | 逐帧对四通道整体 `(x-均值)/标准差`，保留通道间幅值差异 |
| 归一化 0–1 | 无 | 关 | 逐帧把四通道整体缩放到 [0, 1] |

- 参数修改后 300 ms 防抖，在后台线程重算（`PreprocessWorker`），界面不冻结；997×1000×4 一次完整管线约 0.9–1.0 s（首次因 scipy 冷启动会更久）。
- 状态栏右下角显示预处理状态（关闭 / 计算中 / 已更新耗时 / 参数错误）。
- 参数自动保存到 `aus_viewer/config/preprocess.json`，下次启动自动恢复；点“恢复默认”会重写为默认值。
- 带通参数非法（低 ≥ 高、超过 Nyquist）时输入框标红且不会提交，避免崩溃。

## 批量导出

[export_preprocessed.py](export_preprocessed.py) 按当前保存的参数（`config/preprocess.json`）批量处理一个一级目录下的所有记录，并保持子目录结构输出。

```bash
# 处理 AUS-EMG-data/gnx 下的全部记录，输出 CSV（可被查看器直接打开）
"D:/Maxwe/miniconda3/envs/us-process/python.exe" aus_viewer/export_preprocessed.py gnx "E:/processed/gnx"

# 输出紧凑的 npz（单文件，约 14 MB/条，比 CSV 小一半以上）
... export_preprocessed.py hjh "E:/processed/hjh" --format npz

# 试跑前 2 条 / 覆盖已有输出 / 指定参数文件
... export_preprocessed.py gnx out/gnx --limit 2
... export_preprocessed.py gnx out/gnx --overwrite
... export_preprocessed.py gnx out/gnx --params aus_viewer/config/preprocess.json
```

- 第一个参数可以是一级目录名（自动到 `AUS-EMG-data` 下查找），也可以是任意目录路径；指向单条记录文件夹时只处理该条。
- 输出结构：`<输出目录>/<记录名>/US_CH1..4.csv + info.yaml`（`--format npz` 时为 `US_preprocessed.npz`），另在输出根目录写入 `preprocess_params.json`（参数快照）和 `export_manifest.csv`（每条记录的状态与耗时）。
- CSV 与原始数据同格式（`Frame_ID, Timestamp, P0..Pn`，时间戳保留绝对毫秒值），因此可以直接用查看器打开导出目录查看处理结果。
- 已存在的记录默认跳过，`--overwrite` 覆盖；单条失败不会中断其他记录，最终退出码非 0。
- 体积参考：CSV 约 30 MB/条、npz 约 14 MB/条（997×1000×4）；处理+写出约 1.5–2 s/条（CSV 较慢，主要耗时在文本写入）。

## 高 DPI 显示

启动时在创建 `QApplication` 之前完成以下配置：

- 进程级 Per-Monitor DPI 感知（Windows `SetProcessDpiAwarenessContext`），避免系统对窗口做位图拉伸导致文字发虚、偏大；
- `AA_EnableHighDpiScaling` + `AA_UseHighDpiPixmaps`；
- 缩放取整策略设为 `PassThrough`：Windows 的 125%/150%/175% 缩放会按真实比例渲染，不再被取整成 100%/200%。**4K + 150% 缩放下界面过大主要就是原来的取整策略造成的。**

如果仍希望整体再小一点或大一点，可设置环境变量后启动，例如：

```bat
set AUS_UI_SCALE=0.9
"D:\Maxwe\miniconda3\envs\us-process\python.exe" aus_viewer\mview.py
```

## 数据与缓存

- 只读取 `US_CH1.csv` ~ `US_CH4.csv` 与 `info.yaml`；`US_EMG.csv`（肌电）不读取。
- CSV 列含义：`Frame_ID`、`Timestamp(ms)`、`P0..Pn`（由浅到深的 A 超采样值，有符号 8 位 ADC 原始值）。
- 首次加载一个记录会把四通道堆叠后缓存到 `aus_viewer/.cache/<记录名>_<路径哈希>.npz`，CSV 修改时间变化时自动失效；缓存目录可随时删除，不影响数据。实测首次加载约 2–4 s，命中缓存后约 0.05 s。
- **帧对齐**：部分记录的 4 个通道帧数会相差 1–2 帧（各通道独立计数）。加载时按帧编号取交集对齐，并在信息栏注明（例如“CH1-1、CH4-1”表示该通道多出的帧被裁掉）。
- 实测（`gnx_ankle_15_1_20260914_200147`）：997 帧 × 1000 点 × 4 通道，49.8 s，约 20 Hz；`gnx_multi_300_3_*`：913 帧（CH3/CH4 各多 1 帧已对齐），45.6 s。

## 目录结构

```
aus_viewer/
├── aus_data.py            # 数据读取：文件夹名解析、CSV 加载、info.yaml、缓存、批量枚举
├── preprocess.py          # 预处理管线、参数校验、参数持久化
├── preprocess_dialog.py   # 预处理参数窗口
├── export_preprocessed.py # 批量预处理导出
├── mview.py               # 查看器主程序
├── requirements.txt       # us-process 环境依赖
├── run_viewer.bat         # Windows 双击启动
├── config/                # preprocess.json（自动生成/更新）
├── screenshots/           # 导出截图（自动生成）
└── .cache/                # npz 缓存（自动生成）
```

## 已知限制

- 时间轴按每帧时间戳映射；采样间隔不均时图像在时间方向上是按帧等宽拉伸的近似显示。
- 预处理结果只保存在内存中，不落盘；切换记录或修改参数后需要重新计算（约 1 s）。
- 预处理参数是四通道共用的，暂不支持每通道单独设置带通。
