"""
AUS 四通道超声 M 模式（瀑布流）查看器。

用法：
    python mview.py [记录文件夹]

不带参数时打开 ``AUS-EMG-data/gnx`` 下的第一个记录。界面上方可切换同级记录、
调节伪彩 / 色阶 / 深度与时间范围，下方回放并同步显示当前帧 A 超曲线。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

from aus_data import USRecording, list_recordings, load_recording
from preprocess import (
    DisplayParams,
    PreprocessParams,
    apply_pipeline,
    load_display_params,
    load_params,
    save_params,
    validate_bandpass,
)
from preprocess_dialog import PreprocessDialog

CHANNEL_COLORS = ["#4e9a06", "#3465a4", "#c17d11", "#cc0000"]
CHANNEL_COUNT = 4

# 伪彩锚点：位置 0~1 与对应 RGB
COLORMAP_ANCHORS = {
    "灰度": [(0.0, (0, 0, 0)), (0.5, (128, 128, 128)), (1.0, (255, 255, 255))],
    "绿色": [
        (0.0, (0, 0, 0)),
        (0.35, (0, 70, 0)),
        (0.70, (40, 170, 40)),
        (1.0, (180, 255, 180)),
    ],
    "热色": [
        (0.0, (0, 0, 0)),
        (0.33, (150, 0, 0)),
        (0.66, (255, 140, 0)),
        (1.0, (255, 255, 210)),
    ],
    "蓝色": [
        (0.0, (0, 0, 0)),
        (0.35, (0, 40, 90)),
        (0.70, (0, 120, 200)),
        (1.0, (170, 235, 255)),
    ],
}


def build_lut(colormap_name: str, gamma: float = 1.0, size: int = 256) -> np.ndarray:
    """生成 pyqtgraph 使用的 (size, 4) uint8 查找表；gamma 已折算进 LUT。"""
    anchors = COLORMAP_ANCHORS.get(colormap_name, COLORMAP_ANCHORS["灰度"])
    positions = np.array([item[0] for item in anchors], dtype=np.float64)
    colors = np.array([item[1] for item in anchors], dtype=np.float64)
    grid = np.linspace(0.0, 1.0, size)
    rgb = np.stack(
        [np.interp(grid, positions, colors[:, channel]) for channel in range(3)], axis=1
    )
    if abs(gamma - 1.0) > 1e-3:
        source = (grid ** (1.0 / gamma)) * (size - 1)
        rgb = rgb[np.clip(np.round(source).astype(int), 0, size - 1)]
    lut = np.zeros((size, 4), dtype=np.ubyte)
    lut[:, :3] = np.clip(rgb, 0, 255).astype(np.ubyte)
    lut[:, 3] = 255
    return lut


class MModePlot(pg.PlotWidget):
    """单个通道的 M 模式面板：点击/拖动定位帧，滚轮缩放时间窗口。"""

    seekRequested = pyqtSignal(int, float)
    wheelZoomed = pyqtSignal(int, float, float)
    hovered = pyqtSignal(int, float, float)

    def __init__(self, channel_index: int, parent=None):
        super().__init__(parent=parent, background="w")
        self.channel_index = channel_index
        self.setMinimumSize(320, 220)
        self.showGrid(x=True, y=True, alpha=0.12)
        self.setLabel("bottom", "时间 (s)", size="9pt")
        self.setLabel("left", "深度 (采样点)", size="9pt")
        self.setMouseEnabled(x=False, y=False)
        self.getViewBox().invertY(True)
        self.getViewBox().setDefaultPadding(0.0)
        self.setMenuEnabled(False)
        self.hideButtons()

        self.image = pg.ImageItem()
        self.addItem(self.image)
        self.playhead = pg.InfiniteLine(
            pos=0.0, angle=90, movable=False, pen=pg.mkPen("#ff9900", width=1.5)
        )
        self.addItem(self.playhead)

        self.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.scene().sigMouseClicked.connect(self._on_mouse_clicked)

    # -- 鼠标交互 ---------------------------------------------------------
    def _map_scene_pos(self, pos):
        return self.getViewBox().mapSceneToView(pos)

    def _on_mouse_moved(self, pos):
        point = self._map_scene_pos(pos)
        self.hovered.emit(self.channel_index, float(point.x()), float(point.y()))
        if QtWidgets.QApplication.mouseButtons() & Qt.LeftButton:
            self.seekRequested.emit(self.channel_index, float(point.x()))

    def _on_mouse_clicked(self, event):
        point = self._map_scene_pos(event.scenePos())
        self.seekRequested.emit(self.channel_index, float(point.x()))

    def wheelEvent(self, event):
        point = self._map_scene_pos(self.mapToScene(event.pos()))
        delta = event.angleDelta().y()
        if delta:
            factor = 0.8 if delta > 0 else 1.25
            self.wheelZoomed.emit(self.channel_index, factor, float(point.x()))
            event.accept()
            return
        super().wheelEvent(event)


class PreprocessWorker(QtCore.QThread):
    """后台执行预处理管线，避免参数调整时界面卡顿。"""

    resultReady = pyqtSignal(int, object, float)

    def __init__(
        self,
        token: int,
        data: np.ndarray,
        params: PreprocessParams,
        parent=None,
    ):
        super().__init__(parent)
        self._token = token
        self._data = data
        self._params = params.copy()

    def run(self):
        started = time.perf_counter()
        try:
            result = apply_pipeline(self._data, self._params)
        except Exception as error:  # noqa: BLE001 - 交给主线程提示
            result = error
        self.resultReady.emit(self._token, result, time.perf_counter() - started)


class ViewerWindow(QtWidgets.QMainWindow):
    def __init__(self, folder: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("AUS 四通道瀑布流查看器")
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(1680, int(available.width() * 0.92)),
                min(1020, int(available.height() * 0.92)),
            )
        else:
            self.resize(1680, 1020)

        self.rec: Optional[USRecording] = None
        self.frame = 0
        self.t0 = 0
        self.t1 = 1
        self.d0 = 0
        self.d1 = 1

        self._plots: List[MModePlot] = []
        self._a_curves: List[pg.PlotDataItem] = []
        self._a_raw_curves: List[pg.PlotDataItem] = []
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._updating_controls = False

        # 预处理状态
        self._params = load_params()
        self._display_params = load_display_params()
        self._show_processed = False
        self._proc_data: Optional[np.ndarray] = None
        self._proc_signature: Optional[tuple] = None
        self._proc_token = 0
        self._proc_thread: Optional[PreprocessWorker] = None
        self._pending_params: Optional[PreprocessParams] = None
        self._dialog: Optional[PreprocessDialog] = None

        self._compute_timer = QtCore.QTimer(self)
        self._compute_timer.setSingleShot(True)
        self._compute_timer.setInterval(300)
        self._compute_timer.timeout.connect(self._start_preprocess)

        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_params)

        self._build_ui()
        self.gamma_spin.setValue(self._display_params.get(self._show_processed))

        if folder is not None:
            self.load_folder(Path(folder))
        else:
            self._load_default()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 第一行：文件夹与记录信息
        top_row = QtWidgets.QHBoxLayout()
        self.open_button = QtWidgets.QPushButton("打开文件夹…")
        self.open_button.clicked.connect(self._choose_folder)
        top_row.addWidget(self.open_button)

        top_row.addWidget(QtWidgets.QLabel("受试者："))
        self.subject_combo = QtWidgets.QComboBox()
        self.subject_combo.setMinimumWidth(150)
        self.subject_combo.activated.connect(self._on_subject_selected)
        top_row.addWidget(self.subject_combo)

        top_row.addWidget(QtWidgets.QLabel("记录："))
        self.record_combo = QtWidgets.QComboBox()
        self.record_combo.setMinimumWidth(380)
        self.record_combo.activated.connect(self._on_record_selected)
        top_row.addWidget(self.record_combo, 1)

        self.info_label = QtWidgets.QLabel("未加载数据")
        self.info_label.setStyleSheet("color:#333;")
        top_row.addWidget(self.info_label, 2)
        root.addLayout(top_row)

        # 第二行：显示控制
        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)

        controls.addWidget(QtWidgets.QLabel("伪彩："))
        self.colormap_combo = QtWidgets.QComboBox()
        self.colormap_combo.addItems(list(COLORMAP_ANCHORS.keys()))
        self.colormap_combo.setCurrentText("绿色")
        self.colormap_combo.currentTextChanged.connect(self._on_display_changed)
        controls.addWidget(self.colormap_combo)

        controls.addWidget(QtWidgets.QLabel("色阶："))
        self.level_combo = QtWidgets.QComboBox()
        self.level_combo.addItems(["逐通道", "全局"])
        self.level_combo.currentTextChanged.connect(self._on_display_changed)
        controls.addWidget(self.level_combo)

        controls.addWidget(QtWidgets.QLabel("低百分位："))
        self.low_spin = QtWidgets.QDoubleSpinBox()
        self.low_spin.setRange(0.0, 49.0)
        self.low_spin.setSingleStep(0.5)
        self.low_spin.setValue(1.0)
        self.low_spin.setDecimals(1)
        self.low_spin.setFixedWidth(72)
        self.low_spin.valueChanged.connect(self._on_display_changed)
        controls.addWidget(self.low_spin)

        controls.addWidget(QtWidgets.QLabel("高百分位："))
        self.high_spin = QtWidgets.QDoubleSpinBox()
        self.high_spin.setRange(51.0, 100.0)
        self.high_spin.setSingleStep(0.5)
        self.high_spin.setValue(99.0)
        self.high_spin.setDecimals(1)
        self.high_spin.setFixedWidth(72)
        self.high_spin.valueChanged.connect(self._on_display_changed)
        controls.addWidget(self.high_spin)

        controls.addWidget(QtWidgets.QLabel("gamma："))
        self.gamma_spin = QtWidgets.QDoubleSpinBox()
        self.gamma_spin.setRange(0.1, 3.0)
        self.gamma_spin.setSingleStep(0.1)
        self.gamma_spin.setValue(self._display_params.get(False))
        self.gamma_spin.setDecimals(2)
        self.gamma_spin.setFixedWidth(72)
        self.gamma_spin.valueChanged.connect(self._on_display_changed)
        controls.addWidget(self.gamma_spin)

        controls.addWidget(QtWidgets.QLabel("深度："))
        self.depth_start_spin = QtWidgets.QSpinBox()
        self.depth_end_spin = QtWidgets.QSpinBox()
        for spin in (self.depth_start_spin, self.depth_end_spin):
            spin.setFixedWidth(76)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._on_range_changed)
        controls.addWidget(self.depth_start_spin)
        controls.addWidget(QtWidgets.QLabel("~"))
        controls.addWidget(self.depth_end_spin)

        controls.addWidget(QtWidgets.QLabel("时间(帧)："))
        self.time_start_spin = QtWidgets.QSpinBox()
        self.time_end_spin = QtWidgets.QSpinBox()
        for spin in (self.time_start_spin, self.time_end_spin):
            spin.setFixedWidth(76)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._on_range_changed)
        controls.addWidget(self.time_start_spin)
        controls.addWidget(QtWidgets.QLabel("~"))
        controls.addWidget(self.time_end_spin)

        self.reset_button = QtWidgets.QPushButton("重置范围")
        self.reset_button.clicked.connect(self._reset_ranges)
        controls.addWidget(self.reset_button)

        self.export_button = QtWidgets.QPushButton("导出截图")
        self.export_button.clicked.connect(self._export_screenshot)
        controls.addWidget(self.export_button)

        controls.addSpacing(10)
        self.preprocess_check = QtWidgets.QCheckBox("预处理")
        self.preprocess_check.setToolTip("开启后显示预处理结果，关闭则显示原始数据")
        self.preprocess_check.toggled.connect(self._on_preprocess_toggled)
        controls.addWidget(self.preprocess_check)

        self.preprocess_button = QtWidgets.QPushButton("预处理参数…")
        self.preprocess_button.clicked.connect(self._open_preprocess_dialog)
        controls.addWidget(self.preprocess_button)
        controls.addStretch(1)
        root.addLayout(controls)

        # 2×2 M 模式网格
        grid_widget = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for channel in range(CHANNEL_COUNT):
            cell = QtWidgets.QWidget()
            cell_layout = QtWidgets.QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)

            title = QtWidgets.QLabel(f"CH{channel + 1}")
            title.setAlignment(Qt.AlignCenter)
            title.setStyleSheet(
                f"color:{CHANNEL_COLORS[channel]}; font-weight:bold; font-size:11pt;"
            )
            cell_layout.addWidget(title)

            plot = MModePlot(channel)
            plot.seekRequested.connect(self._on_seek)
            plot.wheelZoomed.connect(self._on_wheel_zoom)
            plot.hovered.connect(self._on_hover)
            cell_layout.addWidget(plot, 1)

            self._plots.append(plot)
            row, col = divmod(channel, 2)
            grid.addWidget(cell, row, col)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        root.addWidget(grid_widget, 5)

        # 底部 A 超
        a_mode_header = QtWidgets.QHBoxLayout()
        a_mode_header.addWidget(QtWidgets.QLabel("当前帧 A 超（同深度窗口）"))

        a_mode_header.addSpacing(16)
        a_mode_header.addWidget(QtWidgets.QLabel("通道："))
        self.channel_checks: List[QtWidgets.QCheckBox] = []
        for channel in range(CHANNEL_COUNT):
            check = QtWidgets.QCheckBox(f"CH{channel + 1}")
            check.setChecked(True)
            check.setStyleSheet(f"color:{CHANNEL_COLORS[channel]}; font-weight:bold;")
            check.toggled.connect(
                lambda checked, index=channel: self._on_channel_visibility_changed(index, checked)
            )
            a_mode_header.addWidget(check)
            self.channel_checks.append(check)

        a_mode_header.addSpacing(12)
        self.raw_overlay_check = QtWidgets.QCheckBox("叠加原始")
        self.raw_overlay_check.setToolTip(
            "显示预处理结果时，叠加同一帧的原始 A 超（按帧归一化到当前显示范围，仅用于比较形态）"
        )
        self.raw_overlay_check.toggled.connect(self._on_raw_overlay_toggled)
        a_mode_header.addWidget(self.raw_overlay_check)

        a_mode_header.addStretch(1)
        self.a_mode_info = QtWidgets.QLabel("")
        a_mode_header.addWidget(self.a_mode_info)
        root.addLayout(a_mode_header)

        self.a_plot = pg.PlotWidget(background="w")
        self.a_plot.setMinimumHeight(170)
        self.a_plot.showGrid(x=True, y=True, alpha=0.15)
        self.a_plot.setLabel("bottom", "深度 (采样点)", size="9pt")
        self.a_plot.setLabel("left", "ADC 值", size="9pt")
        self.a_plot.hideButtons()
        self._a_legend = self.a_plot.addLegend(
            offset=(0, 0),
            brush=pg.mkBrush(255, 255, 255, 225),
            pen=pg.mkPen("#bbbbbb"),
            labelTextColor="#333333",
        )
        # 图例放到右上角，避开浅层强回波
        self._a_legend.anchor((1, 0), (1, 0), offset=(-10, 8))
        self.a_plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("#999999")))
        for channel in range(CHANNEL_COUNT):
            curve = self.a_plot.plot(
                pen=pg.mkPen(CHANNEL_COLORS[channel], width=1.2),
                name=f"CH{channel + 1}",
            )
            self._a_curves.append(curve)

        # 预处理模式下用于叠加的原始虚线（默认隐藏，不进入图例）
        for channel in range(CHANNEL_COUNT):
            curve = self.a_plot.plot(
                pen=pg.mkPen(
                    CHANNEL_COLORS[channel], width=1.0, style=Qt.DashLine
                )
            )
            curve.setVisible(False)
            self._a_raw_curves.append(curve)
        root.addWidget(self.a_plot, 2)

        # 回放控制
        playback = QtWidgets.QHBoxLayout()
        self.play_button = QtWidgets.QPushButton("播放")
        self.play_button.setCheckable(True)
        self.play_button.toggled.connect(self._on_play_toggled)
        playback.addWidget(self.play_button)

        self.slider = QtWidgets.QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        playback.addWidget(self.slider, 1)

        self.frame_label = QtWidgets.QLabel("0 / 0   0.00 s")
        self.frame_label.setMinimumWidth(200)
        playback.addWidget(self.frame_label)

        playback.addWidget(QtWidgets.QLabel("回放帧率："))
        self.fps_spin = QtWidgets.QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 60.0)
        self.fps_spin.setValue(20.0)
        self.fps_spin.setDecimals(1)
        self.fps_spin.setFixedWidth(70)
        self.fps_spin.valueChanged.connect(self._on_fps_changed)
        playback.addWidget(self.fps_spin)

        self.loop_check = QtWidgets.QCheckBox("循环")
        self.loop_check.setChecked(True)
        playback.addWidget(self.loop_check)
        root.addLayout(playback)

        self.setCentralWidget(central)

        self._status = QtWidgets.QLabel("提示：左键点击/拖动瀑布流可跳帧，滚轮缩放时间窗口，空格播放/暂停")
        self.statusBar().addWidget(self._status, 1)

        self._proc_status = QtWidgets.QLabel("预处理：关（显示原始）")
        self.statusBar().addPermanentWidget(self._proc_status)

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def _load_default(self):
        default_root = Path(__file__).resolve().parents[1] / "AUS-EMG-data" / "gnx"
        found = list_recordings(default_root)
        if found:
            self.load_folder(found[0])
        else:
            self.info_label.setText(f"未找到数据（{default_root}）")

    def _choose_folder(self):
        start_dir = (
            str(self.rec.folder.parent)
            if self.rec is not None
            else str(Path(__file__).resolve().parents[1] / "AUS-EMG-data" / "gnx")
        )
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选择记录文件夹", start_dir)
        if folder:
            self.load_folder(Path(folder))

    def load_folder(self, target: Path):
        target = Path(target)
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            folder = self._resolve_recording(target)
            recording = load_recording(folder)
        except Exception as error:  # noqa: BLE001 - 直接反馈给用户
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, "加载失败", f"{target}\n\n{error}")
            return
        finally:
            if QtWidgets.QApplication.overrideCursor() is not None:
                QtWidgets.QApplication.restoreOverrideCursor()

        self._timer.stop()
        self.play_button.setChecked(False)
        self.rec = recording
        self.frame = 0

        # 记录切换后旧的预处理结果失效
        self._proc_data = None
        self._proc_signature = None
        self._proc_token += 1

        self.setWindowTitle(f"AUS 四通道瀑布流查看器 — {folder.name}")
        self.info_label.setText(f"{recording.meta.describe()}\n{recording.summary()}")
        self.fps_spin.setValue(round(recording.fps, 1) or 20.0)

        self._populate_navigation(folder)
        self._configure_ranges()
        self._rebuild_images()
        self._set_frame(0)

        if self._show_processed:
            self._proc_status.setText("预处理：计算中…")
            self._start_preprocess()

    @staticmethod
    def _resolve_recording(target: Path) -> Path:
        """把用户选中的路径解析为具体的记录文件夹。

        支持三种选择：记录文件夹本身、受试者文件夹（如 ``AUS-EMG-data/hjh``）、
        以及受试者上层的总数据目录。
        """
        if (target / "US_CH1.csv").exists():
            return target.resolve()
        if not target.is_dir():
            raise NotADirectoryError(f"不是有效文件夹: {target}")

        recordings = list_recordings(target)
        if recordings:
            return recordings[0].resolve()

        for child in sorted(target.iterdir()):
            if child.is_dir():
                found = list_recordings(child)
                if found:
                    return found[0].resolve()

        raise FileNotFoundError(f"{target} 下没有找到包含 US_CH1.csv 的记录文件夹")

    def _populate_navigation(self, folder: Path):
        folder = folder.resolve()
        subject_root = folder.parent
        data_root = subject_root.parent

        subjects = []
        if data_root.is_dir():
            subjects = [
                path.resolve()
                for path in sorted(data_root.iterdir())
                if path.is_dir() and list_recordings(path)
            ]
        if subject_root not in subjects:
            subjects.insert(0, subject_root)

        self.subject_combo.blockSignals(True)
        self.subject_combo.clear()
        for subject in subjects:
            self.subject_combo.addItem(subject.name, str(subject.resolve()))
        index = self.subject_combo.findData(str(subject_root))
        if index >= 0:
            self.subject_combo.setCurrentIndex(index)
        self.subject_combo.blockSignals(False)

        self._fill_record_combo(subject_root, folder)

    def _fill_record_combo(self, subject_root: Path, selected: Optional[Path] = None):
        recordings = list_recordings(subject_root)
        self.record_combo.blockSignals(True)
        self.record_combo.clear()
        for path in recordings:
            self.record_combo.addItem(path.name, str(path.resolve()))
        if selected is not None:
            index = self.record_combo.findData(str(selected.resolve()))
            if index >= 0:
                self.record_combo.setCurrentIndex(index)
        self.record_combo.blockSignals(False)

    def _on_subject_selected(self, index: int):
        data = self.subject_combo.itemData(index)
        if not data:
            return
        recordings = list_recordings(Path(data))
        if not recordings:
            QtWidgets.QMessageBox.warning(self, "没有记录", f"{data} 下没有有效记录")
            return
        self.load_folder(recordings[0])

    def _on_record_selected(self, index: int):
        data = self.record_combo.itemData(index)
        if data:
            self.load_folder(Path(data))

    # ------------------------------------------------------------------
    # 范围与图像
    # ------------------------------------------------------------------
    def _configure_ranges(self):
        assert self.rec is not None
        self._updating_controls = True
        try:
            self.time_start_spin.setRange(0, self.rec.n_frames - 1)
            self.time_end_spin.setRange(1, self.rec.n_frames)
            self.depth_start_spin.setRange(0, self.rec.n_depth - 1)
            self.depth_end_spin.setRange(1, self.rec.n_depth)
            self.time_start_spin.setValue(0)
            self.time_end_spin.setValue(self.rec.n_frames)
            self.depth_start_spin.setValue(0)
            self.depth_end_spin.setValue(self.rec.n_depth)
        finally:
            self._updating_controls = False

        self.t0, self.t1 = 0, self.rec.n_frames
        self.d0, self.d1 = 0, self.rec.n_depth

        self._update_a_mode_range()

        self.slider.blockSignals(True)
        self.slider.setRange(0, self.rec.n_frames - 1)
        self.slider.setValue(0)
        self.slider.blockSignals(False)

    def _on_range_changed(self, _value=None):
        if self.rec is None or self._updating_controls:
            return
        t0 = self.time_start_spin.value()
        t1 = self.time_end_spin.value()
        d0 = self.depth_start_spin.value()
        d1 = self.depth_end_spin.value()
        if t1 <= t0:
            t1 = min(self.rec.n_frames, t0 + 1)
        if d1 <= d0:
            d1 = min(self.rec.n_depth, d0 + 1)
        self.t0, self.t1, self.d0, self.d1 = t0, t1, d0, d1
        self._rebuild_images()

    def _reset_ranges(self):
        if self.rec is None:
            return
        self._configure_ranges()
        self._rebuild_images()
        self._set_frame(min(self.frame, self.rec.n_frames - 1))

    def _set_time_window(self, new_t0: int, new_t1: int):
        if self.rec is None:
            return
        new_t0 = max(0, min(new_t0, self.rec.n_frames - 2))
        new_t1 = max(new_t0 + 2, min(new_t1, self.rec.n_frames))
        self._updating_controls = True
        try:
            self.time_start_spin.setValue(new_t0)
            self.time_end_spin.setValue(new_t1)
        finally:
            self._updating_controls = False
        self.t0, self.t1 = new_t0, new_t1
        self._rebuild_images()

    def _rebuild_images(self):
        if self.rec is None:
            return
        crop = self._active_data()[:, self.t0 : self.t1, self.d0 : self.d1]
        levels = self._compute_levels(crop)
        lut = build_lut(self.colormap_combo.currentText(), self.gamma_spin.value())

        timestamps = self.rec.timestamps
        x_start = float(timestamps[self.t0])
        if self.t1 - self.t0 >= 2:
            step = float(np.median(np.diff(timestamps[self.t0 : self.t1])))
        else:
            step = 0.05
        x_width = float(timestamps[self.t1 - 1] - timestamps[self.t0]) + step

        for channel, plot in enumerate(self._plots):
            image = np.ascontiguousarray(crop[channel].T, dtype=np.float32)
            plot.image.setLookupTable(lut)
            plot.image.setImage(image, autoLevels=False, levels=levels[channel])
            plot.image.setRect(QtCore.QRectF(x_start, self.d0, x_width, self.d1 - self.d0))
            plot.setXRange(x_start, x_start + x_width, padding=0.0)
            plot.setYRange(self.d0, self.d1, padding=0.0)

        self.a_plot.setXRange(self.d0, self.d1, padding=0.0)
        self._update_a_mode()
        self.a_mode_info.setText(
            f"帧 {self.frame + 1}/{self.rec.n_frames} | 深度 {self.d0}-{self.d1}"
        )

    def _compute_levels(self, crop: np.ndarray):
        low_p = float(self.low_spin.value())
        high_p = float(self.high_spin.value())

        def robust(values: np.ndarray):
            flat = values.reshape(-1)
            if flat.size > 200000:
                flat = flat[:: flat.size // 200000]
            low, high = np.percentile(flat, [low_p, high_p])
            if not np.isfinite(low) or not np.isfinite(high) or high - low < 1e-6:
                low, high = float(flat.min()), float(flat.max())
            if high - low < 1e-6:
                high = low + 1.0
            return float(low), float(high)

        if self.level_combo.currentText() == "全局":
            low, high = robust(crop)
            return [(low, high)] * crop.shape[0]
        return [robust(crop[channel]) for channel in range(crop.shape[0])]

    def _on_display_changed(self, _value=None):
        self._display_params.set(self._show_processed, self.gamma_spin.value())
        self._save_timer.start()
        if self.rec is None:
            return
        crop = self._active_data()[:, self.t0 : self.t1, self.d0 : self.d1]
        levels = self._compute_levels(crop)
        lut = build_lut(self.colormap_combo.currentText(), self.gamma_spin.value())
        for channel, plot in enumerate(self._plots):
            plot.image.setLookupTable(lut)
            plot.image.setLevels(levels[channel])

    # ------------------------------------------------------------------
    # 预处理
    # ------------------------------------------------------------------
    def _active_data(self) -> np.ndarray:
        """当前用于显示的数据：预处理结果（若可用）或原始数据。"""
        if (
            self._show_processed
            and self._proc_data is not None
            and self._proc_data.shape == self.rec.data.shape
        ):
            return self._proc_data
        return self.rec.data

    def _update_a_mode_range(self):
        data = self._active_data()
        values = data.reshape(-1)
        sample = values[:: max(1, values.size // 200000)]
        low, high = np.percentile(sample, [0.5, 99.5])
        margin = max(1e-6, (float(high) - float(low)) * 0.05)
        self._a_y_range = (float(low) - margin, float(high) + margin)
        self.a_plot.setYRange(
            self._a_y_range[0], self._a_y_range[1], padding=0.0
        )

    def _on_preprocess_toggled(self, checked: bool):
        self._show_processed = checked
        # 两种视图各自使用记住的 gamma；切换时只刷新 LUT，不重算数据
        self.gamma_spin.blockSignals(True)
        self.gamma_spin.setValue(self._display_params.get(checked))
        self.gamma_spin.blockSignals(False)
        self._refresh_gamma()
        if checked and self.rec is not None:
            signature = self._params.signature()
            if self._proc_data is not None and self._proc_signature == signature:
                self._proc_status.setText("预处理：已显示缓存结果")
                self._refresh_display_source()
            else:
                self._proc_status.setText("预处理：计算中…")
                self._start_preprocess()
        else:
            self._proc_status.setText("预处理：关（显示原始）")
            self._refresh_display_source()

    def _refresh_gamma(self):
        if self.rec is None:
            return
        lut = build_lut(self.colormap_combo.currentText(), self.gamma_spin.value())
        for plot in self._plots:
            plot.image.setLookupTable(lut)

    def _refresh_display_source(self):
        if self.rec is None:
            return
        self.a_plot.setLabel(
            "left",
            "预处理幅度"
            if self._show_processed and self._proc_data is not None
            else "ADC 值",
            size="9pt",
        )
        self._update_a_mode_range()
        self._rebuild_images()
        self._set_frame(self.frame)

    def _schedule_preprocess(self):
        if self.rec is None:
            return
        self._compute_timer.start()

    def _worker_running(self) -> bool:
        worker = self._proc_thread
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            self._proc_thread = None
            return False

    def _start_preprocess(self):
        if self.rec is None or not self._show_processed:
            return
        if self._worker_running():
            self._pending_params = self._params.copy()
            return

        params = self._params.copy()
        if not params.has_enabled_steps():
            self._proc_data = None
            self._proc_signature = None
            self._proc_status.setText("预处理：全部关闭（等同原始）")
            self._refresh_display_source()
            return

        error = validate_bandpass(params) if params.bandpass_enabled else None
        if error:
            self._proc_status.setText(f"预处理：参数无效（{error}）")
            return

        self._proc_token += 1
        self._proc_started_signature = params.signature()
        self._proc_status.setText("预处理：计算中…")
        worker = PreprocessWorker(self._proc_token, self.rec.data, params, self)
        worker.resultReady.connect(self._on_preprocess_ready)
        worker.finished.connect(worker.deleteLater)
        self._proc_thread = worker
        worker.start()

    def _on_preprocess_ready(self, token: int, result, elapsed: float):
        self._proc_thread = None
        if token != self._proc_token:
            # 过期结果（期间切换了记录或重复提交），只负责继续处理待办
            if self._pending_params is not None:
                self._pending_params = None
                self._compute_timer.start()
            return

        if isinstance(result, Exception):
            self._proc_status.setText(f"预处理失败：{result}")
        else:
            self._proc_data = result
            self._proc_signature = getattr(
                self, "_proc_started_signature", None
            )
            self._proc_status.setText(f"预处理：已更新（{elapsed:.2f} s）")
            if self._show_processed:
                self._refresh_display_source()

        if self._pending_params is not None:
            self._pending_params = None
            self._compute_timer.start()

    def _on_params_changed(self, params: PreprocessParams):
        self._params = params
        self._save_timer.start()
        if self._show_processed:
            self._schedule_preprocess()

    def _open_preprocess_dialog(self):
        if self._dialog is None:
            self._dialog = PreprocessDialog(self._params, self)
            self._dialog.paramsChanged.connect(self._on_params_changed)
        else:
            self._dialog.set_params(self._params)
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def _on_raw_overlay_toggled(self, _checked: bool):
        self._update_a_mode()

    def _save_params(self):
        save_params(self._params, display=self._display_params)

    # ------------------------------------------------------------------
    # 帧与回放
    # ------------------------------------------------------------------
    def _set_frame(self, index: int):
        if self.rec is None:
            return
        index = int(max(0, min(index, self.rec.n_frames - 1)))
        self.frame = index

        self.slider.blockSignals(True)
        self.slider.setValue(index)
        self.slider.blockSignals(False)

        seconds = float(self.rec.timestamps[index])
        self.frame_label.setText(
            f"{index + 1} / {self.rec.n_frames}   {seconds:.2f} s"
        )
        for plot in self._plots:
            plot.playhead.setPos(seconds)

        self._update_a_mode()

    def _on_channel_visibility_changed(self, channel: int, visible: bool):
        if channel >= len(self._a_curves):
            return
        self._a_curves[channel].setVisible(visible)
        if channel < len(self._a_raw_curves):
            self._a_raw_curves[channel].setVisible(
                visible and self._raw_overlay_active()
            )
        self._refresh_a_legend()

    def _raw_overlay_active(self) -> bool:
        return (
            self._show_processed
            and self._proc_data is not None
            and self.raw_overlay_check.isChecked()
        )

    def _refresh_a_legend(self):
        self._a_legend.clear()
        for channel, curve in enumerate(self._a_curves):
            if self.channel_checks[channel].isChecked():
                self._a_legend.addItem(curve, f"CH{channel + 1}")

    def _update_a_mode(self):
        if self.rec is None:
            return
        x = np.arange(self.d0, self.d1)
        data = self._active_data()
        for channel, curve in enumerate(self._a_curves):
            curve.setData(x, data[channel, self.frame, self.d0 : self.d1])

        overlay = self._raw_overlay_active()
        target_low, target_high = getattr(self, "_a_y_range", (0.0, 1.0))
        for channel, curve in enumerate(self._a_raw_curves):
            visible = overlay and self.channel_checks[channel].isChecked()
            curve.setVisible(visible)
            if visible:
                raw = self.rec.data[channel, self.frame, self.d0 : self.d1].astype(
                    np.float32
                )
                low, high = float(raw.min()), float(raw.max())
                if high - low > 1e-6:
                    normalized = (raw - low) / (high - low)
                else:
                    normalized = np.zeros_like(raw)
                curve.setData(
                    x, target_low + normalized * (target_high - target_low)
                )

        self.a_mode_info.setText(
            f"帧 {self.frame + 1}/{self.rec.n_frames} | 深度 {self.d0}-{self.d1}"
        )

    def _on_slider(self, value: int):
        self._set_frame(value)

    def _on_slider_pressed(self):
        self._was_playing = self._timer.isActive()
        self._timer.stop()

    def _on_slider_released(self):
        if getattr(self, "_was_playing", False):
            self._timer.start(int(1000.0 / max(1.0, self.fps_spin.value())))

    def _on_play_toggled(self, playing: bool):
        if self.rec is None:
            self.play_button.setChecked(False)
            return
        if playing:
            self.play_button.setText("暂停")
            interval = int(1000.0 / max(1.0, self.fps_spin.value()))
            self._timer.start(interval)
        else:
            self.play_button.setText("播放")
            self._timer.stop()

    def _on_fps_changed(self, _value: float):
        if self._timer.isActive():
            self._timer.start(int(1000.0 / max(1.0, self.fps_spin.value())))

    def _on_tick(self):
        if self.rec is None:
            return
        next_frame = self.frame + 1
        if next_frame >= self.t1 or next_frame >= self.rec.n_frames:
            if self.loop_check.isChecked():
                next_frame = self.t0
            else:
                self.play_button.setChecked(False)
                return
        self._set_frame(next_frame)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_seek(self, _channel: int, x_seconds: float):
        if self.rec is None:
            return
        if self._timer.isActive():
            self.play_button.setChecked(False)
        index = int(np.argmin(np.abs(self.rec.timestamps - x_seconds)))
        self._set_frame(index)

    def _on_wheel_zoom(self, _channel: int, factor: float, x_seconds: float):
        if self.rec is None:
            return
        center = int(np.argmin(np.abs(self.rec.timestamps - x_seconds)))
        span = max(2, self.t1 - self.t0)
        new_span = int(round(span * factor))
        new_span = max(10, min(self.rec.n_frames, new_span))
        ratio = new_span / span
        new_t0 = int(round(center - (center - self.t0) * ratio))
        new_t0 = max(0, min(new_t0, self.rec.n_frames - new_span))
        self._set_time_window(new_t0, new_t0 + new_span)

    def _on_hover(self, channel: int, x_seconds: float, depth: float):
        if self.rec is None:
            return
        depth_index = int(round(depth))
        if not (self.d0 <= depth_index < self.d1):
            return
        frame_index = int(np.argmin(np.abs(self.rec.timestamps - x_seconds)))
        value = self._active_data()[channel, frame_index, depth_index]
        number = f"{value:.3f}" if self._show_processed else f"{value:.1f}"
        self._status.setText(
            f"CH{channel + 1} | 深度 {depth_index} | 帧 {frame_index + 1} | 值 {number}"
        )

    def _export_screenshot(self):
        if self.rec is None:
            return
        out_dir = Path(__file__).resolve().parent / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        default_name = f"{self.rec.folder.name}_frame{self.frame + 1:04d}.png"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存截图", str(out_dir / default_name), "PNG 图片 (*.png)"
        )
        if not path:
            return
        self.grab().save(path)
        self.statusBar().showMessage(f"截图已保存：{path}", 5000)

    # ------------------------------------------------------------------
    # 键盘
    # ------------------------------------------------------------------
    def keyPressEvent(self, event):
        focus = QtWidgets.QApplication.focusWidget()
        if isinstance(focus, (QtWidgets.QAbstractSpinBox, QtWidgets.QLineEdit)):
            super().keyPressEvent(event)
            return
        if self.rec is None:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key == Qt.Key_Space:
            self.play_button.setChecked(not self.play_button.isChecked())
        elif key == Qt.Key_Left:
            self._set_frame(self.frame - 1)
        elif key == Qt.Key_Right:
            self._set_frame(self.frame + 1)
        elif key == Qt.Key_Home:
            self._set_frame(self.t0)
        elif key == Qt.Key_End:
            self._set_frame(self.t1 - 1)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self._timer.stop()
        self._compute_timer.stop()
        self._save_timer.stop()
        save_params(self._params, display=self._display_params)
        if self._worker_running():
            try:
                self._proc_thread.wait(3000)
            except RuntimeError:
                pass
        super().closeEvent(event)


def _enable_windows_dpi_awareness() -> None:
    """让进程具备 Per-Monitor DPI 感知，避免 Windows 对窗口做位图拉伸。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def _configure_high_dpi() -> None:
    """在创建 QApplication 之前完成高 DPI 配置。

    - 使用 PassThrough 缩放取整策略：Windows 的 125%/150%/175% 缩放不再被
      取整到 100%/200%，避免 4K 屏上界面整体偏大或随分辨率跳变。
    - 可用环境变量 ``AUS_UI_SCALE``（如 0.9）整体微调界面比例。
    """
    scale = os.environ.get("AUS_UI_SCALE", "").strip()
    if scale:
        try:
            float(scale)
            os.environ["QT_SCALE_FACTOR"] = scale
        except ValueError:
            pass

    _enable_windows_dpi_awareness()

    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    try:
        QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except (AttributeError, TypeError):
        pass


def main():
    _configure_high_dpi()
    pg.setConfigOptions(imageAxisOrder="row-major", antialias=True, background="w")

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Microsoft YaHei", 9))

    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    window = ViewerWindow(folder)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
