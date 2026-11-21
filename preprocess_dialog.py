"""预处理参数窗口。"""

from __future__ import annotations

from PyQt5 import QtWidgets
from PyQt5.QtCore import pyqtSignal

from preprocess import (
    DEPTH_PER_SAMPLE_CM,
    PreprocessParams,
    validate_bandpass,
)


class PreprocessDialog(QtWidgets.QDialog):
    """独立的预处理参数窗口，修改即生效（由主窗口防抖后重算）。"""

    paramsChanged = pyqtSignal(object)

    def __init__(self, params: PreprocessParams, parent=None):
        super().__init__(parent)
        self.setWindowTitle("预处理参数")
        self.setModal(False)
        self.setMinimumWidth(460)
        self._loading = True

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        title = QtWidgets.QLabel("处理顺序固定：带通 → TGC → 包络 → 对数压缩 → 标准化 → 归一化")
        title.setStyleSheet("color:#555;")
        title.setWordWrap(True)
        layout.addWidget(title)

        # --- 带通滤波 ---
        self.bandpass_group = QtWidgets.QGroupBox("带通滤波")
        self.bandpass_group.setCheckable(True)
        bandpass_form = QtWidgets.QFormLayout(self.bandpass_group)
        self.low_spin = self._make_double_spin(0.1, 9.9, 0.1, 2, " MHz")
        self.high_spin = self._make_double_spin(0.2, 10.0, 0.1, 2, " MHz")
        self.order_spin = QtWidgets.QSpinBox()
        self.order_spin.setRange(1, 12)
        self.order_spin.setFixedWidth(130)
        bandpass_form.addRow("低截止频率：", self.low_spin)
        bandpass_form.addRow("高截止频率：", self.high_spin)
        bandpass_form.addRow("滤波器阶数：", self.order_spin)
        self.bandpass_error = QtWidgets.QLabel("")
        self.bandpass_error.setStyleSheet("color:#b00020;")
        self.bandpass_error.setWordWrap(True)
        self.bandpass_error.setVisible(False)
        bandpass_form.addRow(self.bandpass_error)
        layout.addWidget(self.bandpass_group)

        # --- TGC ---
        self.tgc_group = QtWidgets.QGroupBox("TGC（时间增益补偿）")
        self.tgc_group.setCheckable(True)
        tgc_form = QtWidgets.QFormLayout(self.tgc_group)
        self.tgc_spin = self._make_double_spin(0.0, 2.0, 0.05, 2, " /cm")
        tgc_form.addRow("增益强度：", self.tgc_spin)
        tgc_hint = QtWidgets.QLabel(
            f"g(d) = exp(强度 × 深度cm)；采样率 20 MHz、声速 1540 m/s，"
            f"单点 ≈ {DEPTH_PER_SAMPLE_CM:.5f} cm（1000 点 ≈ {DEPTH_PER_SAMPLE_CM * 1000:.2f} cm）"
        )
        tgc_hint.setStyleSheet("color:#666; font-size:9pt;")
        tgc_hint.setWordWrap(True)
        tgc_form.addRow(tgc_hint)
        layout.addWidget(self.tgc_group)

        # --- 包络 ---
        self.envelope_group = QtWidgets.QGroupBox("希尔伯特包络")
        self.envelope_group.setCheckable(True)
        envelope_layout = QtWidgets.QVBoxLayout(self.envelope_group)
        envelope_hint = QtWidgets.QLabel("对信号取解析信号的模长，得到随深度变化的回波包络。")
        envelope_hint.setStyleSheet("color:#666; font-size:9pt;")
        envelope_hint.setWordWrap(True)
        envelope_layout.addWidget(envelope_hint)
        layout.addWidget(self.envelope_group)

        # --- 对数压缩 ---
        self.log_group = QtWidgets.QGroupBox("对数压缩（可选）")
        self.log_group.setCheckable(True)
        log_form = QtWidgets.QFormLayout(self.log_group)
        self.log_k_spin = self._make_double_spin(0.001, 10.0, 0.01, 3, "")
        log_form.addRow("压缩系数 k：", self.log_k_spin)
        log_hint = QtWidgets.QLabel("log10(1 + k × 包络)，压缩动态范围，使深层弱回波更可见。")
        log_hint.setStyleSheet("color:#666; font-size:9pt;")
        log_hint.setWordWrap(True)
        log_form.addRow(log_hint)
        layout.addWidget(self.log_group)

        # --- 标准化 ---
        self.standardize_group = QtWidgets.QGroupBox("标准化")
        self.standardize_group.setCheckable(True)
        standardize_layout = QtWidgets.QVBoxLayout(self.standardize_group)
        standardize_hint = QtWidgets.QLabel("逐帧对四通道整体做 (x-均值)/标准差，保留通道间幅值差异。")
        standardize_hint.setStyleSheet("color:#666; font-size:9pt;")
        standardize_hint.setWordWrap(True)
        standardize_layout.addWidget(standardize_hint)
        layout.addWidget(self.standardize_group)

        # --- 归一化 ---
        self.normalize_group = QtWidgets.QGroupBox("归一化 0–1（可选）")
        self.normalize_group.setCheckable(True)
        normalize_layout = QtWidgets.QVBoxLayout(self.normalize_group)
        normalize_hint = QtWidgets.QLabel("逐帧把四通道整体缩放到 [0, 1]，便于跨记录比较亮度。")
        normalize_hint.setStyleSheet("color:#666; font-size:9pt;")
        normalize_hint.setWordWrap(True)
        normalize_layout.addWidget(normalize_hint)
        layout.addWidget(self.normalize_group)

        layout.addStretch(1)

        buttons = QtWidgets.QHBoxLayout()
        self.reset_button = QtWidgets.QPushButton("恢复默认")
        self.reset_button.clicked.connect(self._restore_defaults)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        self.close_button = QtWidgets.QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self._groups = [
            (self.bandpass_group, [self.low_spin, self.high_spin, self.order_spin]),
            (self.tgc_group, [self.tgc_spin]),
            (self.envelope_group, []),
            (self.log_group, [self.log_k_spin]),
            (self.standardize_group, []),
            (self.normalize_group, []),
        ]

        for group, _widgets in self._groups:
            group.toggled.connect(self._on_group_toggled)
        for widget in (
            self.low_spin,
            self.high_spin,
            self.order_spin,
            self.tgc_spin,
            self.log_k_spin,
        ):
            widget.valueChanged.connect(self._collect)

        self.set_params(params)
        self._loading = False

    # ------------------------------------------------------------------
    def _make_double_spin(self, low, high, step, decimals, suffix) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        if suffix:
            spin.setSuffix(suffix)
        spin.setFixedWidth(130)
        return spin

    def set_params(self, params: PreprocessParams):
        """从外部同步参数到控件（不触发计算）。"""
        self._loading = True
        try:
            self.bandpass_group.setChecked(params.bandpass_enabled)
            self.low_spin.setValue(params.low_cut_mhz)
            self.high_spin.setValue(params.high_cut_mhz)
            self.order_spin.setValue(params.order)
            self.tgc_group.setChecked(params.tgc_enabled)
            self.tgc_spin.setValue(params.tgc_strength)
            self.envelope_group.setChecked(params.envelope_enabled)
            self.log_group.setChecked(params.log_enabled)
            self.log_k_spin.setValue(params.log_k)
            self.standardize_group.setChecked(params.standardize_enabled)
            self.normalize_group.setChecked(params.normalize_enabled)
            self._update_enabled_states()
            self.bandpass_error.setVisible(False)
            self._clear_error_style()
        finally:
            self._loading = False

    def _update_enabled_states(self):
        for group, widgets in self._groups:
            enabled = group.isChecked()
            for widget in widgets:
                widget.setEnabled(enabled)

    def _on_group_toggled(self, _checked: bool):
        self._update_enabled_states()
        self._collect()

    def _collect(self):
        if self._loading:
            return
        params = PreprocessParams(
            bandpass_enabled=self.bandpass_group.isChecked(),
            low_cut_mhz=self.low_spin.value(),
            high_cut_mhz=self.high_spin.value(),
            order=self.order_spin.value(),
            tgc_enabled=self.tgc_group.isChecked(),
            tgc_strength=self.tgc_spin.value(),
            envelope_enabled=self.envelope_group.isChecked(),
            log_enabled=self.log_group.isChecked(),
            log_k=self.log_k_spin.value(),
            standardize_enabled=self.standardize_group.isChecked(),
            normalize_enabled=self.normalize_group.isChecked(),
        )

        error = validate_bandpass(params) if params.bandpass_enabled else None
        if error:
            self.bandpass_error.setText(error)
            self.bandpass_error.setVisible(True)
            self.low_spin.setStyleSheet("color:#b00020;")
            self.high_spin.setStyleSheet("color:#b00020;")
            return

        self.bandpass_error.setVisible(False)
        self._clear_error_style()
        self.paramsChanged.emit(params)

    def _clear_error_style(self):
        self.low_spin.setStyleSheet("")
        self.high_spin.setStyleSheet("")

    def _restore_defaults(self):
        defaults = PreprocessParams()
        self.set_params(defaults)
        self.paramsChanged.emit(defaults)
