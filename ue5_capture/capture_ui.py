# -*- coding: utf-8 -*-
"""
capture_ui.py  --  UE5.7 Scene Capture GUI (tkinter)

Python 標準の tkinter でウィンドウを描画する。tkinter の mainloop は UE の
メインスレッドをブロックするため使わず、register_slate_post_tick_callback で
毎フレーム root.update() を呼ぶ「非ブロッキング統合」にする。

Tk ルートはプロセスで1つだけ作り、絶対に destroy しない（UE 埋め込み Python では
root.destroy() 後の tk.Tk() 再生成が Tcl panic となりエディタごと落ちる）。
閉じる=withdraw / 再表示=deiconify＋UI 再構築。ルートと tick ハンドルは
reload をまたいで残るよう unreal モジュール上に保持する。

tkinter が利用できない環境（UE 同梱 Python に tcl/tk が無い等）では
ImportError を送出するので、呼び出し側（capture_tool.py）が CONFIG/CUI に
フォールバックする。
"""

import os
import json
import importlib

import unreal

import capture_core as core

# tkinter は import 時点では失敗させない（呼び出し側で判定させる）
try:
    import tkinter as tk
    from tkinter import ttk, filedialog
    _HAS_TK = True
except Exception:
    tk = None
    ttk = None
    filedialog = None
    _HAS_TK = False


# 映像タブの出力素材: (キー, UI ラベル, ファイル素材名)
_SEQ_OUTPUTS = [
    ("beauty", "Beauty", "Beauty"),
    ("depth", "Z-Depth", "Depth"),
    ("mfront", "Beauty+Matte（Matteの前）", "MatteBeauty"),
    ("behind", "Matteの奥", "Behind"),
    ("objid", "ObjectID", "ObjectID"),
]

# MP4 レートプリセット（H.264 の CRF。小さいほど高品質・大容量）
_MP4_RATE_PRESETS = {
    "最高 (CRF 17)": 17,
    "高 (CRF 20)": 20,
    "標準 (CRF 24)": 24,
    "軽量 (CRF 28)": 28,
}


class CaptureWindow(object):
    def __init__(self):
        if not _HAS_TK:
            raise ImportError("tkinter が利用できません。")

        self._cameras = core.list_cameras()

        self.root = _persistent_root()
        for child in self.root.winfo_children():
            child.destroy()      # 子ウィジェットの破棄は安全（ルートだけは破棄禁止）
        self.root.title("Scene Capture Tool (UE5.7) ★Beauty版★")
        self.root.geometry("540x1040")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self.root.deiconify()
        self._register_tick()

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        outer = ttk.Frame(self.root, padding=(6, 6, 6, 4))
        outer.pack(fill="both", expand=True)
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        tab_img = ttk.Frame(nb, padding=6)
        tab_seq = ttk.Frame(nb, padding=6)
        nb.add(tab_img, text="画像キャプチャ")
        nb.add(tab_seq, text="映像キャプチャ")
        # ステータスはタブ共通で最下部に表示
        self.status_var = tk.StringVar(master=self.root, value="")
        ttk.Label(outer, textvariable=self.status_var, foreground="#0a7").pack(
            anchor="w", padx=8, pady=(2, 0))
        try:
            ttk.Style().configure("Big.TButton", font=("", 14, "bold"), padding=12)
        except Exception:
            pass
        self._build_image_tab(tab_img, pad)
        self._build_seq_tab(tab_seq, pad)
        # 前回の入力を復元
        self._load_ui_state()
        self._update_cam_res()
        self._refresh_sequence()

    def _build_image_tab(self, frm, pad):
        """従来の単発キャプチャ UI（レイアウトは従来のまま）。"""
        row = 0

        # Camera（Refresh で現在のレベルのカメラに更新）
        ttk.Label(frm, text="Camera:").grid(row=row, column=0, sticky="w", **pad)
        self.cam_var = tk.StringVar(master=self.root)
        cam_labels = [c.get_actor_label() for c in self._cameras] or ["(no camera)"]
        self.cam_combo = ttk.Combobox(frm, textvariable=self.cam_var,
                                      values=cam_labels, state="readonly", width=28)
        self.cam_combo.current(0)
        self.cam_combo.grid(row=row, column=1, sticky="we", **pad)
        ttk.Button(frm, text="⟳", width=3, command=self._refresh_cameras).grid(
            row=row, column=2, sticky="w")
        self.cam_combo.bind("<<ComboboxSelected>>", lambda e: self._on_camera_change())
        row += 1

        # Resolution
        ttk.Label(frm, text="Resolution:").grid(row=row, column=0, sticky="nw", **pad)
        camrow = ttk.Frame(frm)
        self.res_mode = tk.StringVar(master=self.root, value="camera")
        ttk.Radiobutton(camrow, text="Use Camera Setting", variable=self.res_mode,
                        value="camera").pack(side="left")
        self.cam_res_var = tk.StringVar(master=self.root, value="")
        ttk.Label(camrow, textvariable=self.cam_res_var, foreground="#0a7").pack(side="left", padx=(8, 0))
        camrow.grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1
        ovr = ttk.Frame(frm)
        ttk.Radiobutton(ovr, text="Override:", variable=self.res_mode,
                        value="override").pack(side="left")
        self.w_var = tk.StringVar(master=self.root, value="3840")
        self.h_var = tk.StringVar(master=self.root, value="2160")
        tk.Entry(ovr, textvariable=self.w_var, width=6).pack(side="left", padx=2)
        ttk.Label(ovr, text="x").pack(side="left")
        self.h_entry = tk.Entry(ovr, textvariable=self.h_var, width=6)
        self.h_entry.pack(side="left", padx=2)
        self.aspect_lock_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(ovr, text="アスペクト維持(幅⇄高さ自動)", variable=self.aspect_lock_var,
                        command=self._on_width_change).pack(side="left", padx=(8, 0))
        ovr.grid(row=row, column=1, columnspan=2, sticky="w", padx=8)
        row += 1
        self._aspect_guard = False  # W↔H 相互更新のループ防止
        self.w_var.trace_add("write", lambda *a: self._on_width_change())
        self.h_var.trace_add("write", lambda *a: self._on_height_change())
        # Override に入る直前の解像度を覚えて、Camera Setting に戻したら復元する
        self._prev_res_mode = self.res_mode.get()
        self._saved_cam_wh = None        # Camera Setting 用に退避した解像度
        self._saved_override_wh = None   # Override 入力値（カメラ切替まで維持）
        self.res_mode.trace_add("write", lambda *a: self._on_res_mode_change())

        # Overscan（Override の下。ON のとき % か 直接ピクセルで余白を追加。元フレームは中央維持・全パス共通）
        ttk.Label(frm, text="Overscan:").grid(row=row, column=0, sticky="w", **pad)
        osf = ttk.Frame(frm)
        self.overscan_on_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(osf, text="ON", variable=self.overscan_on_var).pack(side="left")
        self.overscan_mode_var = tk.StringVar(master=self.root, value="percent")
        ttk.Radiobutton(osf, text="%", variable=self.overscan_mode_var,
                        value="percent").pack(side="left", padx=(8, 0))
        self.overscan_var = tk.StringVar(master=self.root, value="0")
        tk.Entry(osf, textvariable=self.overscan_var, width=5).pack(side="left", padx=(2, 8))
        ttk.Radiobutton(osf, text="px", variable=self.overscan_mode_var,
                        value="pixels").pack(side="left")
        ttk.Label(osf, text="X").pack(side="left", padx=(4, 1))
        self.overscan_x_var = tk.StringVar(master=self.root, value="0")
        tk.Entry(osf, textvariable=self.overscan_x_var, width=5).pack(side="left")
        ttk.Label(osf, text="Y").pack(side="left", padx=(4, 1))
        self.overscan_y_var = tk.StringVar(master=self.root, value="0")
        tk.Entry(osf, textvariable=self.overscan_y_var, width=5).pack(side="left")
        osf.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        row += 1

        # anti-aliasing（旧 AA）
        ttk.Label(frm, text="anti-aliasing:").grid(row=row, column=0, sticky="w", **pad)
        self.aa_var = tk.StringVar(master=self.root, value="2x")
        ttk.Combobox(frm, textvariable=self.aa_var, values=["1x", "2x", "4x"],
                     state="readonly", width=8).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        # Output dir
        ttk.Label(frm, text="Output Dir:").grid(row=row, column=0, sticky="w", **pad)
        default_dir = os.path.normpath(
            os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
        self.out_var = tk.StringVar(master=self.root, value=default_dir)
        tk.Entry(frm, textvariable=self.out_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        ttk.Button(frm, text="...", width=3, command=self._browse).grid(
            row=row, column=2, sticky="w")
        row += 1

        # ファイル名: [任意名]_[カメラ名]_素材名_001（任意名/カメラ名は下のチェックで含める）
        self.name_usecustom_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="任意名を付ける:", variable=self.name_usecustom_var).grid(
            row=row, column=0, sticky="w", **pad)
        self.name_custom_var = tk.StringVar(master=self.root, value="")
        tk.Entry(frm, textvariable=self.name_custom_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        row += 1
        self.name_usecam_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(frm, text="カメラ名を付ける", variable=self.name_usecam_var).grid(
            row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        ttk.Label(frm, text="  ファイル名: [任意名]_[カメラ名]_素材名_NNN",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=8)
        row += 1
        ttk.Label(frm, text="出力:").grid(
            row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1

        # Beauty（MRQ = ビューポート露出＋シーケンサ品質）
        self.beauty_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(frm, text="Beauty（MRQ = ビューポート露出＋シーケンサ品質）",
                        variable=self.beauty_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        mrqf = ttk.Frame(frm)
        ttk.Label(mrqf, text="ウォームアップ:").pack(side="left")
        self.mrq_warmup_var = tk.StringVar(master=self.root, value="32")
        tk.Entry(mrqf, textvariable=self.mrq_warmup_var, width=5).pack(side="left", padx=2)
        ttk.Label(mrqf, text="サンプリングフレーム:").pack(side="left", padx=(8, 0))
        self.mrq_ts_var = tk.StringVar(master=self.root, value="8")
        tk.Entry(mrqf, textvariable=self.mrq_ts_var, width=5).pack(side="left", padx=2)
        mrqf.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        mrqf2 = ttk.Frame(frm)
        self.mrq_exr_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(mrqf2, text="EXR", variable=self.mrq_exr_var).pack(side="left")
        self.mrq_camasp_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(mrqf2, text="カメラのアスペクト",
                        variable=self.mrq_camasp_var).pack(side="left", padx=(8, 0))
        self.fog_off_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(mrqf2, text="Fogなし", variable=self.fog_off_var).pack(
            side="left", padx=(8, 0))
        mrqf2.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        # Z-Depth（手前=白/奥=黒 固定）
        self.depth_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="Z-Depth（手前=白 / 奥=黒）", variable=self.depth_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        depth_frm = ttk.Frame(frm)
        ttk.Label(depth_frm, text="Format:").pack(side="left")
        self.depth_bit_var = tk.StringVar(master=self.root, value="16bit PNG")
        ttk.Combobox(depth_frm, textvariable=self.depth_bit_var,
                     values=["8bit PNG", "16bit PNG", "EXR float"], state="readonly",
                     width=11).pack(side="left", padx=4)
        ttk.Label(depth_frm, text="Near:").pack(side="left")
        self.near_var = tk.StringVar(master=self.root, value="0")
        tk.Entry(depth_frm, textvariable=self.near_var, width=6).pack(side="left", padx=2)
        ttk.Label(depth_frm, text="cm").pack(side="left")
        ttk.Label(depth_frm, text="Far:").pack(side="left", padx=(6, 0))
        self.far_var = tk.StringVar(master=self.root, value="10000")
        tk.Entry(depth_frm, textvariable=self.far_var, width=7).pack(side="left", padx=2)
        ttk.Label(depth_frm, text="cm（=Unreal世界単位。1m=100cm）").pack(side="left")
        depth_frm.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        # Matte 系（Beauty+Matte / Matteの奥。対象は Matte targets）
        self.mfront_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="Beauty+Matte（Matteの前）",
                        variable=self.mfront_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.behind_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="Matteの奥",
                        variable=self.behind_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        ttk.Label(frm, text="  （対象は下の Matte targets、空ならエディタ選択。出力時は Beauty から対象を自動で隠す）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.matte_pick, row = self._make_picker(frm, row, "Matte targets")

        # ObjectID（対象を色分け・他は黒）
        self.objid_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="ObjectID（対象を色分け・他は黒 + 色↔名前 JSON）",
                        variable=self.objid_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.objid_pick, row = self._make_picker(frm, row, "Object ID targets")

        self.capture_btn = ttk.Button(
            frm, text="Capture", style="Big.TButton", command=self._on_mrq)
        self.capture_btn.grid(row=row, column=0, columnspan=3,
                              pady=14, padx=24, ipady=6, sticky="we")

        frm.columnconfigure(1, weight=1)

    def _build_seq_tab(self, frm, pad):
        """映像キャプチャ（シーケンスレンダ）タブ。設定は画像タブから独立していて、
        「設定を転送」ボタンで画像タブの値を一括コピーできる。"""
        row = 0
        ttk.Button(frm, text="← 画像キャプチャの設定を転送 (解像度/出力先/任意名/品質/Depth/Fog)",
                   command=self._transfer_from_image_tab).grid(
            row=row, column=0, columnspan=3, sticky="we", padx=8, pady=(6, 10))
        row += 1

        seqrow = ttk.Frame(frm)
        ttk.Label(seqrow, text="Sequence:").pack(side="left")
        self.seq_name_var = tk.StringVar(master=self.root, value="(未取得)")
        ttk.Label(seqrow, textvariable=self.seq_name_var, foreground="#0a7").pack(
            side="left", padx=(4, 0))
        ttk.Button(seqrow, text="⟳", width=3, command=self._refresh_sequence).pack(
            side="left", padx=(6, 0))
        seqrow.grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        ttk.Label(frm, text="（Sequencer で開いているシーケンスをカメラカットでレンダ。"
                            "fps はシーケンスの Display Rate）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        rng = ttk.Frame(frm)
        ttk.Label(rng, text="Range:").pack(side="left")
        self.seq_range_mode = tk.StringVar(master=self.root, value="auto")
        ttk.Radiobutton(rng, text="シーケンス範囲", variable=self.seq_range_mode,
                        value="auto").pack(side="left", padx=(4, 0))
        ttk.Radiobutton(rng, text="指定:", variable=self.seq_range_mode,
                        value="custom").pack(side="left", padx=(8, 0))
        self.seq_start_var = tk.StringVar(master=self.root, value="0")
        tk.Entry(rng, textvariable=self.seq_start_var, width=6).pack(side="left", padx=2)
        ttk.Label(rng, text="〜").pack(side="left")
        self.seq_end_var = tk.StringVar(master=self.root, value="0")
        tk.Entry(rng, textvariable=self.seq_end_var, width=6).pack(side="left", padx=2)
        ttk.Label(rng, text="(End含む)").pack(side="left")
        rng.grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1

        res = ttk.Frame(frm)
        ttk.Label(res, text="Resolution:").pack(side="left")
        self.seq_w_var = tk.StringVar(master=self.root, value="1920")
        tk.Entry(res, textvariable=self.seq_w_var, width=6).pack(side="left", padx=2)
        ttk.Label(res, text="x").pack(side="left")
        self.seq_h_var = tk.StringVar(master=self.root, value="1080")
        tk.Entry(res, textvariable=self.seq_h_var, width=6).pack(side="left", padx=2)
        ttk.Label(res, text="ウォームアップ:").pack(side="left", padx=(12, 0))
        self.seq_warm_var = tk.StringVar(master=self.root, value="32")
        tk.Entry(res, textvariable=self.seq_warm_var, width=5).pack(side="left", padx=2)
        ttk.Label(res, text="サンプリングフレーム:").pack(side="left", padx=(8, 0))
        self.seq_ts_var = tk.StringVar(master=self.root, value="8")
        tk.Entry(res, textvariable=self.seq_ts_var, width=5).pack(side="left", padx=2)
        res.grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        self.seq_fog_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="Fogなし", variable=self.seq_fog_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=8)
        row += 1

        ttk.Label(frm, text="出力（素材ごとに PNG連番 / MP4 を選択。MP4 はシーケンスの fps で ffmpeg エンコード）:").grid(
            row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        mtx = ttk.Frame(frm)
        ttk.Label(mtx, text="PNG連番").grid(row=0, column=1, padx=8)
        ttk.Label(mtx, text="MP4").grid(row=0, column=2, padx=8)
        self.seq_out_vars = {}
        for i, (key, label, _pass) in enumerate(_SEQ_OUTPUTS):
            ttk.Label(mtx, text=label).grid(row=i + 1, column=0, sticky="w", pady=1)
            pv = tk.BooleanVar(master=self.root, value=(key == "beauty"))
            mv = tk.BooleanVar(master=self.root, value=(key == "beauty"))
            ttk.Checkbutton(mtx, variable=pv).grid(row=i + 1, column=1)
            ttk.Checkbutton(mtx, variable=mv).grid(row=i + 1, column=2)
            self.seq_out_vars[key] = (pv, mv)
        mtx.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        rate = ttk.Frame(frm)
        ttk.Label(rate, text="レート:").pack(side="left")
        self.seq_rate_var = tk.StringVar(master=self.root, value="高 (CRF 20)")
        ttk.Combobox(rate, textvariable=self.seq_rate_var, state="normal", width=12,
                     values=list(_MP4_RATE_PRESETS.keys())).pack(side="left", padx=2)
        ttk.Label(rate, text="(CRF 16-51 直接入力可)", foreground="#888").pack(
            side="left", padx=(2, 0))
        rate.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        dep = ttk.Frame(frm)
        ttk.Label(dep, text="Z-Depth 設定:  Near:").pack(side="left")
        self.seq_near_var = tk.StringVar(master=self.root, value="0")
        tk.Entry(dep, textvariable=self.seq_near_var, width=6).pack(side="left", padx=2)
        ttk.Label(dep, text="Far:").pack(side="left", padx=(6, 0))
        self.seq_far_var = tk.StringVar(master=self.root, value="10000")
        tk.Entry(dep, textvariable=self.seq_far_var, width=7).pack(side="left", padx=2)
        ttk.Label(dep, text="cm（手前=白 / 奥=黒）").pack(side="left")
        dep.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        self.seq_matte_hide_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="Matte 対象を隠す（クリーンプレートのみ。Matte系出力時は自動で隠れる）",
                        variable=self.seq_matte_hide_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        ttk.Label(frm, text="（Matte系の対象=画像タブの Matte targets / ObjectID の対象=画像タブの Object ID targets。"
                            "空ならエディタ選択。Matteの奥は2本目レンダ＋合成、ObjectID は色↔名前の JSON 付き）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=8)
        row += 1

        ttk.Label(frm, text="Output Dir:").grid(row=row, column=0, sticky="w", **pad)
        default_dir = os.path.normpath(
            os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
        self.seq_out_var = tk.StringVar(master=self.root, value=default_dir)
        tk.Entry(frm, textvariable=self.seq_out_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        ttk.Button(frm, text="...", width=3,
                   command=lambda: self._browse(self.seq_out_var)).grid(
            row=row, column=2, sticky="w")
        row += 1
        self.seq_usecustom_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(frm, text="任意名を付ける:", variable=self.seq_usecustom_var).grid(
            row=row, column=0, sticky="w", **pad)
        self.seq_custom_var = tk.StringVar(master=self.root, value="")
        tk.Entry(frm, textvariable=self.seq_custom_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        row += 1
        self.seq_subdir_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(frm, text="テイク毎サブフォルダに出力 (OFF で指定フォルダへ直接)",
                        variable=self.seq_subdir_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        ttk.Label(frm, text="  ファイル名: [任意名]_[シーケンス名]_素材名_NNN.フレーム番号",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        self.seq_btn = ttk.Button(frm, text="Sequence Render", style="Big.TButton",
                                  command=self._on_seq_render)
        self.seq_btn.grid(row=row, column=0, columnspan=3,
                          pady=14, padx=24, ipady=6, sticky="we")
        frm.columnconfigure(1, weight=1)

    def _transfer_from_image_tab(self):
        """画像キャプチャタブの設定（解像度/出力先/任意名/品質/Depth/Fog）を映像タブへコピー。"""
        W = self._int_var(self.w_var, 1920)
        if self.mrq_camasp_var.get():
            asp = self._aspect_ratio()
            H = int(round(W / asp)) if asp > 0.1 else self._int_var(self.h_var, 1080)
        else:
            H = self._int_var(self.h_var, 1080)
        self.seq_w_var.set(str(W))
        self.seq_h_var.set(str(H))
        self.seq_out_var.set(self.out_var.get())
        self.seq_usecustom_var.set(self.name_usecustom_var.get())
        self.seq_custom_var.set(self.name_custom_var.get())
        self.seq_warm_var.set(self.mrq_warmup_var.get())
        self.seq_ts_var.set(self.mrq_ts_var.get())
        self.seq_near_var.set(self.near_var.get())
        self.seq_far_var.set(self.far_var.get())
        self.seq_fog_var.set(self.fog_off_var.get())
        self.status_var.set("画像キャプチャの設定を映像タブへ転送しました")

    # ------------------------------------------------------------- handlers
    @staticmethod
    def _int_var(var, default):
        try:
            return int(var.get())
        except ValueError:
            return default

    @staticmethod
    def _float_var(var, default):
        try:
            return float(var.get())
        except ValueError:
            return default

    def _resolve_crf(self):
        """レート欄からプリセット名 or 直接入力の CRF 数値を解決する（16-51 に clamp）。"""
        txt = (self.seq_rate_var.get() or "").strip()
        if txt in _MP4_RATE_PRESETS:
            return _MP4_RATE_PRESETS[txt]
        try:
            return max(16, min(51, int(float(txt))))
        except ValueError:
            return 20

    def _browse(self, var=None):
        var = var if var is not None else self.out_var
        d = filedialog.askdirectory(initialdir=var.get() or "/")
        if d:
            var.set(os.path.normpath(d))

    def _on_mrq(self):
        """Movie Render Queue で Beauty を高品質レンダ（非同期・PIE）。"""
        import capture_mrq
        importlib.reload(capture_mrq)
        cam = self._current_camera()
        if cam is None:
            self.status_var.set("MRQ: カメラが選択されていません")
            return
        out = self.out_var.get().strip()
        if not out:
            self.status_var.set("MRQ: 出力先を指定してください")
            return
        if not os.path.isdir(out):
            try:
                os.makedirs(out)
            except Exception:
                pass
        W = self._int_var(self.w_var, 1920)
        if self.mrq_camasp_var.get():
            asp = core.get_camera_settings(cam).get("aspect_ratio", 0.0)
            H = int(round(W / asp)) if asp > 0.1 else 1080
        else:
            H = self._int_var(self.h_var, 1080)
        # Overscan: ON のとき fx(横)/fy(縦) を決める。% は一律、px は X/Y 別。
        # カメラの filmback を一時拡大して FOV を縦横独立に広げ、解像度も ×(1+f) に拡大。
        fx = fy = 0.0
        if self.overscan_on_var.get():
            if self.overscan_mode_var.get() == "pixels":
                fx = (max(0.0, self._float_var(self.overscan_x_var, 0.0)) / W) if W > 0 else 0.0
                fy = (max(0.0, self._float_var(self.overscan_y_var, 0.0)) / H) if H > 0 else 0.0
            else:
                fx = fy = max(0.0, self._float_var(self.overscan_var, 0.0) / 100.0)
        if fx > 0.0 or fy > 0.0:
            W = int(round(W * (1.0 + fx)))
            H = int(round(H * (1.0 + fy)))
        warm = self._int_var(self.mrq_warmup_var, 32)
        ts = self._int_var(self.mrq_ts_var, 8)
        self._save_ui_state()
        # Overscan: filmback を一時拡大（全パスのレンダ前。MRQ完了後/失敗時に復元）。
        _osc_fb = None
        if fx > 0.0 or fy > 0.0:
            try:
                _osc_fb = core.set_camera_overscan_filmback(cam, fx, fy)
            except Exception as e:
                self.status_var.set("Overscan filmback 設定失敗: %s" % e)

        def _restore_fb():
            if _osc_fb is not None:
                try:
                    core.restore_camera_filmback(cam, _osc_fb[0], _osc_fb[1])
                except Exception:
                    pass
        # この Capture の通し番号（全出力に _NNN を付与。設定違いを上書きしない）
        suf = "%03d" % core.next_take_number(out)
        # ① データパス(内部 Matte マスク / ObjectID / Depth)を同フレーム・同解像度で先に出す。
        #    Beauty+Matte の合成は MRQ Beauty 完了後に行う。
        s = self._collect_settings()
        s.camera_actor = cam
        s.use_camera_resolution = False
        s.override_width, s.override_height = W, H
        s.do_behind_matte = False           # Matteの奥は下の MRQ near-clip ジョブで行う
        s.take_suffix = suf                 # SceneCapture 系の出力にも同じ通し番号

        def _name(pass_type):
            """MRQ 出力名（SceneCapture 側と同じ 任意名_カメラ名_素材名_NNN 規則）。"""
            return core.out_basename(s, pass_type, suf)

        matte_path = objid_path = None
        want_mfront = self.mfront_var.get()
        want_behind = self.behind_var.get()
        try:
            if s.do_matte or s.do_object_id or s.do_depth:
                self.status_var.set("同フレームの Matte/ObjectID/Depth を出力中…")
                self.root.update()
                outs = core.run_capture(s)
                for o in outs:
                    if o.endswith("_Matte_%s.png" % suf):
                        matte_path = o
                    elif o.endswith("_ObjectID_%s.png" % suf):
                        objid_path = o
        except Exception as e:
            self.status_var.set("データパス出力でエラー: %s" % e)

        beauty_path = os.path.join(out, _name("Beauty") + ".png")
        exr = self.mrq_exr_var.get()

        # Beauty（MRQ）は Beauty 指定時か Matte 系合成が要るときだけレンダする
        beauty_needed = self.beauty_var.get() or want_mfront or want_behind
        if not beauty_needed:
            _restore_fb()
            self.status_var.set("完了（データパスのみ出力）" if (s.do_depth or s.do_object_id)
                                else "出力が選ばれていません")
            return

        # Matte 系出力時は Beauty から対象を常に隠す（クリーンプレート）。
        beauty_hidden = None
        if want_mfront or want_behind:
            matte_names = self._pick_targets_resolved(self.matte_pick)
            beauty_hidden = core._resolve_target_actors(None, matte_names or None)
            if beauty_hidden:
                self.status_var.set("Beauty: Matte 対象 %d 個を隠して撮影（クリーンプレート）" % len(beauty_hidden))
            else:
                self.status_var.set("Matte 対象が見つかりません（Beauty は全表示で撮ります）")

        # 後続 MRQ ジョブのキュー（Matteの奥のプレート）
        jobs = []
        if want_behind:
            mt = core._resolve_target_actors(None, self._pick_targets_resolved(self.matte_pick) or None)
            if mt:
                nc = core.matte_near_clip_cm(mt, core.get_camera_settings(cam))
                jobs.append(dict(hidden=mt, base=_name("BehindPlate"),
                                 near_clip=nc, composite=True, matte=mt))
            else:
                self.status_var.set("Matteの奥: Matte 対象が見つかりません")

        def _finalize():
            # 内部素材の後始末: 生 Matte マスクは製品ではないので削除。
            # Beauty のチェックが無い場合（合成のためだけにレンダした場合）も削除。
            for p, keep in ((matte_path, False),
                            (beauty_path, self.beauty_var.get())):
                if p and not keep and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            _restore_fb()
            self.status_var.set("完了")

        def _run_jobs():
            if not jobs:
                _finalize()
                return
            j = jobs.pop(0)

            def _jdone(ok, od, _j=j):
                # behind ジョブはマットシルエットで通常 Beauty と合成し behindmatte.png を作る
                if ok and _j.get("composite"):
                    try:
                        c = self._current_camera()
                        inter = os.path.join(out, _j["base"] + ".png")
                        core.composite_behind_in_matte(
                            core._get_editor_world(), core.get_camera_settings(c),
                            _j["matte"], beauty_path, inter,
                            os.path.join(out, _name("Behind") + ".png"),
                            W, H)
                        # 中間の全画面 near-clip は残さない（最終 behindmatte.png のみ）
                        try:
                            if os.path.isfile(inter):
                                os.remove(inter)
                        except Exception:
                            pass
                    except Exception as e:
                        self.status_var.set("behind 合成エラー: %s" % e)
                _run_jobs()
            self.status_var.set("追加 MRQ レンダ中… (%s)" % j["base"])
            self.root.update()
            try:
                capture_mrq.render_beauty(cam, out, W, H, use_exr=exr,
                                          temporal_samples=ts, warmup=warm,
                                          file_basename=j["base"],
                                          hidden_actors=j["hidden"],
                                          near_clip_cm=j.get("near_clip"),
                                          fog_off=self.fog_off_var.get(), on_done=_jdone)
            except Exception as e:
                self.status_var.set("追加 MRQ 失敗: %s" % e)

        def _after_beauty(ok, od):
            if ok:
                try:
                    if want_mfront and matte_path:
                        core.blend_with_beauty(
                            beauty_path, matte_path, None,
                            matte_out=os.path.join(out, _name("MatteBeauty") + ".png"))
                except Exception as e:
                    _restore_fb()
                    self.status_var.set("Beautyブレンドでエラー: %s" % e)
                    return
                _run_jobs()
                return
            _restore_fb()
            self.status_var.set("MRQ 失敗: " + od)

        self.status_var.set("MRQ Beauty レンダ中… (PIEに入ります / 完了まで待機)")
        self.root.update()
        try:
            capture_mrq.render_beauty(cam, out, W, H, use_exr=exr,
                                      temporal_samples=ts, warmup=warm,
                                      file_basename=_name("Beauty"),
                                      hidden_actors=beauty_hidden,
                                      fog_off=self.fog_off_var.get(), on_done=_after_beauty)
        except Exception as e:
            _restore_fb()
            self.status_var.set("MRQ 起動失敗: %s" % e)

    # ------------------------------------------------------ sequence render
    def _current_sequence(self):
        """Sequencer で現在開いている LevelSequence（無ければ None）。"""
        try:
            return unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()
        except Exception:
            return None

    def _sequence_camera_at(self, seq, frame):
        """プレイヘッドを frame に合わせ、カメラカットに束縛されたカメラアクターを返す。
        バインディング解決に失敗した場合はレベル内の先頭カメラにフォールバック。"""
        try:
            unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(int(frame))
        except Exception:
            pass
        ext = unreal.MovieSceneSequenceExtensions
        try:
            world = core._get_editor_world()
            sec = ext.find_tracks_by_exact_type(
                seq, unreal.MovieSceneCameraCutTrack)[0].get_sections()[0]
            guid = sec.get_camera_binding_id().get_editor_property("guid")
            for b in ext.get_bindings(seq):
                if b.get_id() == guid:
                    for o in ext.locate_bound_objects(seq, b, world):
                        if isinstance(o, unreal.Actor):
                            return o
        except Exception:
            pass
        cams = core.list_cameras()
        return cams[0] if cams else None

    def _refresh_sequence(self):
        seq = self._current_sequence()
        if seq is None:
            self.seq_name_var.set("(Sequencer で開いていません)")
            return
        try:
            ext = unreal.MovieSceneSequenceExtensions
            s = ext.get_playback_start(seq)
            e = ext.get_playback_end(seq)          # end は排他的
            fr = ext.get_display_rate(seq)
            fps = float(fr.numerator) / max(float(fr.denominator), 1.0)
            self.seq_name_var.set("%s  [%d〜%d @%gfps]" % (seq.get_name(), s, e - 1, fps))
        except Exception:
            self.seq_name_var.set(seq.get_name())

    def _on_seq_render(self):
        """Sequencer で開いている LevelSequence をレンダ（非同期・PIE）。
        MRQ は PNG 連番（マスター）のみを出力し、MP4 はシーケンスの Display Rate で
        ffmpeg エンコードする（fps を確実に一致させるため）。余剰フレームは
        エンコード前にトリムする。素材ごとに PNG連番/MP4 を選択できる。"""
        import capture_mrq
        importlib.reload(capture_mrq)
        seq = self._current_sequence()
        if seq is None:
            self.status_var.set("シーケンスレンダ: Sequencer でシーケンスを開いてください")
            return
        self._refresh_sequence()
        base_out = self.seq_out_var.get().strip()
        if not base_out:
            self.status_var.set("シーケンスレンダ: 出力先を指定してください")
            return
        if not os.path.isdir(base_out):
            try:
                os.makedirs(base_out)
            except Exception:
                pass
        W = self._int_var(self.seq_w_var, 1920)
        H = self._int_var(self.seq_h_var, 1080)
        warm = self._int_var(self.seq_warm_var, 32)
        ts = self._int_var(self.seq_ts_var, 8)
        ext = unreal.MovieSceneSequenceExtensions
        cs = ce = None
        if self.seq_range_mode.get() == "custom":
            cs = self._int_var(self.seq_start_var, 0)
            ce = self._int_var(self.seq_end_var, cs) + 1   # UI は End含む → 排他へ
            if ce <= cs:
                self.status_var.set("シーケンスレンダ: フレーム範囲が不正です (End は Start 以上)")
                return
            cs_eff, ce_eff = cs, ce
        else:
            cs_eff = ext.get_playback_start(seq)
            ce_eff = ext.get_playback_end(seq)
        fr = ext.get_display_rate(seq)
        fps_num, fps_den = fr.numerator, max(fr.denominator, 1)

        wants = {k: (pv.get(), mv.get()) for k, (pv, mv) in self.seq_out_vars.items()}
        if not any(p or m for p, m in wants.values()):
            self.status_var.set("シーケンスレンダ: 出力素材が1つも選ばれていません")
            return

        def _need(key):
            return wants[key][0] or wants[key][1]

        mp4_any = any(m for _, m in wants.values())
        ffmpeg = None
        if mp4_any:
            ffmpeg = core.find_ffmpeg(getattr(self, "_ffmpeg_hint", None))
            if not ffmpeg:
                self.status_var.set("MP4 出力には ffmpeg が必要です（見つかりません。"
                                    "設定 JSON の ffmpeg_path か PATH を確認）")
                return
            self._ffmpeg_hint = ffmpeg
        crf = self._resolve_crf()

        matte_needed = _need("mfront") or _need("behind")
        depth_needed = _need("depth")
        objid_needed = _need("objid")

        matte_actors = None
        if matte_needed or self.seq_matte_hide_var.get():
            matte_actors = core._resolve_target_actors(
                None, self._pick_targets_resolved(self.matte_pick) or None)
            if not matte_actors:
                self.status_var.set("シーケンスレンダ: Matte 対象が見つかりません"
                                    "（画像タブの Matte targets か選択を確認）")
                return
        objid_actors = None
        if objid_needed:
            objid_actors = core._resolve_target_actors(
                None, self._pick_targets_resolved(self.objid_pick) or None)
            if not objid_actors:
                self.status_var.set("シーケンスレンダ: ObjectID 対象が見つかりません"
                                    "（画像タブの Object ID targets か選択を確認）")
                return

        take_str = "%03d" % core.next_take_number(base_out)
        parts = []
        if self.seq_usecustom_var.get():
            c = self.seq_custom_var.get().strip()
            if c:
                parts.append(core._safe_name(c))
        parts.append(core._safe_name(seq.get_name()))
        name_body = "_".join(parts)
        out = base_out
        if self.seq_subdir_var.get():
            out = os.path.join(base_out, "%s_%s" % (name_body, take_str))
        self._save_ui_state()

        depth_mat = matte_mat = objid_mat = None
        hide_actors = None
        try:
            if depth_needed:
                depth_mat = core.create_temp_depth_material(
                    self._float_var(self.seq_near_var, 0.0),
                    self._float_var(self.seq_far_var, 10000.0),
                    invert=True)   # 手前=白 / 奥=黒 固定
            if matte_needed:
                matte_mat = core.create_temp_matte_material()
            elif self.seq_matte_hide_var.get():
                hide_actors = matte_actors
            if objid_needed:
                objid_mat = core.create_temp_objid_material()
        except Exception as e:
            self.status_var.set("一時マテリアル生成失敗: %s" % e)
            return

        def _cleanup_materials():
            if depth_mat is not None:
                core.delete_temp_depth_material()
            if matte_mat is not None:
                core.delete_temp_matte_material()
            if objid_mat is not None:
                core.delete_temp_objid_material()

        def _final(ok, od):
            _cleanup_materials()
            self.status_var.set(("シーケンスレンダ完了: %s" % od) if ok
                                else "シーケンスレンダ失敗 (Output Log 参照)")

        pass_files_main = ["Beauty"]
        if depth_needed:
            pass_files_main.append("Depth")
        if matte_mat is not None:
            pass_files_main.append("Matte")
        if objid_needed:
            pass_files_main.append("ObjectID")

        def _finish_outputs(ok, od):
            """トリム → 合成 → マニフェスト → MP4 エンコード → 不要 PNG 削除。"""
            if not ok:
                _final(False, od)
                return
            try:
                trim_list = list(pass_files_main)
                if _need("behind"):
                    trim_list.append("BehindPlate")
                core.trim_sequence_frames(out, name_body, take_str,
                                          trim_list, cs_eff, ce_eff)
                if _need("mfront"):
                    core.composite_mattefront_sequence(out, name_body, take_str)
                if _need("behind"):
                    core.composite_behind_sequence(out, name_body, take_str)
                if objid_needed and objid_actors:
                    man = {}
                    for i, a in enumerate(objid_actors[:255]):
                        r, g, b = core.objid_stencil_color(i + 1)
                        try:
                            man["#%02X%02X%02X" % (r, g, b)] = a.get_actor_label()
                        except Exception:
                            pass
                    with open(os.path.join(out, "%s_ObjectID_%s.json" % (name_body, take_str)),
                              "w", encoding="utf-8") as f:
                        json.dump(man, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.status_var.set("後処理エラー: %s" % e)
                _final(False, od)
                return

            cmds = []
            for key, _label, pass_name in _SEQ_OUTPUTS:
                if wants[key][1]:
                    cmd, _dst = core.encode_mp4_cmd(ffmpeg, out, name_body, pass_name,
                                                    take_str, fps_num, fps_den, crf, cs_eff)
                    cmds.append(cmd)

            def _after_encode(enc_ok):
                drop = ["Matte", "BehindPlate"]      # 中間素材は常に削除
                for key, _label, pass_name in _SEQ_OUTPUTS:
                    if not wants[key][0]:
                        drop.append(pass_name)
                core.delete_pass_frames(out, name_body, take_str, drop)
                _final(enc_ok, od)

            if cmds:
                self._run_ffmpeg_jobs(cmds, _after_encode)
            else:
                _after_encode(True)

        def _after_main(ok, od):
            if not (ok and _need("behind")):
                _finish_outputs(ok, od)
                return
            # Matteの奥: 開始フレームのカメラ→マット距離で near-clip した2本目ジョブ
            try:
                cam_actor = self._sequence_camera_at(seq, cs_eff)
                if cam_actor is None:
                    raise RuntimeError("シーケンスカメラを特定できません")
                nc = core.matte_near_clip_cm(
                    matte_actors, {"transform": cam_actor.get_actor_transform()})
            except Exception as e:
                self.status_var.set("Matteの奥: near-clip 計算失敗: %s" % e)
                _final(False, od)
                return
            self.status_var.set("Matteの奥プレートをレンダ中… (near-clip %.0fcm)" % nc)
            self.root.update()
            try:
                capture_mrq.render_sequence(
                    seq, out, W, H, name_body, take_str,
                    do_png=True, do_mp4=False,
                    temporal_samples=ts, warmup=warm,
                    custom_start=cs, custom_end=ce,
                    hidden_actors=matte_actors, near_clip_cm=nc,
                    beauty_label="BehindPlate",
                    fog_off=self.seq_fog_var.get(), on_done=_finish_outputs)
            except Exception as e:
                self.status_var.set("Matteの奥プレート起動失敗: %s" % e)
                _final(False, od)

        self.status_var.set("シーケンスレンダ中… (PIE / %d〜%dF @%gfps)"
                            % (cs_eff, ce_eff - 1, float(fps_num) / fps_den))
        self.root.update()
        try:
            capture_mrq.render_sequence(
                seq, out, W, H, name_body, take_str,
                do_png=True, do_mp4=False,
                temporal_samples=ts, warmup=warm,
                custom_start=cs, custom_end=ce,
                depth_material=depth_mat,
                matte_material=matte_mat, matte_actors=matte_actors,
                objid_material=objid_mat, objid_actors=objid_actors,
                hidden_actors=hide_actors, fog_off=self.seq_fog_var.get(),
                on_done=_after_main)
        except Exception as e:
            _cleanup_materials()
            self.status_var.set("シーケンスレンダ起動失敗: %s" % e)

    def _run_ffmpeg_jobs(self, cmds, on_done):
        """ffmpeg を1本ずつ非同期実行し、全完了で on_done(ok)。
        Slate tick でポーリングするのでエディタをブロックしない。"""
        import subprocess
        state = {"i": 0, "p": None, "h": None}

        def _tick(dt):
            p = state["p"]
            if p is None:
                if state["i"] >= len(cmds):
                    unreal.unregister_slate_post_tick_callback(state["h"])
                    on_done(True)
                    return
                try:
                    state["p"] = subprocess.Popen(
                        cmds[state["i"]], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=0x08000000)
                except Exception as e:
                    unreal.unregister_slate_post_tick_callback(state["h"])
                    self.status_var.set("ffmpeg 起動失敗: %s" % e)
                    on_done(False)
                    return
                state["i"] += 1
                self.status_var.set("MP4 エンコード中… (%d/%d)" % (state["i"], len(cmds)))
                return
            rc = p.poll()
            if rc is None:
                return
            state["p"] = None
            if rc != 0:
                unreal.unregister_slate_post_tick_callback(state["h"])
                self.status_var.set("ffmpeg 失敗 (exit %d)" % rc)
                on_done(False)

        state["h"] = unreal.register_slate_post_tick_callback(_tick)

    def _make_picker(self, frm, row, label):
        """対象アクターのリストを作る。リストの中身＝対象。
        Add Sel: アウトライナ/ビューポートの選択を追加 / Clear: リストで選択した項目を削除。
        キーはフルパス名だが、追加時のラベルも保持し、パスが解決できなくなった場合
        （再インポート等でアクターが作り直された場合）はラベルで再解決する。"""
        p = {"all": [], "labels": {}}
        bar = ttk.Frame(frm)
        ttk.Label(bar, text=label).pack(side="left")
        ttk.Button(bar, text="Add Sel", width=8,
                   command=lambda: self._pick_add_selection(p)).pack(side="right")
        ttk.Button(bar, text="Clear", width=6,
                   command=lambda: self._pick_clear(p)).pack(side="right", padx=3)
        bar.grid(row=row, column=0, columnspan=3, sticky="we", padx=24)
        row += 1
        lbf = ttk.Frame(frm)
        p["list"] = tk.Listbox(lbf, selectmode="extended", height=4,
                               exportselection=False, activestyle="none")
        sb = ttk.Scrollbar(lbf, orient="vertical", command=p["list"].yview)
        p["list"].configure(yscrollcommand=sb.set)
        p["list"].pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        lbf.grid(row=row, column=0, columnspan=3, sticky="we", padx=24)
        row += 1
        return p, row

    def _pick_targets(self, p):
        """リストの全項目＝対象アクターのフルパス名（get_path_name()）。
        一意かつ、レベル側でラベルをリネームしても不変。"""
        return list(p["all"])

    def _pick_targets_resolved(self, p):
        """解決用の名前リスト。パスが現在のレベルに存在すればパス、無ければ
        追加時に保存したラベルへフォールバックする（再インポート等でアクターが
        作り直されるとパスが変わるため。ラベルが同じなら自動で追従できる）。"""
        p2l = self._path2label()
        out = []
        for path in p["all"]:
            if path in p2l:
                out.append(path)
            else:
                out.append(p.get("labels", {}).get(path) or path)
        return out

    def _path2label(self):
        """現在レベルの フルパス名→ラベル の対応を作る（表示用に毎回ライブ取得）。"""
        m = {}
        try:
            for a in core._actor_subsystem().get_all_level_actors():
                m[a.get_path_name()] = a.get_actor_label()
        except Exception:
            pass
        return m

    def _pick_refresh(self, p):
        # p["all"] はフルパス名のリスト。表示は現在のラベルをライブ取得する（リネーム追従）。
        # レベルに無いパスは保存済みラベルでの再解決を試みる旨を表示する。
        p2l = self._path2label()
        p["list"].delete(0, "end")
        for path in p["all"]:
            lab = p2l.get(path)
            short = path.rsplit(".", 1)[-1]    # 末尾の内部名だけ補助表示
            if lab:
                p["list"].insert("end", "%s  [%s]" % (lab, short))
            else:
                saved = p.get("labels", {}).get(path)
                p["list"].insert("end", "%s  (パス無し→ラベル'%s'で解決)" % (short, saved)
                                 if saved else "%s  (レベルに無し)" % short)

    def _pick_add_selection(self, p):
        """選択中アクターをリストへ追加。キーはフルパス名（get_path_name()）で重複無視。
        追加時のラベルも保持する（パスが失効した場合のフォールバック解決用）。"""
        sel = core.get_selected_actors()
        added = 0
        for a in sel:
            try:
                path = a.get_path_name()
                p.setdefault("labels", {})[path] = a.get_actor_label()
            except Exception:
                continue
            if path not in p["all"]:
                p["all"].append(path)
                added += 1
        self._pick_refresh(p)
        self.status_var.set("Added %d (list total %d)" % (added, len(p["all"])))

    def _pick_clear(self, p):
        """リスト上で選択（ハイライト）した項目を行インデックスで削除する。"""
        idx = sorted(p["list"].curselection(), reverse=True)
        if not idx:
            self.status_var.set("Clear: リスト内で消したい項目を選択してください")
            return
        for i in idx:
            del p["all"][i]
        self._pick_refresh(p)
        self.status_var.set("Removed %d (list total %d)" % (len(idx), len(p["all"])))

    def _current_camera(self):
        """選択中ラベルのカメラを毎回ライブで取得（キャッシュ参照は PIE 後に無効化するため）。"""
        label = self.cam_var.get()
        cams = core.list_cameras()
        self._cameras = cams
        for c in cams:
            try:
                if c.get_actor_label() == label:
                    return c
            except Exception:
                continue
        return cams[0] if cams else None

    def _aspect_ratio(self):
        cam = self._current_camera()
        return core.get_camera_settings(cam).get("aspect_ratio", 0.0) if cam else 0.0

    def _set_wh(self, wh):
        """w/h をまとめて設定（アスペクトロックのトレースと競合しないようガード）。"""
        self._aspect_guard = True
        self.w_var.set(wh[0])
        self.h_var.set(wh[1])
        self._aspect_guard = False

    def _on_res_mode_change(self, *a):
        """Camera⇄Override 切替。Camera に戻す時は元解像度を復元、Override に入る時は
        前回の Override 入力（カメラ切替まで維持）を復元する。"""
        new = self.res_mode.get()
        prev = getattr(self, "_prev_res_mode", new)
        if prev == "camera" and new == "override":
            self._saved_cam_wh = (self.w_var.get(), self.h_var.get())
            if self._saved_override_wh:               # 以前の Override 入力を維持
                self._set_wh(self._saved_override_wh)
        elif prev == "override" and new == "camera":
            self._saved_override_wh = (self.w_var.get(), self.h_var.get())  # Override 入力を記憶
            if self._saved_cam_wh:
                self._set_wh(self._saved_cam_wh)
        self._prev_res_mode = new
        self._update_cam_res()

    def _on_camera_change(self):
        """カメラを切り替えたら Override の維持をリセット（新カメラ基準にする）。"""
        self._saved_override_wh = None
        self._update_cam_res()

    def _on_width_change(self, *a):
        """幅が変わったら解像度表示を更新し、Override+アスペクト維持なら高さ(=W/asp)を自動算出。"""
        self._update_cam_res()
        if self._aspect_guard:
            return
        try:
            if self.aspect_lock_var.get() and self.res_mode.get() == "override":
                asp = self._aspect_ratio()
                W = int(self.w_var.get())
                if asp > 0.1:
                    h = str(int(round(W / asp)))
                    if self.h_var.get() != h:
                        self._aspect_guard = True
                        self.h_var.set(h)
                        self._aspect_guard = False
        except Exception:
            self._aspect_guard = False

    def _on_height_change(self, *a):
        """高さが変わったら、Override+アスペクト維持なら幅(=H*asp)を自動算出。"""
        if self._aspect_guard:
            return
        try:
            if self.aspect_lock_var.get() and self.res_mode.get() == "override":
                asp = self._aspect_ratio()
                H = int(self.h_var.get())
                if asp > 0.1:
                    w = str(int(round(H * asp)))
                    if self.w_var.get() != w:
                        self._aspect_guard = True
                        self.w_var.set(w)
                        self._aspect_guard = False
                        self._update_cam_res()
        except Exception:
            self._aspect_guard = False

    def _update_cam_res(self):
        """選択カメラのアスペクトと、現在の幅から算出した解像度を表示する。"""
        try:
            cam = self._current_camera()
            if cam is None:
                self.cam_res_var.set("(no camera)")
                return
            asp = core.get_camera_settings(cam).get("aspect_ratio", 0.0)
            W = int(self.w_var.get())
            H = int(round(W / asp)) if asp > 0.1 else 0
            self.cam_res_var.set("→ %d×%d  (%.3f:1)" % (W, H, asp))
        except Exception:
            self.cam_res_var.set("")

    def _refresh_cameras(self):
        """現在のレベルのカメラを取得し直してプルダウンを更新する。"""
        prev = self.cam_var.get()
        self._cameras = core.list_cameras()
        labels = [c.get_actor_label() for c in self._cameras] or ["(no camera)"]
        self.cam_combo["values"] = labels
        if prev in labels:
            self.cam_combo.current(labels.index(prev))
        else:
            self.cam_combo.current(0)
        self.status_var.set("Cameras refreshed: %d" % len(self._cameras))
        self._update_cam_res()

    # ----------------------------------------------------------- 設定の保持
    def _settings_path(self):
        return os.path.normpath(os.path.join(
            unreal.Paths.project_saved_dir(), "UE5Capture_ui_settings.json"))

    def _save_ui_state(self):
        try:
            state = {
                "camera": self.cam_var.get(),
                "res_mode": self.res_mode.get(),
                "w": self.w_var.get(), "h": self.h_var.get(),
                "aa": self.aa_var.get(),
                "overscan": self.overscan_var.get(),
                "overscan_on": self.overscan_on_var.get(),
                "overscan_mode": self.overscan_mode_var.get(),
                "overscan_x": self.overscan_x_var.get(),
                "overscan_y": self.overscan_y_var.get(),
                "aspect_lock": self.aspect_lock_var.get(),
                "out": self.out_var.get(),
                "name_usecustom": self.name_usecustom_var.get(),
                "name_custom": self.name_custom_var.get(),
                "name_usecam": self.name_usecam_var.get(),
                "depth": self.depth_var.get(),
                "beauty": self.beauty_var.get(),
                "mfront": self.mfront_var.get(),
                "behind": self.behind_var.get(),
                "objid": self.objid_var.get(),
                "matte_names": self._pick_targets(self.matte_pick),
                "objid_names": self._pick_targets(self.objid_pick),
                "matte_labels": self.matte_pick.get("labels", {}),
                "objid_labels": self.objid_pick.get("labels", {}),
                "depth_bit": self.depth_bit_var.get(),
                "near": self.near_var.get(), "far": self.far_var.get(),
                "mrq_warmup": self.mrq_warmup_var.get(),
                "mrq_ts": self.mrq_ts_var.get(),
                "mrq_exr": self.mrq_exr_var.get(),
                "mrq_camasp": self.mrq_camasp_var.get(),
                "fog_off": self.fog_off_var.get(),
                "seq_range_mode": self.seq_range_mode.get(),
                "seq_start": self.seq_start_var.get(),
                "seq_end": self.seq_end_var.get(),
                "seq_rate": self.seq_rate_var.get(),
                "seq_matte_hide": self.seq_matte_hide_var.get(),
                "seq_subdir": self.seq_subdir_var.get(),
                "seq_outputs": {k: [pv.get(), mv.get()]
                                for k, (pv, mv) in self.seq_out_vars.items()},
                "ffmpeg_path": getattr(self, "_ffmpeg_hint", "") or "",
                "seq_w": self.seq_w_var.get(), "seq_h": self.seq_h_var.get(),
                "seq_warm": self.seq_warm_var.get(), "seq_ts": self.seq_ts_var.get(),
                "seq_fog": self.seq_fog_var.get(),
                "seq_out": self.seq_out_var.get(),
                "seq_usecustom": self.seq_usecustom_var.get(),
                "seq_custom": self.seq_custom_var.get(),
                "seq_near": self.seq_near_var.get(),
                "seq_far": self.seq_far_var.get(),
            }
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            unreal.log_warning("[SceneCapture] 設定保存に失敗: %s" % e)

    def _load_ui_state(self):
        p = self._settings_path()
        if not os.path.isfile(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception as e:
            unreal.log_warning("[SceneCapture] 設定読込に失敗: %s" % e)
            return

        def _setvar(var, key):
            if key in st and st[key] is not None:
                var.set(st[key])
        cam = st.get("camera")
        if cam and cam in self.cam_combo["values"]:
            self.cam_combo.set(cam)
        _setvar(self.res_mode, "res_mode")
        _setvar(self.w_var, "w"); _setvar(self.h_var, "h")
        _setvar(self.aa_var, "aa")
        _setvar(self.overscan_var, "overscan")
        _setvar(self.overscan_on_var, "overscan_on")
        if st.get("overscan_mode") in ("percent", "pixels"):
            self.overscan_mode_var.set(st["overscan_mode"])
        _setvar(self.overscan_x_var, "overscan_x")
        _setvar(self.overscan_y_var, "overscan_y")
        _setvar(self.aspect_lock_var, "aspect_lock")
        _setvar(self.out_var, "out")
        _setvar(self.name_usecustom_var, "name_usecustom")
        _setvar(self.name_custom_var, "name_custom")
        _setvar(self.name_usecam_var, "name_usecam")
        _setvar(self.depth_var, "depth")
        _setvar(self.beauty_var, "beauty")
        _setvar(self.mfront_var, "mfront")
        _setvar(self.behind_var, "behind")
        _setvar(self.objid_var, "objid")

        def _restore_picker(p, names_key, labels_key):
            names = st.get(names_key)
            if isinstance(names, str):   # 旧形式互換
                names = [x.strip() for x in names.split(",") if x.strip()]
            labels = st.get(labels_key)
            if isinstance(labels, dict):
                p["labels"] = dict(labels)
            if names:
                # 旧設定はラベル/内部名を保存していた。ラベル一致するものはフルパス名へ移行する。
                label2path = {v: k for k, v in self._path2label().items()}
                p["all"] = [label2path.get(n, n) for n in names]
                self._pick_refresh(p)
        _restore_picker(self.matte_pick, "matte_names", "matte_labels")
        _restore_picker(self.objid_pick, "objid_names", "objid_labels")
        if st.get("depth_bit") in ("8bit PNG", "16bit PNG", "EXR float"):
            self.depth_bit_var.set(st["depth_bit"])
        _setvar(self.near_var, "near"); _setvar(self.far_var, "far")
        _setvar(self.mrq_warmup_var, "mrq_warmup")
        _setvar(self.mrq_ts_var, "mrq_ts")
        _setvar(self.mrq_exr_var, "mrq_exr")
        _setvar(self.mrq_camasp_var, "mrq_camasp")
        _setvar(self.fog_off_var, "fog_off")
        if st.get("seq_range_mode") in ("auto", "custom"):
            self.seq_range_mode.set(st["seq_range_mode"])
        _setvar(self.seq_start_var, "seq_start")
        _setvar(self.seq_end_var, "seq_end")
        _setvar(self.seq_rate_var, "seq_rate")   # プリセット名 or CRF 数値そのまま
        _setvar(self.seq_matte_hide_var, "seq_matte_hide")
        _setvar(self.seq_subdir_var, "seq_subdir")
        outs = st.get("seq_outputs")
        if isinstance(outs, dict):
            for k, (pv, mv) in self.seq_out_vars.items():
                v = outs.get(k)
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    pv.set(bool(v[0]))
                    mv.set(bool(v[1]))
        fp = st.get("ffmpeg_path")
        if fp:
            self._ffmpeg_hint = fp
        _setvar(self.seq_w_var, "seq_w"); _setvar(self.seq_h_var, "seq_h")
        _setvar(self.seq_warm_var, "seq_warm"); _setvar(self.seq_ts_var, "seq_ts")
        _setvar(self.seq_fog_var, "seq_fog")
        _setvar(self.seq_out_var, "seq_out")
        _setvar(self.seq_usecustom_var, "seq_usecustom")
        _setvar(self.seq_custom_var, "seq_custom")
        _setvar(self.seq_near_var, "seq_near")
        _setvar(self.seq_far_var, "seq_far")

    def _collect_settings(self):
        s = core.CaptureSettings()
        s.camera_actor = self._current_camera()
        s.use_camera_resolution = (self.res_mode.get() == "camera")
        s.override_width = self._int_var(self.w_var, s.override_width)
        s.override_height = self._int_var(self.h_var, s.override_height)
        s.aa_factor = {"1x": 1, "2x": 2, "4x": 4}.get(self.aa_var.get(), 2)
        s.output_dir = self.out_var.get().strip()
        s.name_prefix = self.name_custom_var.get().strip() if self.name_usecustom_var.get() else ""
        s.name_include_camera = self.name_usecam_var.get()
        s.fog_off = self.fog_off_var.get()
        s.do_color = False                 # 旧 Color(SceneCapture) は廃止。Beauty は MRQ で出す。
        s.do_depth = self.depth_var.get()
        s.do_matte = self.mfront_var.get()   # MatteBeauty 合成用の内部マスク（製品ではない）
        s.matte_invert = True                # 選択=黒/周囲=白 で固定
        s.matte_fill_alpha = False           # 合成は MRQ Beauty 側で行う
        s.depth_hide_matte = self.mfront_var.get() or self.behind_var.get()
        s.do_behind_matte = self.behind_var.get()
        s.do_object_id = self.objid_var.get()
        s.objid_fill_alpha = False
        s.objid_hide_render = False
        # 対象リスト（リストの中身＝対象。空ならエディタ選択にフォールバック。
        # パス失効時はラベルで再解決）
        s.matte_actor_names = self._pick_targets_resolved(self.matte_pick) or None
        s.objid_actor_names = self._pick_targets_resolved(self.objid_pick) or None
        dsel = self.depth_bit_var.get()
        if dsel.startswith("8"):
            s.depth_bit = "8bit"
        elif dsel.startswith("16"):
            s.depth_bit = "16bit"
        else:
            s.depth_bit = "exr"
        s.depth_invert = True                # 手前=白 / 奥=黒 固定
        s.depth_near = self._float_var(self.near_var, s.depth_near)
        s.depth_far = self._float_var(self.far_var, s.depth_far)
        return s

    # ------------------------------------------------------------ UE tick
    def _register_tick(self):
        _unregister_global_tick()      # reload 後の二重登録を防ぐ
        def _tick(dt):
            try:
                self.root.update()
            except Exception:
                _unregister_global_tick()
        unreal._ue5capture_tick_handle = unreal.register_slate_post_tick_callback(_tick)

    def _on_close(self):
        """閉じる=withdraw。ルートは destroy しない（再生成時に Tcl panic で
        エディタごと落ちるため）。再表示は show() が deiconify する。"""
        self._save_ui_state()
        _unregister_global_tick()
        try:
            self.root.withdraw()
        except Exception:
            pass


_window_ref = None  # GC 防止


def _persistent_root():
    """reload をまたいで使い回す唯一の Tk ルート（unreal モジュールに保持）。"""
    root = getattr(unreal, "_ue5capture_tk_root", None)
    if root is not None:
        try:
            root.winfo_exists()
        except Exception:
            root = None
    if root is None:
        root = tk.Tk()
        unreal._ue5capture_tk_root = root
    return root


def _unregister_global_tick():
    h = getattr(unreal, "_ue5capture_tick_handle", None)
    if h is not None:
        try:
            unreal.unregister_slate_post_tick_callback(h)
        except Exception:
            pass
        unreal._ue5capture_tick_handle = None


def _close_legacy_windows():
    """旧実装（destroy 方式）が unreal._ue5capture_windows に残したウィンドウを
    withdraw で畳む。destroy は絶対に呼ばない。"""
    reg = getattr(unreal, "_ue5capture_windows", None)
    if not reg:
        return
    for w in list(reg):
        try:
            h = getattr(w, "_tick_handle", None)
            if h is not None:
                unreal.unregister_slate_post_tick_callback(h)
        except Exception:
            pass
        try:
            w.root.withdraw()
        except Exception:
            pass
    reg[:] = []


def close_all_windows():
    """ツールウィンドウを閉じる（withdraw のみ。ルートは保持）。"""
    _close_legacy_windows()
    _unregister_global_tick()
    root = getattr(unreal, "_ue5capture_tk_root", None)
    if root is not None:
        try:
            root.withdraw()
        except Exception:
            pass


def show():
    """GUI を表示。永続ルートを使い回し、UI だけ作り直す（reload 対応）。"""
    global _window_ref
    _close_legacy_windows()
    _window_ref = CaptureWindow()
    return _window_ref
