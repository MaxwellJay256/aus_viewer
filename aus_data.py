"""AUS 超声数据读取模块。

目录结构约定（以 ``gnx_ankle_15_1_20260914_200147`` 为例）：
    gnx              受试者
    ankle            关节位置
    15               关节角速度（度/秒）
    1                实验批次
    20260914_200147  记录日期与时间

目录内每个 ``US_CHn.csv``：
    第 1 列     帧编号
    第 2 列     时间戳（ms）
    第 3 列起   一个完整 A 超扫描帧的采样值（由浅到深），有符号 8 位 ADC 原始值
    ``US_EMG.csv`` 为肌电数据，本模块不读取。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

CHANNEL_COUNT = 4
_CSV_PATTERN = "US_CH{}.csv"
_CACHE_DIR = Path(__file__).resolve().parent / ".cache"


@dataclass
class RecordingMeta:
    """一次记录（一个文件夹）的元信息。"""

    subject: str = ""
    joint: str = ""
    velocity: Optional[float] = None
    batch: str = ""
    date: str = ""
    time: str = ""
    start_time: str = ""
    duration_sec: Optional[float] = None
    total_frames: Optional[int] = None
    loss_count: Dict[str, int] = field(default_factory=dict)
    dropped_frames: Dict[str, int] = field(default_factory=dict)

    def describe(self) -> str:
        velocity = f"{self.velocity:g}°/s" if self.velocity is not None else "?角度"
        return (
            f"{self.subject} | {self.joint} | {velocity} | 第 {self.batch} 批 | "
            f"{self.date} {self.time}"
        )


@dataclass
class USRecording:
    """四通道 A 超数据。``data`` 形状为 (通道, 帧, 深度)。"""

    folder: Path
    meta: RecordingMeta
    data: np.ndarray
    frame_ids: np.ndarray
    timestamps: np.ndarray  # 相对首帧的时间（秒）

    @property
    def n_channels(self) -> int:
        return int(self.data.shape[0])

    @property
    def n_frames(self) -> int:
        return int(self.data.shape[1])

    @property
    def n_depth(self) -> int:
        return int(self.data.shape[2])

    @property
    def duration(self) -> float:
        if self.timestamps.size < 2:
            return 0.0
        return float(self.timestamps[-1] - self.timestamps[0])

    @property
    def fps(self) -> float:
        duration = self.duration
        if duration <= 0 or self.n_frames < 2:
            return 0.0
        return float((self.n_frames - 1) / duration)

    def summary(self) -> str:
        text = (
            f"{self.n_frames} 帧 × {self.n_depth} 点 × {self.n_channels} 通道 | "
            f"{self.duration:.2f} s | 实际帧率 {self.fps:.1f} Hz"
        )
        if self.meta.dropped_frames:
            detail = "、".join(
                f"{channel}-{count}"
                for channel, count in sorted(self.meta.dropped_frames.items())
            )
            text += f" | 通道帧数不一致，已按帧号对齐（{detail}）"
        return text


# ---------------------------------------------------------------------------
# 目录名与 info.yaml 解析
# ---------------------------------------------------------------------------


def parse_folder_name(name: str) -> RecordingMeta:
    """从文件夹名解析受试者/关节/角速度/批次/时间。"""
    parts = name.split("_")
    meta = RecordingMeta()
    if len(parts) >= 6:
        meta.date, meta.time = parts[-2], parts[-1]
        meta.batch = parts[-3]
        try:
            meta.velocity = float(parts[-4])
        except ValueError:
            meta.velocity = None
        meta.joint = parts[-5]
        meta.subject = "_".join(parts[:-5])
    else:
        meta.subject = name
    return meta


def _parse_info_yaml(path: Path, meta: RecordingMeta) -> RecordingMeta:
    """轻量解析 info.yaml（不依赖 PyYAML）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return meta

    match = re.search(r"Start_Time:\s*'?\"?([^'\"\n]+)'?\"?", text)
    if match:
        meta.start_time = match.group(1).strip()

    match = re.search(r"Duration_sec:\s*([\d.]+)", text)
    if match:
        try:
            meta.duration_sec = float(match.group(1))
        except ValueError:
            pass

    match = re.search(r"Total_Frames:\s*(\d+)", text)
    if match:
        meta.total_frames = int(match.group(1))

    for channel, _frames, loss in re.findall(
        r"(CH\d):\s*Total_Frames:\s*(\d+)\s*Loss_Count:\s*(\d+)", text
    ):
        meta.loss_count[channel] = int(loss)

    return meta


# ---------------------------------------------------------------------------
# CSV 读取与缓存
# ---------------------------------------------------------------------------


def _read_channel_csv(path: Path):
    """返回 (帧编号, 时间戳毫秒, 采样矩阵[n_frames, n_depth])。"""
    data = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.float64)
    if data.ndim == 1:
        data = data[None, :]
    frame_ids = data[:, 0].astype(np.int64)
    timestamps_ms = data[:, 1]
    samples = data[:, 2:].astype(np.float32)
    return frame_ids, timestamps_ms, samples


def _cache_path(folder: Path) -> Path:
    digest = hashlib.md5(str(folder.resolve()).encode("utf-8")).hexdigest()[:8]
    return _CACHE_DIR / f"{folder.name}_{digest}.npz"


def _csv_mtime(folder: Path) -> float:
    mtime = 0.0
    for channel in range(1, CHANNEL_COUNT + 1):
        csv = folder / _CSV_PATTERN.format(channel)
        if csv.exists():
            mtime = max(mtime, csv.stat().st_mtime)
    return mtime


def _load_from_cache(folder: Path) -> Optional[USRecording]:
    cache_file = _cache_path(folder)
    if not cache_file.exists():
        return None
    try:
        with np.load(cache_file, allow_pickle=False) as cache:
            cached_mtime = float(cache["mtime"])
            if abs(cached_mtime - _csv_mtime(folder)) > 1.0:
                return None
            data = cache["data"]
            frame_ids = cache["frame_ids"]
            timestamps = cache["timestamps"]
            meta = RecordingMeta(**json.loads(str(cache["meta"].item())))
    except (OSError, KeyError, ValueError):
        return None
    return USRecording(
        folder=folder, meta=meta, data=data, frame_ids=frame_ids, timestamps=timestamps
    )


def _save_to_cache(recording: USRecording) -> None:
    cache_file = _cache_path(recording.folder)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        meta_json = json.dumps(dict(recording.meta.__dict__), ensure_ascii=False)
        np.savez_compressed(
            cache_file,
            data=recording.data,
            frame_ids=recording.frame_ids,
            timestamps=recording.timestamps,
            meta=np.array(meta_json),
            mtime=np.array(_csv_mtime(recording.folder)),
        )
    except OSError:
        pass  # 缓存失败不影响正常使用


def load_recording(folder: str | Path, use_cache: bool = True) -> USRecording:
    """读取一个记录文件夹中的 4 通道超声数据。

    Parameters
    ----------
    folder : 记录文件夹路径
    use_cache : 是否使用 ``aus_viewer/.cache`` 加速重复加载
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"不是有效文件夹: {folder}")

    if use_cache:
        cached = _load_from_cache(folder)
        if cached is not None:
            return cached

    meta = parse_folder_name(folder.name)
    info_file = folder / "info.yaml"
    if info_file.exists():
        meta = _parse_info_yaml(info_file, meta)

    channel_ids: List[np.ndarray] = []
    channel_times: List[np.ndarray] = []
    channels: List[np.ndarray] = []

    for channel in range(1, CHANNEL_COUNT + 1):
        csv = folder / _CSV_PATTERN.format(channel)
        if not csv.exists():
            raise FileNotFoundError(f"缺少通道文件: {csv}")
        ids, times, samples = _read_channel_csv(csv)
        order = np.argsort(ids, kind="stable")
        channel_ids.append(ids[order])
        channel_times.append(times[order])
        channels.append(samples[order])

    depths = {channel.shape[1] for channel in channels}
    if len(depths) != 1:
        raise ValueError(f"各通道深度不一致: {sorted(depths)}")

    # 不同通道可能相差 1~2 帧（各通道独立计数），按帧编号取交集对齐
    common_ids = channel_ids[0]
    for ids in channel_ids[1:]:
        common_ids = np.intersect1d(common_ids, ids, assume_unique=True)
    if common_ids.size == 0:
        raise ValueError("四个通道没有共同帧")

    aligned: List[np.ndarray] = []
    for channel, ids in enumerate(channel_ids):
        positions = np.searchsorted(ids, common_ids)
        aligned.append(channels[channel][positions])
        dropped = int(ids.size - common_ids.size)
        if dropped:
            meta.dropped_frames[f"CH{channel + 1}"] = dropped

    data = np.stack(aligned, axis=0)
    reference_positions = np.searchsorted(channel_ids[0], common_ids)
    timestamps_ms = channel_times[0][reference_positions]
    frame_ids = common_ids
    timestamps = (timestamps_ms - timestamps_ms[0]) / 1000.0

    recording = USRecording(
        folder=folder,
        meta=meta,
        data=data,
        frame_ids=frame_ids if frame_ids is not None else np.arange(data.shape[1]),
        timestamps=timestamps,
    )
    if use_cache:
        _save_to_cache(recording)
    return recording


def list_recordings(root: str | Path) -> List[Path]:
    """列出根目录下所有包含 US_CH1.csv 的记录文件夹（按名称排序）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        [
            path
            for path in root.iterdir()
            if path.is_dir() and (path / "US_CH1.csv").exists()
        ],
        key=lambda path: path.name,
    )


if __name__ == "__main__":
    import sys
    import time

    default_root = Path(__file__).resolve().parents[1] / "AUS-EMG-data" / "gnx"
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        found = list_recordings(default_root)
        if not found:
            raise SystemExit(f"未找到记录: {default_root}")
        target = found[0]

    start = time.perf_counter()
    recording = load_recording(target)
    elapsed = time.perf_counter() - start

    print(recording.meta.describe())
    print(recording.summary())
    print(f"加载耗时 {elapsed:.2f} s | 丢失帧统计 {recording.meta.loss_count or '无'}")
    for channel in range(recording.n_channels):
        block = recording.data[channel]
        print(
            f"CH{channel + 1}: min={block.min():.1f} max={block.max():.1f} "
            f"mean={block.mean():.2f} std={block.std():.2f}"
        )
