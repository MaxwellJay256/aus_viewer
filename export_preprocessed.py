"""批量预处理导出脚本。

按 ``aus_viewer/config/preprocess.json`` 中保存的“当前算法参数”，把指定一级
数据目录（如 ``gnx``、``hjh``）下的全部超声记录处理并导出到新目录，保持记录
子目录结构不变。

用法::

    python export_preprocessed.py gnx "E:/A超-sEMG等速预测/processed/gnx"
    python export_preprocessed.py hjh out/hjh --format npz
    python export_preprocessed.py gnx out/gnx --limit 2      # 只处理前 2 条，用于试跑
    python export_preprocessed.py gnx out/gnx --overwrite    # 覆盖已有输出

输出结构（--format csv，默认，与原始数据同格式，可直接用查看器打开）::

    <输出目录>/
    ├── preprocess_params.json      # 本次使用的参数快照
    ├── export_manifest.csv         # 每条记录的处理状态与耗时
    └── gnx_ankle_15_1_.../
        ├── US_CH1.csv ... US_CH4.csv   # 处理后数据（Frame_ID, Timestamp, P0..Pn）
        └── info.yaml                   # 原样复制

``--format npz`` 时每个记录目录下为单个 ``US_preprocessed.npz``
（data / frame_ids / timestamps_ms），体积远小于 CSV。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from aus_data import CHANNEL_COUNT, USRecording, list_recordings, load_recording
from preprocess import CONFIG_PATH, PreprocessParams, apply_pipeline, load_params, save_params

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "AUS-EMG-data"


# ---------------------------------------------------------------------------
# 参数解析与目录定位
# ---------------------------------------------------------------------------


def resolve_input(argument: str) -> Path:
    """接受一级目录名（在 AUS-EMG-data 下查找）或直接路径。"""
    path = Path(argument)
    if path.is_dir():
        return path.resolve()
    candidate = DATA_ROOT / argument
    if candidate.is_dir():
        return candidate.resolve()
    raise SystemExit(
        f"找不到输入目录：{argument}\n"
        f"既不是现有路径，也不在 {DATA_ROOT} 下。"
    )


def find_recordings(root: Path) -> List[Path]:
    if (root / "US_CH1.csv").exists():
        return [root]
    recordings = list_recordings(root)
    if recordings:
        return recordings
    # 输入可能误传成了总数据目录，尝试向下找一层受试者目录
    nested = []
    for child in sorted(root.iterdir()):
        if child.is_dir():
            nested.extend(list_recordings(child))
    if nested:
        return nested
    raise SystemExit(f"{root} 下没有找到包含 US_CH1.csv 的记录文件夹。")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def _reference_columns(recording: USRecording):
    """从原始 CH1.csv 取回帧编号与绝对时间戳（ms），并按对齐结果取样。"""
    csv_path = recording.folder / f"US_CH1.csv"
    table = np.loadtxt(
        csv_path, delimiter=",", skiprows=1, usecols=(0, 1), dtype=np.float64
    )
    ids = table[:, 0].astype(np.int64)
    times = table[:, 1]
    order = np.argsort(ids, kind="stable")
    ids = ids[order]
    times = times[order]
    positions = np.searchsorted(ids, recording.frame_ids)
    positions = np.clip(positions, 0, len(ids) - 1)
    return recording.frame_ids, times[positions]


def _csv_header(depth: int) -> str:
    return ",".join(["Frame_ID", "Timestamp"] + [f"P{i}" for i in range(depth)])


def _csv_formats(depth: int) -> List[str]:
    return ["%d", "%.0f"] + ["%.5g"] * depth


def export_record(
    folder: Path,
    out_dir: Path,
    params: PreprocessParams,
    output_format: str,
) -> dict:
    started = time.perf_counter()
    recording = load_recording(folder)
    processed = apply_pipeline(recording.data, params)

    out_dir.mkdir(parents=True, exist_ok=True)
    frame_ids, timestamps_ms = _reference_columns(recording)

    if output_format == "npz":
        np.savez_compressed(
            out_dir / "US_preprocessed.npz",
            data=processed,
            frame_ids=frame_ids,
            timestamps_ms=timestamps_ms,
        )
    else:
        header = _csv_header(recording.n_depth)
        formats = _csv_formats(recording.n_depth)
        for channel in range(CHANNEL_COUNT):
            table = np.column_stack(
                [frame_ids, timestamps_ms, processed[channel]]
            )
            np.savetxt(
                out_dir / f"US_CH{channel + 1}.csv",
                table,
                delimiter=",",
                header=header,
                comments="",
                fmt=formats,
            )

    info_file = folder / "info.yaml"
    if info_file.exists():
        shutil.copy2(info_file, out_dir / "info.yaml")

    return {
        "record": folder.name,
        "frames": recording.n_frames,
        "depth": recording.n_depth,
        "duration_s": f"{recording.duration:.2f}",
        "elapsed_s": f"{time.perf_counter() - started:.2f}",
        "status": "ok",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="按当前预处理参数批量处理超声记录并导出到新目录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", help="一级目录名（如 gnx、hjh）或记录/受试者目录路径")
    parser.add_argument("output", help="输出目录（不存在会自动创建）")
    parser.add_argument(
        "--format",
        choices=["csv", "npz"],
        default="csv",
        help="输出格式：csv（默认，与原数据同格式，可被查看器打开）或 npz（体积小）",
    )
    parser.add_argument(
        "--params",
        type=Path,
        default=None,
        help=f"参数文件路径（默认 {CONFIG_PATH}）",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条记录（试跑用）")
    args = parser.parse_args(argv)

    try:
        input_root = resolve_input(args.input)
        recordings = find_recordings(input_root)
    except SystemExit as error:
        print(error, file=sys.stderr)
        return 2

    output_root = Path(args.output).resolve()
    if output_root == input_root or input_root in output_root.parents:
        print("输出目录不能位于输入目录内部，请换一个位置。", file=sys.stderr)
        return 2
    if not recordings:
        print("没有需要处理的记录。", file=sys.stderr)
        return 2

    params_path = args.params or CONFIG_PATH
    params = load_params(params_path)
    if args.limit > 0:
        recordings = recordings[: args.limit]

    output_root.mkdir(parents=True, exist_ok=True)
    save_params(params, output_root / "preprocess_params.json")

    print(f"输入目录：{input_root}")
    print(f"输出目录：{output_root}")
    print(f"记录数量：{len(recordings)} | 输出格式：{args.format}")
    print(f"参数来源：{params_path}")
    print(f"当前算法：{params.summary()}")
    if not params.has_enabled_steps():
        print("注意：所有处理步骤均已关闭，输出将与原始数据相同。")
    print("-" * 78)

    manifest_lines = ["record,frames,depth,duration_s,elapsed_s,status"]
    failures = 0
    total_started = time.perf_counter()

    for index, folder in enumerate(recordings, start=1):
        relative = folder.relative_to(input_root)
        label = folder.name if str(relative) == "." else str(relative)
        out_dir = output_root / relative
        expected = out_dir / (
            "US_preprocessed.npz" if args.format == "npz" else "US_CH1.csv"
        )

        if expected.exists() and not args.overwrite:
            print(f"[{index}/{len(recordings)}] {label}  已存在，跳过（--overwrite 可覆盖）")
            manifest_lines.append(f"{folder.name},,,,0,skipped")
            continue

        try:
            info = export_record(folder, out_dir, params, args.format)
        except Exception as error:  # noqa: BLE001 - 单条失败不影响其他记录
            failures += 1
            print(f"[{index}/{len(recordings)}] {label}  失败：{error}", file=sys.stderr)
            manifest_lines.append(f"{folder.name},,,,0,failed")
            continue

        manifest_lines.append(
            ",".join(
                [
                    info["record"],
                    str(info["frames"]),
                    str(info["depth"]),
                    info["duration_s"],
                    info["elapsed_s"],
                    info["status"],
                ]
            )
        )
        print(
            f"[{index}/{len(recordings)}] {label}  "
            f"{info['frames']}×{info['depth']}  完成（{info['elapsed_s']} s）"
        )

    manifest_path = output_root / "export_manifest.csv"
    manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8-sig")

    total_elapsed = time.perf_counter() - total_started
    print("-" * 78)
    print(
        f"完成：{len(recordings) - failures}/{len(recordings)} 条，"
        f"总耗时 {total_elapsed:.1f} s"
    )
    print(f"参数快照：{output_root / 'preprocess_params.json'}")
    print(f"导出清单：{manifest_path}")
    if failures:
        print(f"有 {failures} 条记录失败，详见清单。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
