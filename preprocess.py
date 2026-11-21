"""超声 A 超预处理管线。

固定顺序（每一步都可单独关闭，但相对顺序不变）::

    带通滤波 → TGC → 希尔伯特包络 → 对数压缩(可选) → 标准化 → 归一化 0~1(可选)

数据形状为 ``(通道, 帧, 深度)``，所有运算沿深度轴向量化，避免逐帧 Python 循环。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional

import numpy as np

# 采集系统参数（固定，不开放调参）
SAMPLE_RATE_HZ = 20.0e6          # ADC 采样率 20 MHz
SOUND_SPEED_M_S = 1540.0         # 声速 1540 m/s
NYQUIST_MHZ = SAMPLE_RATE_HZ / 2.0 / 1e6                      # 10 MHz
DEPTH_PER_SAMPLE_CM = SOUND_SPEED_M_S / (2.0 * SAMPLE_RATE_HZ) * 100.0  # ≈0.00385 cm/点

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "preprocess.json"


@dataclass
class PreprocessParams:
    """预处理参数。字段顺序即管线顺序。"""

    bandpass_enabled: bool = True
    low_cut_mhz: float = 3.0
    high_cut_mhz: float = 7.0
    order: int = 4

    tgc_enabled: bool = True
    tgc_strength: float = 0.5          # 每 cm 的指数增益系数

    envelope_enabled: bool = True

    log_enabled: bool = False
    log_k: float = 0.1

    standardize_enabled: bool = True
    normalize_enabled: bool = False

    # -- 派生 ---------------------------------------------------------
    def has_enabled_steps(self) -> bool:
        return any(
            (
                self.bandpass_enabled,
                self.tgc_enabled,
                self.envelope_enabled,
                self.log_enabled,
                self.standardize_enabled,
                self.normalize_enabled,
            )
        )

    def signature(self) -> tuple:
        """参数快照，用于判断是否需要重算。"""
        return tuple(getattr(self, item.name) for item in fields(self))

    def summary(self) -> str:
        steps = []
        if self.bandpass_enabled:
            steps.append(f"带通 {self.low_cut_mhz:g}–{self.high_cut_mhz:g} MHz（{self.order} 阶）")
        if self.tgc_enabled:
            steps.append(f"TGC {self.tgc_strength:g}/cm")
        if self.envelope_enabled:
            steps.append("希尔伯特包络")
        if self.log_enabled:
            steps.append(f"对数压缩 k={self.log_k:g}")
        if self.standardize_enabled:
            steps.append("标准化")
        if self.normalize_enabled:
            steps.append("归一化 0–1")
        return " → ".join(steps) if steps else "全部关闭（等同原始数据）"

    def to_dict(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "PreprocessParams":
        valid = {item.name: item.type for item in fields(cls)}
        kwargs = {}
        for key, value in (data or {}).items():
            if key not in valid:
                continue
            field_type = valid[key]
            try:
                if field_type is bool or field_type == "bool":
                    kwargs[key] = bool(value)
                elif field_type is int or field_type == "int":
                    kwargs[key] = int(value)
                elif field_type is float or field_type == "float":
                    kwargs[key] = float(value)
                else:
                    kwargs[key] = value
            except (TypeError, ValueError):
                continue
        return cls(**kwargs)

    def copy(self) -> "PreprocessParams":
        return PreprocessParams(**self.to_dict())


# ---------------------------------------------------------------------------
# 参数持久化
# ---------------------------------------------------------------------------


def load_params(path: Path = CONFIG_PATH) -> PreprocessParams:
    """读取本地参数文件；不存在或损坏时返回默认参数。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PreprocessParams()
    if not isinstance(raw, dict):
        return PreprocessParams()
    return PreprocessParams.from_dict(raw)


def save_params(params: PreprocessParams, path: Path = CONFIG_PATH) -> None:
    """原子写入参数文件（先写临时文件再替换）。"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(
            json.dumps(params.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def validate_bandpass(params: PreprocessParams) -> Optional[str]:
    """返回错误信息；参数合法时返回 None。"""
    low = float(params.low_cut_mhz)
    high = float(params.high_cut_mhz)
    if not (0.0 < low < high < NYQUIST_MHZ):
        return f"带通范围无效：需满足 0 < 低截止 < 高截止 < {NYQUIST_MHZ:g} MHz"
    if not (1 <= int(params.order) <= 12):
        return "滤波器阶数需在 1–12 之间"
    return None


# ---------------------------------------------------------------------------
# 各处理步骤
# ---------------------------------------------------------------------------


def build_tgc_gain(depth: int, strength: float) -> np.ndarray:
    """深度方向指数增益曲线 g(d) = exp(strength * d_cm)。"""
    depth_cm = np.arange(max(1, int(depth)), dtype=np.float32) * DEPTH_PER_SAMPLE_CM
    return np.exp(float(strength) * depth_cm).astype(np.float32)


def bandpass_filter(values: np.ndarray, params: PreprocessParams) -> np.ndarray:
    """零相位巴特沃斯带通。输入/输出形状 (通道, 帧, 深度)。"""
    from scipy import signal

    error = validate_bandpass(params)
    if error:
        raise ValueError(error)

    low_hz = float(params.low_cut_mhz) * 1e6
    high_hz = float(params.high_cut_mhz) * 1e6
    sos = signal.butter(
        int(params.order),
        [low_hz, high_hz],
        btype="bandpass",
        fs=SAMPLE_RATE_HZ,
        output="sos",
    )

    depth = values.shape[-1]
    padlen = 3 * (
        2 * len(sos) + 1 - min((sos[:, 2] == 0).sum(), (sos[:, 5] == 0).sum())
    )
    if depth <= padlen:
        return signal.sosfilt(sos, values, axis=-1).astype(np.float32, copy=False)
    return signal.sosfiltfilt(sos, values, axis=-1).astype(np.float32, copy=False)


def apply_tgc(values: np.ndarray, strength: float) -> np.ndarray:
    gain = build_tgc_gain(values.shape[-1], strength)
    return (values * gain).astype(np.float32, copy=False)


def hilbert_envelope(values: np.ndarray) -> np.ndarray:
    from scipy import signal

    analytic = signal.hilbert(values, axis=-1)
    return np.abs(analytic).astype(np.float32, copy=False)


def log_compress(values: np.ndarray, k: float) -> np.ndarray:
    k = max(1e-9, float(k))
    return np.log10(1.0 + k * np.maximum(values, 0.0)).astype(np.float32, copy=False)


def standardize_per_frame(values: np.ndarray) -> np.ndarray:
    """逐帧对四通道整体做标准化：均值 0、标准差 1。"""
    mean = values.mean(axis=(0, 2), keepdims=True)
    std = values.std(axis=(0, 2), keepdims=True)
    return ((values - mean) / (std + 1e-6)).astype(np.float32, copy=False)


def normalize_per_frame(values: np.ndarray) -> np.ndarray:
    """逐帧对四通道整体归一化到 [0, 1]。"""
    low = values.min(axis=(0, 2), keepdims=True)
    high = values.max(axis=(0, 2), keepdims=True)
    span = high - low
    safe_span = np.where(span > 1e-6, span, 1.0)
    normalized = (values - low) / safe_span
    normalized = np.where(span > 1e-6, normalized, 0.0)
    return normalized.astype(np.float32, copy=False)


def apply_pipeline(
    data: np.ndarray,
    params: PreprocessParams,
    progress=None,
) -> np.ndarray:
    """按固定顺序执行预处理。

    Parameters
    ----------
    data : np.ndarray
        形状 ``(通道, 帧, 深度)`` 的原始数据，不会被修改。
    params : PreprocessParams
    progress : callable[[str], None], optional
        进度回调（步骤名称）。

    Returns
    -------
    np.ndarray
        与输入同形状的 float32 结果。
    """
    values = np.asarray(data, dtype=np.float32)
    if values.ndim != 3:
        raise ValueError(f"期望 (通道, 帧, 深度) 三维数组，实际 {values.shape}")

    if not params.has_enabled_steps():
        return np.array(values, dtype=np.float32, copy=True)

    if params.bandpass_enabled:
        if progress:
            progress("带通滤波")
        values = bandpass_filter(values, params)

    if params.tgc_enabled:
        if progress:
            progress("TGC")
        values = apply_tgc(values, params.tgc_strength)

    if params.envelope_enabled:
        if progress:
            progress("希尔伯特包络")
        values = hilbert_envelope(values)

    if params.log_enabled:
        if progress:
            progress("对数压缩")
        values = log_compress(values, params.log_k)

    if params.standardize_enabled:
        if progress:
            progress("标准化")
        values = standardize_per_frame(values)

    if params.normalize_enabled:
        if progress:
            progress("归一化")
        values = normalize_per_frame(values)

    return np.ascontiguousarray(values, dtype=np.float32)


if __name__ == "__main__":
    import sys
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from aus_data import load_recording

    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1]
        / "AUS-EMG-data"
        / "gnx"
        / "gnx_ankle_15_1_20260914_200147"
    )
    recording = load_recording(target)
    settings = PreprocessParams()
    start = time.perf_counter()
    processed = apply_pipeline(recording.data, settings, progress=lambda step: print(f"  · {step}"))
    elapsed = time.perf_counter() - start
    print(f"管线: {settings.summary()}")
    print(f"输入 {recording.data.shape} → 输出 {processed.shape}，耗时 {elapsed:.2f} s")
    print(
        f"输出统计: min={processed.min():.3f} max={processed.max():.3f} "
        f"mean={processed.mean():.3f} std={processed.std():.3f}"
    )
