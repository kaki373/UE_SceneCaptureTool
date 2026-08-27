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
    ("normal", "Normal（法線）", "Normal"),
    ("mfront", "Matteの前（Beauty+Matte）", "MatteBeauty"),
    ("behind", "Matteの奥", "Behind"),
    ("cloudmatte", "雲マット（VDB雲=黒）", "CloudMatte"),
    ("skymatte", "空マット（空=黒・大気光維持）", "SkyMatte"),
    ("objid", "ObjectID", "ObjectID"),
    ("rlfull", "Raw Lighting Full(Sun+GI+Sky)", "RawLightingFull"),
    ("rldir", "Raw Lighting Direct", "RawLightingDirect"),
]

# 画像タブの出力形式 → 拡張子（Beauty/Raw Lighting 系で共用）
_FMT_EXT = {"png": ".png", "jpg": ".jpg", "exr": ".exr"}

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
        """従来の単発キャプチャ UI。ウィジェット/変数/ロジックは従来のまま、
        「カメラ / 解像度」「出力先 / ファイル名」「出力素材」の 3 枠に区分けのみ。"""
        # ---- カメラ / 解像度 ----
        camf = ttk.LabelFrame(frm, text="カメラ / 解像度")
        row = 0

        # Camera（Refresh で現在のレベルのカメラに更新）
        ttk.Label(camf, text="Camera:").grid(row=row, column=0, sticky="w", **pad)
        self.cam_var = tk.StringVar(master=self.root)
        cam_labels = [c.get_actor_label() for c in self._cameras] or ["(no camera)"]
        self.cam_combo = ttk.Combobox(camf, textvariable=self.cam_var,
                                      values=cam_labels, state="readonly", width=28)
        self.cam_combo.current(0)
        self.cam_combo.grid(row=row, column=1, sticky="we", **pad)
        ttk.Button(camf, text="⟳", width=3, command=self._refresh_cameras).grid(
            row=row, column=2, sticky="w")
        self.cam_combo.bind("<<ComboboxSelected>>", lambda e: self._on_camera_change())
        row += 1

        # Resolution
        ttk.Label(camf, text="Resolution:").grid(row=row, column=0, sticky="nw", **pad)
        camrow = ttk.Frame(camf)
        self.res_mode = tk.StringVar(master=self.root, value="camera")
        ttk.Radiobutton(camrow, text="Use Camera Setting", variable=self.res_mode,
                        value="camera").pack(side="left")
        self.cam_res_var = tk.StringVar(master=self.root, value="")
        ttk.Label(camrow, textvariable=self.cam_res_var, foreground="#0a7").pack(side="left", padx=(8, 0))
        camrow.grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1
        ovr = ttk.Frame(camf)
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
        ttk.Label(camf, text="Overscan:").grid(row=row, column=0, sticky="w", **pad)
        osf = ttk.Frame(camf)
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
        ttk.Label(camf, text="anti-aliasing:").grid(row=row, column=0, sticky="w", **pad)
        self.aa_var = tk.StringVar(master=self.root, value="2x")
        ttk.Combobox(camf, textvariable=self.aa_var, values=["1x", "2x", "4x"],
                     state="readonly", width=8).grid(row=row, column=1, sticky="w", **pad)
        camf.columnconfigure(1, weight=1)
        camf.grid(row=0, column=0, sticky="we", padx=6, pady=(6, 2))

        # ---- 出力先 / ファイル名 ----
        outf = ttk.LabelFrame(frm, text="出力先 / ファイル名")
        row = 0

        # Output dir
        ttk.Label(outf, text="Output Dir:").grid(row=row, column=0, sticky="w", **pad)
        default_dir = os.path.normpath(
            os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
        self.out_var = tk.StringVar(master=self.root, value=default_dir)
        tk.Entry(outf, textvariable=self.out_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        ttk.Button(outf, text="...", width=3, command=self._browse).grid(
            row=row, column=2, sticky="w")
        row += 1

        # ファイル名: [任意名]_[カメラ名]_素材名_001（任意名/カメラ名は下のチェックで含める）
        self.name_usecustom_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(outf, text="任意名を付ける:", variable=self.name_usecustom_var).grid(
            row=row, column=0, sticky="w", **pad)
        self.name_custom_var = tk.StringVar(master=self.root, value="")
        tk.Entry(outf, textvariable=self.name_custom_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        row += 1
        self.name_usecam_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(outf, text="カメラ名を付ける", variable=self.name_usecam_var).grid(
            row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        ttk.Label(outf, text="  ファイル名: [任意名]_[カメラ名]_素材名_NNN",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        outf.columnconfigure(1, weight=1)
        outf.grid(row=1, column=0, sticky="we", padx=6, pady=2)

        # ---- 出力素材 ----
        matf = ttk.LabelFrame(frm, text="出力素材")
        row = 0

        # 品質と出力形式（Beauty より上に置く）
        mrqf = ttk.Frame(matf)
        ttk.Label(mrqf, text="ウォームアップ:").pack(side="left")
        self.mrq_warmup_var = tk.StringVar(master=self.root, value="32")
        tk.Entry(mrqf, textvariable=self.mrq_warmup_var, width=5).pack(side="left", padx=2)
        ttk.Label(mrqf, text="サンプリングフレーム:").pack(side="left", padx=(8, 0))
        self.mrq_ts_var = tk.StringVar(master=self.root, value="8")
        tk.Entry(mrqf, textvariable=self.mrq_ts_var, width=5).pack(side="left", padx=2)
        mrqf.grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        fmtf = ttk.Frame(matf)
        ttk.Label(fmtf, text="出力形式:").pack(side="left")
        self.beauty_fmt_var = tk.StringVar(master=self.root, value="PNG 8bit")
        ttk.Combobox(fmtf, textvariable=self.beauty_fmt_var, state="readonly", width=14,
                     values=["PNG 8bit", "JPG 8bit", "EXR 16bit (float)"]).pack(
            side="left", padx=4)
        ttk.Label(fmtf, text="（Beauty の形式。Matte系合成/ObjectID は PNG）",
                  foreground="#888").pack(side="left")
        fmtf.grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        ttk.Frame(matf, height=8).grid(row=row, column=0)   # 余白
        row += 1

        # Beauty（MRQ = ビューポート露出＋シーケンサ品質）
        self.beauty_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(matf, text="Beauty（MRQ = ビューポート露出＋シーケンサ品質）",
                        variable=self.beauty_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        mrqf2 = ttk.Frame(matf)
        self.mrq_camasp_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(mrqf2, text="カメラのアスペクト",
                        variable=self.mrq_camasp_var).pack(side="left")
        self.fog_off_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(mrqf2, text="Fogなし", variable=self.fog_off_var).pack(
            side="left", padx=(8, 0))
        # VDB雲モード: Auto=レベルに HV(VDB雲) があれば自動で雲対応（バッキングT/
        # 雲ObjectID/Behindの手前雲hide）。ON=検出に関わらず強制有効。
        # OFF=雲を完全無視した従来の書き出し。画像/映像タブ共通の1セレクタ。
        ttk.Label(mrqf2, text="VDB雲:").pack(side="left", padx=(8, 0))
        self.vdb_var = tk.StringVar(master=self.root, value="Auto")
        ttk.Combobox(mrqf2, textvariable=self.vdb_var, width=5, state="readonly",
                     values=("Auto", "ON", "OFF")).pack(side="left")
        mrqf2.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        # Raw Lighting（MRQ LightingOnly パス = アルベド無視のライティングのみ素材）
        self.rlfull_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="Raw Lighting Full(Sun+GI+Sky)",
                        variable=self.rlfull_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.rldir_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="Raw Lighting Direct",
                        variable=self.rldir_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        ttk.Label(matf, text="  （ライティングのみ素材＝落ち影+シェーディング。Direct は"
                             " GI/スカイライト/AO なしの直射のみ＝追加レンダ1回）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        # Z-Depth（手前=白/奥=黒 固定）
        self.depth_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="Z-Depth（手前=白 / 奥=黒）", variable=self.depth_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        depth_frm = ttk.Frame(matf)
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
        ttk.Label(depth_frm, text="cm（1m=100cm）").pack(side="left")
        depth_frm.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        # Normal（法線 → RGB。-1..1 を *0.5+0.5 で 0..1 に詰める）
        self.normal_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="Normal（法線 RGB = XYZ*0.5+0.5）",
                        variable=self.normal_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        normal_frm = ttk.Frame(matf)
        ttk.Label(normal_frm, text="空間:").pack(side="left")
        self.normal_space_var = tk.StringVar(master=self.root, value="カメラ")
        ttk.Combobox(normal_frm, textvariable=self.normal_space_var, state="readonly",
                     width=7, values=["カメラ", "ワールド"]).pack(side="left", padx=4)
        ttk.Label(normal_frm, text="（カメラ=正対面が青 / ワールド=上向きが青・VDB雲領域は黒）",
                  foreground="#888").pack(side="left")
        normal_frm.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        # Matte 系（Beauty+Matte / Matteの奥。対象は Matte targets）
        self.mfront_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="Matteの前（Beauty+Matte）",
                        variable=self.mfront_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.behind_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="Matteの奥",
                        variable=self.behind_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        ttk.Label(matf, text="  （対象は下の Matte targets、空ならエディタ選択。出力時は Beauty から対象を自動で隠す）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.matte_pick, row = self._make_picker(matf, row, "Matte targets")
        # マット対象はリストに入れた時点でライティングから分離、外したら復元する
        self.matte_pick["on_added"] = self._matte_paths_neutralize
        self.matte_pick["on_removed"] = self._matte_paths_restore
        ttk.Label(matf, text="  （リストに入れた対象は自動で 影/AO/受光なし になり、外すと元に戻ります）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        # ObjectID（対象を色分け・他は黒）
        self.objid_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="ObjectID（対象を色分け・他は黒 + 色↔名前 JSON）",
                        variable=self.objid_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.objid_pick, row = self._make_picker(matf, row, "Object ID targets")

        # 雲マット（VDB雲のみの白黒マスク。I2I 用のマスク素材）
        self.cloudmatte_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="雲マット（VDB雲=黒 / 背景=白）",
                        variable=self.cloudmatte_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        ttk.Label(matf, text="  （対象はレベル内の全VDB雲。分離モード＝手前ジオメトリの遮蔽は反映されない）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        # 空マット（空=純黒・大気光/雲は維持。ue-sky-matte-capture 方式）
        self.skymatte_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(matf, text="空マット（空=黒・大気光/雲は維持）",
                        variable=self.skymatte_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        ttk.Label(matf, text="  （UDSのSky_Sphereを黒ドームに差替え・PPマテリアル/ブルームOFFの追加レンダ1回）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        matf.columnconfigure(1, weight=1)
        matf.grid(row=2, column=0, sticky="we", padx=6, pady=2)

        self.capture_btn = ttk.Button(
            frm, text="Capture", style="Big.TButton", command=self._on_mrq)
        self.capture_btn.grid(row=3, column=0,
                              pady=(10, 14), padx=24, ipady=6, sticky="we")

        frm.columnconfigure(0, weight=1)

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
        self.seq_h_entry = tk.Entry(res, textvariable=self.seq_h_var, width=6)
        self.seq_h_entry.pack(side="left", padx=2)
        # カメラカットのカメラの aspect_ratio（Cine は filmback 由来・素のカメラは
        # プロパティ値）から高さを自動算出する。ON の間は高さ入力を無効化。
        self.seq_camasp_var = tk.BooleanVar(master=self.root, value=True)
        ttk.Checkbutton(res, text="カメラのアスペクト", variable=self.seq_camasp_var,
                        command=self._sync_seq_h).pack(side="left", padx=(6, 0))
        self.seq_w_var.trace_add("write", lambda *a: self._sync_seq_h())
        ttk.Label(res, text="ウォームアップ:").pack(side="left", padx=(12, 0))
        self.seq_warm_var = tk.StringVar(master=self.root, value="32")
        tk.Entry(res, textvariable=self.seq_warm_var, width=5).pack(side="left", padx=2)
        ttk.Label(res, text="サンプリングフレーム:").pack(side="left", padx=(8, 0))
        self.seq_ts_var = tk.StringVar(master=self.root, value="8")
        tk.Entry(res, textvariable=self.seq_ts_var, width=5).pack(side="left", padx=2)
        res.grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        seqf2 = ttk.Frame(frm)
        self.seq_fog_var = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(seqf2, text="Fogなし", variable=self.seq_fog_var).pack(side="left")
        ttk.Label(seqf2, text="VDB雲:").pack(side="left", padx=(8, 0))
        ttk.Combobox(seqf2, textvariable=self.vdb_var, width=5, state="readonly",
                     values=("Auto", "ON", "OFF")).pack(side="left")
        seqf2.grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=8)
        row += 1

        # 出力先まわり（画像タブと同じ並びで「出力」の上に置く）
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

        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=8)
        row += 1

        ttk.Label(frm, text="出力（素材ごとに PNG連番 / MP4。MP4 はシーケンスの fps で ffmpeg エンコード）:").grid(
            row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        mtx = ttk.Frame(frm)
        ttk.Label(mtx, text="PNG連番").grid(row=0, column=1, padx=8)
        ttk.Label(mtx, text="MP4").grid(row=0, column=2, padx=8)
        rate = ttk.Frame(mtx)
        ttk.Label(rate, text="レート:").pack(side="left")
        self.seq_rate_var = tk.StringVar(master=self.root, value="高 (CRF 20)")
        ttk.Combobox(rate, textvariable=self.seq_rate_var, state="normal", width=11,
                     values=list(_MP4_RATE_PRESETS.keys())).pack(side="left", padx=2)
        ttk.Label(rate, text="(数値可)", foreground="#888").pack(side="left")
        rate.grid(row=0, column=3, sticky="w", padx=(12, 0))
        self.seq_out_vars = {}
        for i, (key, label, _pass) in enumerate(_SEQ_OUTPUTS):
            ttk.Label(mtx, text=label).grid(row=i + 1, column=0, sticky="w", pady=1)
            pv = tk.BooleanVar(master=self.root, value=(key == "beauty"))
            mv = tk.BooleanVar(master=self.root, value=(key == "beauty"))
            ttk.Checkbutton(mtx, variable=pv).grid(row=i + 1, column=1)
            ttk.Checkbutton(mtx, variable=mv).grid(row=i + 1, column=2)
            self.seq_out_vars[key] = (pv, mv)
            if key == "depth":
                depf = ttk.Frame(mtx)
                ttk.Label(depf, text="Near:").pack(side="left")
                self.seq_near_var = tk.StringVar(master=self.root, value="0")
                tk.Entry(depf, textvariable=self.seq_near_var, width=6).pack(side="left", padx=2)
                ttk.Label(depf, text="Far:").pack(side="left", padx=(6, 0))
                self.seq_far_var = tk.StringVar(master=self.root, value="10000")
                tk.Entry(depf, textvariable=self.seq_far_var, width=7).pack(side="left", padx=2)
                ttk.Label(depf, text="cm（手前=白/奥=黒）").pack(side="left")
                depf.grid(row=i + 1, column=3, sticky="w", padx=(12, 0))
            elif key == "normal":
                nrmf = ttk.Frame(mtx)
                ttk.Label(nrmf, text="空間:").pack(side="left")
                self.seq_normal_space_var = tk.StringVar(master=self.root, value="カメラ")
                ttk.Combobox(nrmf, textvariable=self.seq_normal_space_var,
                             state="readonly", width=7,
                             values=["カメラ", "ワールド"]).pack(side="left", padx=2)
                nrmf.grid(row=i + 1, column=3, sticky="w", padx=(12, 0))
        mtx.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
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
        self.seq_normal_space_var.set(self.normal_space_var.get())
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

    def _matte_paths_neutralize(self, paths):
        """マット対象をライティングから切り離す（影/AO/GIを落とさない・
        アンリット材で受光/受影もしない）。元マテリアルはタグに退避。
        雲(HVボリューム)は板ではないので分離しない（見た目そのまま・αレンダで扱う）。"""
        actors = core._resolve_target_actors(None, list(paths) or None)
        if not actors:
            return
        prims, vols = core.split_volumetric_targets(actors)
        if not prims:
            if vols:
                self.status_var.set(
                    "マット対象は雲(ボリューム)のみ: ライティング分離は不要（αレンダで処理）")
            return
        n = core.set_matte_shadow_occlusion(prims, False)
        m = core.set_matte_unlit(prims)
        self.status_var.set(
            "マット対象を無効化: 影/AO OFF %d comp・アンリット %d slot（リストから外すと復元）" % (n, m))

    def _matte_paths_restore(self, paths):
        actors = core._resolve_target_actors(None, list(paths) or None)
        if not actors:
            return
        prims, _vols = core.split_volumetric_targets(actors)
        if not prims:
            return
        n = core.set_matte_shadow_occlusion(prims, True)
        m = core.restore_matte_materials(prims)
        self.status_var.set("マット対象を復元: 影/AO ON %d comp・マテリアル復元 %d slot" % (n, m))

    def _on_mrq(self):
        """Movie Render Queue で Beauty を高品質レンダ（非同期・PIE）。"""
        import capture_mrq
        # reload はレンダ中に行うと実行中ジョブの executor/復元コールバックを
        # 破壊する（シーンが隠れたまま残る）。必ず busy チェックを先に行う。
        if (unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem).is_rendering()
                or capture_mrq._KEEP.get("executor") is not None):
            self.status_var.set("MRQ は既にレンダリング中です。完了までお待ちください。")
            return
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
        # シーケンサーを開いていれば MRQ 側も「その現在フレーム」で固定する
        # （PIE はエディタの評価ポーズを引き継がないため、SceneCapture 系=現在
        #   フレーム / MRQ 系=フレーム0 とズレる。カメラはツール指定のまま）。
        scene_seq = self._current_sequence()
        scene_frame = None
        if scene_seq is not None:
            try:
                scene_frame = int(
                    unreal.LevelSequenceEditorBlueprintLibrary.get_current_time())
            except Exception:
                scene_seq = None
                scene_frame = None
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

        matte_path = None
        want_mfront = self.mfront_var.get()
        want_behind = self.behind_var.get()
        want_rlfull = self.rlfull_var.get()
        want_rldir = self.rldir_var.get()
        skip_notes = []
        matte_mat_still = None
        if want_mfront:
            # Matteの前のマスクは Beauty と同一 MRQ ジョブの PP パスで撮る。
            # SceneCapture 別撮りだと WPO（風）で揺れる前景（木の葉等）の
            # シルエット位相が Beauty とズレる（2026-07-14 実測）。
            s.do_matte = False
            try:
                matte_mat_still = core.create_temp_matte_material()
            except Exception as e:
                skip_notes.append("Matteの前: 一時マテリアル生成失敗でスキップ")
                self.status_var.set("Matteの前: 一時マテリアル生成失敗: %s" % e)
        depth_pp_mat = None
        if self.depth_var.get() and self.depth_bit_var.get() == "8bit PNG":
            # 8bit深度は Beauty と同一 MRQ ジョブの PP パスで撮る（WPO/風の位相一致）。
            # 16bit PNG / EXR float は厳密リニアが必要なため従来の SceneCapture のまま
            # （風で揺れる前景は Beauty と位相が合わない可能性がある）。
            s.do_depth = False
            try:
                depth_pp_mat = core.create_temp_depth_material(
                    self._float_var(self.near_var, 0.0),
                    self._float_var(self.far_var, 10000.0), invert=True)
            except Exception as e:
                skip_notes.append("Z-Depth: 一時マテリアル生成失敗でスキップ")
                self.status_var.set("Z-Depth: 一時マテリアル生成失敗: %s" % e)
        normal_pp_mat = None
        if self.normal_var.get():
            # 法線も Beauty と同一 MRQ ジョブの PP パスで撮る（WPO/風の位相一致）
            try:
                normal_pp_mat = core.create_temp_normal_material(
                    camera_space=(self.normal_space_var.get() != "ワールド"))
            except Exception as e:
                skip_notes.append("Normal: 一時マテリアル生成失敗でスキップ")
                self.status_var.set("Normal: 一時マテリアル生成失敗: %s" % e)
        try:
            if s.do_matte or s.do_object_id or s.do_depth:
                self.status_var.set("同フレームの ObjectID/Depth を出力中…")
                self.root.update()
                core.run_capture(s)
        except Exception as e:
            self.status_var.set("データパス出力でエラー: %s" % e)

        # VDB雲: Auto=レベル内のHV検出で自動 / ON=強制 / OFF=雲を完全無視した従来書き出し
        vdb = self._vdb_enabled()

        # ObjectID 対象に HV ボリューム(雲)が含まれるか（SceneCapture 系には写らない
        # ため、雲は後段の per-cloud CloudMatte ジョブで色付けして合成する）
        objid_cloud_vols = []
        if s.do_object_id and vdb:
            _oa = core._resolve_target_actors(s.objid_actors, s.objid_actor_names)
            _op, objid_cloud_vols = core.split_volumetric_targets(_oa)
            if objid_cloud_vols:
                skip_notes.append("雲ObjectID: 分離モード（遮蔽なし全投影）")

        # Beauty の出力形式（実際に出せるものだけを提示している）
        img_fmt = {"PNG 8bit": "png", "JPG 8bit": "jpg",
                   "EXR 16bit (float)": "exr"}.get(self.beauty_fmt_var.get(), "png")
        beauty_path = os.path.join(out, _name("Beauty") + _FMT_EXT[img_fmt])
        # Matte 系合成は PIL で読める画像が必要。EXR のときは PNG も内部出力して使う
        # （Depth PP パスの PNG 出力にも必要）。
        need_comp = want_mfront or want_behind
        aux_png = (img_fmt == "exr") and (need_comp or depth_pp_mat is not None
                                          or normal_pp_mat is not None)
        comp_beauty = (os.path.join(out, _name("Beauty") + ".png")
                       if img_fmt == "exr" else beauty_path)

        # Beauty（MRQ）は Beauty 指定時・Matte 系合成・同一ジョブ深度・Raw Lighting Full
        # が要るときにレンダする（Raw Lighting Direct はメイン無しでも専用ジョブで出せる）
        beauty_needed = (self.beauty_var.get() or want_mfront or want_behind
                         or depth_pp_mat is not None or normal_pp_mat is not None
                         or want_rlfull)
        if (not beauty_needed and not want_rldir and not objid_cloud_vols
                and not self.cloudmatte_var.get() and not self.skymatte_var.get()):
            _restore_fb()
            self.status_var.set("完了（データパスのみ出力）" if (s.do_depth or s.do_object_id)
                                else "出力が選ばれていません")
            return

        # Matte 系出力時は Beauty から対象を常に隠す（クリーンプレート）。
        # 対象は 板(プリミティブ)/雲(HVボリューム) に分割: 板は従来の
        # ステンシルPPマスク、雲は専用 CloudMatte ジョブ（αレンダ）で扱う。
        beauty_hidden = None
        matte_prims, matte_vols = [], []
        if want_mfront or want_behind:
            matte_names = self._pick_targets_resolved(self.matte_pick)
            beauty_hidden = core._resolve_target_actors(None, matte_names or None)
            matte_prims, matte_vols = core.split_volumetric_targets(beauty_hidden)
            if matte_vols:
                # 雲(HV)は常に通常オブジェクト扱い: Beauty に写したまま、
                # αだけ CloudMatte ジョブで取得して Matteの前に統合する
                beauty_hidden = list(matte_prims)
            if beauty_hidden:
                self.status_var.set("Beauty: Matte 対象 %d 個を隠して撮影（クリーンプレート）"
                                    % len(beauty_hidden))
            else:
                self.status_var.set("Matte 対象が見つかりません（Beauty は全表示で撮ります）")
        if not vdb and matte_vols:
            # OFF: 雲は対象から外す（Beauty からも隠さない・合成は板のみで即確定）
            skip_notes.append("VDB雲モード OFF: 雲対象は無視されます")
            matte_vols = []
        if want_mfront and matte_vols and not matte_prims:
            # 板なし（雲のみ対象）のときだけ従来の分離モード（遮蔽なし）。UE5.7 は
            # holdout 方式がクラッシュ（単独cvar時）または空出力（ペア時）で使えない。
            skip_notes.append("雲マット: 分離モード（手前ジオメトリの遮蔽は反映されない）")
        if matte_mat_still is not None and not matte_prims:
            # 板対象なし → ステンシルPPマスクは不要（雲のみなら CloudMatte で作る）
            core.delete_temp_matte_material()
            matte_mat_still = None
            if not matte_vols:
                skip_notes.append("Matteの前: Matte 対象が見つからずスキップ")

        # 雲マット独立出力: 対象はレベル内の全 VDB雲。
        # Normal / Depth 選択時も雲αを自動レンダして雲領域を黒に落とす（雲は
        # GBuffer に法線/深度を持たず、放置すると雲の奥のジオメトリが写るため。
        # Depth の EXR float は生cmで 0=カメラ位置になるため対象外）。
        want_cloudmatte = self.cloudmatte_var.get()
        depth_black_ok = (self.depth_var.get()
                          and self.depth_bit_var.get() != "EXR float")
        need_cloud_black = (normal_pp_mat is not None) or depth_black_ok
        cloudmatte_vols = []
        if want_cloudmatte or need_cloud_black:
            if not vdb:
                if want_cloudmatte:
                    skip_notes.append("雲マット: VDB雲モード OFF のためスキップ")
                want_cloudmatte = False
                need_cloud_black = False
                depth_black_ok = False
            else:
                cloudmatte_vols = core.level_volumetrics()
                if not cloudmatte_vols:
                    if want_cloudmatte:
                        skip_notes.append("雲マット: レベルに VDB雲が見つからずスキップ")
                    want_cloudmatte = False
                    need_cloud_black = False
                    depth_black_ok = False
                elif (self.depth_var.get()
                      and self.depth_bit_var.get() == "EXR float"):
                    skip_notes.append("Depth(EXR float): 生cmのため雲抜き対象外")

        # 後続 MRQ ジョブのキュー（CloudMatte / Matteの奥のプレート / 直射 / 雲ID）
        jobs = []
        cloud_matte_path = None
        objid_cloud_entries = []
        # 雲ジョブの r.PostProcessing.PropagateAlpha=1 は MRQ 自身の
        # AlphaOutputOverride 復元と順序が衝突し、レンダ後も 1 が残る（実測）。
        # チェーン完了時に元値へ戻すため開始前の値を控える。
        pa0 = None
        if matte_vols or objid_cloud_vols or cloudmatte_vols:
            pa0 = unreal.SystemLibrary.get_console_variable_bool_value(
                "r.PostProcessing.PropagateAlpha")
        # 板あり: 白/黒バッキング差分で「板より手前の透過率T」を取り、板マスクの穴を
        # 遮蔽関係どおりに塞ぐ（板の手前の雲・半透明が他オブジェクトと同じ挙動になり、
        # 板の後ろの雲は板に遮られて写らないので自動的に無効）。何も隠さず照明も素のまま。
        use_backing = vdb and bool(matte_prims) and want_mfront
        backing = {}
        if use_backing:
            jobs.append(dict(base=_name("BackingW"), backing=matte_prims,
                             backing_white=True, fmt="exr", cloud_kind="backing_w"))
            if matte_vols:
                skip_notes.append("雲マット: 板の手前の雲は自動反映（雲の対象指定は不要）")
        # Matteの前用の雲αジョブ（分離モード）と、雲マット/Normal雲抜き用の
        # 可視雲ジョブ（白バッキング化＝深度順序どおり）。意味論が違うため共用しない。
        cloud_alpha_needed = want_cloudmatte or need_cloud_black
        geomask_mat = None
        if (not use_backing) and want_mfront and matte_vols and vdb:
            base = (_name("CloudMatteMF") if cloud_alpha_needed
                    else _name("CloudMatte"))
            jobs.append(dict(base=base, cloud=matte_vols, cloud_kind="matte"))
        if cloud_alpha_needed:
            try:
                geomask_mat = core.create_temp_geomask_material()
            except Exception as e:
                skip_notes.append("雲マット: GeoMask 生成失敗（空画素はα劣化）")
                self.status_var.set("GeoMask 生成失敗: %s" % e)
            # H5 知覚モデルの3レンダ: 白バッキング(T+α+GeoMask) / 黒バッキング素照明
            # (ベール輝度) / 雲なしBeauty(背景輝度)。合成は _finalize で行う。
            jobs.append(dict(base=_name("CloudMatte"), cloud=cloudmatte_vols,
                             cloud_kind="cloudvis", fmt="exr",
                             geomask=geomask_mat, cloud_backing="white"))
            jobs.append(dict(base=_name("CloudVeil"), cloud=cloudmatte_vols,
                             cloud_kind="cloudveil", fmt="exr",
                             cloud_backing="black"))
            # 雲なしベール: HV 隠し + ShowFlag.Cloud 0 + UDS フォグ板隠し。
            # V = CloudVeil − CloudVeil0 で UDS 雲レイヤー/板の雲も拾い、
            # 空グラデ・大気ヘイズを相殺する（ペア方式 2026-08-26）
            jobs.append(dict(base=_name("CloudVeil0"), cloud=cloudmatte_vols,
                             cloud_kind="cloudveil", fmt="exr",
                             cloud_backing="black",
                             hidden=list(cloudmatte_vols), sources_off=True))
            jobs.append(dict(base=_name("CloudBG"), hidden=list(cloudmatte_vols),
                             ts1=True))
        if self.skymatte_var.get():
            # 空マット: 空=黒・大気光/雲維持の追加レンダ（Beauty と同形式）
            jobs.append(dict(base=_name("SkyMatte"), fmt=img_fmt, sky=True))
        if want_behind:
            mt = core._resolve_target_actors(None, self._pick_targets_resolved(self.matte_pick) or None)
            mt_p, mt_v = core.split_volumetric_targets(mt)
            if mt_p:
                cs_cam = core.get_camera_settings(cam)
                nc = core.matte_near_clip_cm(mt_p, cs_cam)
                # HV(雲)は near-clip の描画クリップを無視して写り込むため、
                # 板より手前の雲はアクター単位で隠す（板の後ろの雲は窓の中身として残す）
                front_vols = core.volumetrics_nearer_than(nc, cs_cam) if vdb else []
                # 凍結静止画のプレートは TS=1 固定（TS>1 は無意味なうえ、クリップの
                # サブフレーム片効きで手前が半透明ゴースト化する素地になる）
                jobs.append(dict(hidden=list(mt_p) + front_vols,
                                 base=_name("BehindPlate"), ts1=True,
                                 near_clip=nc, composite=True, matte=mt_p))
                if mt_v:
                    skip_notes.append("Matteの奥: 雲対象はシルエット非対応（板のみで合成）")
            elif mt_v:
                skip_notes.append("Matteの奥: 雲(ボリューム)のみの対象は未対応のためスキップ")
            else:
                self.status_var.set("Matteの奥: Matte 対象が見つかりません")
        if want_rldir:
            # 直射のみは ShowFlag がジョブ全体（Beauty パス）を汚すため専用ジョブ。
            # Matte 対象の非表示はメインの Beauty と同条件に揃える。
            jobs.append(dict(base=_name("RawLightingDirect"), fmt=img_fmt,
                             hidden=beauty_hidden, raw_light=True))
        for _i, _va in enumerate(objid_cloud_vols):
            jobs.append(dict(base=_name("ObjIDCloud") + "_%d" % _i, cloud=[_va],
                             cloud_kind="objid", cloud_label=_va.get_actor_label()))

        def _compose_mfront():
            """Matteの前: 板マスク（同一ジョブPPパス）と雲α（CloudMatte）を統合して
            MatteBeauty を作る（どちらか一方だけでも可）。"""
            nonlocal matte_path
            mp = os.path.join(out, _name("Beauty") + "_Matte.png")
            mesh_mp = mp if os.path.isfile(mp) else None
            merged = mesh_mp
            if backing.get("t"):
                m2 = core.merge_backing_t_into_matte(
                    mesh_mp, backing["t"],
                    os.path.join(out, _name("Beauty") + "_MatteMerged.png"))
                merged = m2 or mesh_mp
            elif cloud_matte_path:
                m2 = core.merge_cloud_alpha_into_matte(
                    mesh_mp, cloud_matte_path,
                    os.path.join(out, _name("Beauty") + "_MatteMerged.png"))
                merged = m2 or mesh_mp
            if merged:
                matte_path = merged
                core.blend_with_beauty(
                    comp_beauty, merged, None,
                    matte_out=os.path.join(out, _name("MatteBeauty") + ".png"))
            else:
                skip_notes.append("Matteの前: マスクが得られずスキップ（Beauty は残置）")

        def _finalize():
            # 雲 ObjectID を合成してから内部素材を消す
            if objid_cloud_entries:
                try:
                    core.merge_cloud_objid(
                        os.path.join(out, _name("ObjectID") + ".png"),
                        os.path.join(out, _name("ObjectID") + ".json"),
                        objid_cloud_entries)
                except Exception as e:
                    self.status_var.set("雲ObjectID 合成エラー: %s" % e)
            # 内部素材の後始末: 生 Matte マスクと EXR 用の内部 PNG は削除。
            # Beauty のチェックが無い場合（合成のためだけにレンダした場合）も削除するが、
            # 合成がスキップされたときは唯一の成果物になるため残す。
            aux = comp_beauty if comp_beauty != beauty_path else None
            keep_beauty = self.beauty_var.get() or bool(skip_notes)
            # H5 合成: 白T + 黒ベール + 雲なしBeauty から可視雲αを作る
            cm_png = os.path.join(out, _name("CloudMatte") + ".png")
            if cloud_alpha_needed:
                gexr = os.path.join(out, _name("CloudMatte") + "_GeoMask.exr")
                v0_exr = os.path.join(out, _name("CloudVeil0") + ".exr")
                r = core.compose_visible_cloud_h5(
                    os.path.join(out, _name("CloudMatte") + ".exr"),
                    gexr if os.path.isfile(gexr) else None,
                    os.path.join(out, _name("CloudVeil") + ".exr"),
                    os.path.join(out, _name("CloudBG") + ".png"),
                    cm_png,
                    ffmpeg=core.find_ffmpeg(getattr(self, "_ffmpeg_hint", None)),
                    veil_none_exr=v0_exr if os.path.isfile(v0_exr) else None)
                if not r:
                    skip_notes.append("雲マット: H5合成失敗")
            # Normal / Depth の雲領域を黒に落とす（α のままの CloudMatte を乗算。
            # 白黒変換前に行う）。乗算前の素材は _cloudsrc/ に退避して再合成可能に
            cloud_applied = os.path.isfile(cm_png)
            if os.path.isfile(cm_png):
                targets = []
                if normal_pp_mat is not None:
                    targets.append(("Normal", os.path.join(out, _name("Normal") + ".png")))
                if depth_black_ok:
                    targets.append(("Depth", os.path.join(out, _name("Depth") + ".png")))
                for _lbl, _p in targets:
                    if not os.path.isfile(_p):
                        continue
                    try:
                        srcdir = os.path.join(out, "_cloudsrc")
                        os.makedirs(srcdir, exist_ok=True)
                        import shutil
                        shutil.copy2(_p, os.path.join(srcdir, os.path.basename(_p)))
                    except Exception:
                        pass
                    try:
                        core.apply_cloud_black(_p, cm_png)
                    except Exception as e:
                        skip_notes.append("%s: 雲抜き失敗" % _lbl)
                        self.status_var.set("%s 雲抜き失敗: %s" % (_lbl, e))
            # 雲マット独立出力: α のまま残っている CloudMatte を白黒マスク（雲=黒）へ
            # 変換して残す
            if want_cloudmatte and os.path.isfile(cm_png):
                try:
                    core.cloudmatte_alpha_to_mask(cm_png)
                except Exception as e:
                    skip_notes.append("雲マット: 白黒変換失敗")
                    self.status_var.set("雲マット白黒変換失敗: %s" % e)
            matte_exr = os.path.join(out, _name("Beauty") + "_Matte.exr")
            depth_exr = os.path.join(out, _name("Beauty") + "_Depth.exr")
            normal_exr = os.path.join(out, _name("Beauty") + "_Normal.exr")
            removals = [(matte_path, False),
                        (aux, False),
                        (matte_exr, False),
                        (depth_exr, False),
                        (normal_exr, False),
                        (os.path.join(out, _name("Beauty") + "_Matte.png"), False),
                        (cm_png, want_cloudmatte),
                        (os.path.join(out, _name("CloudMatteMF") + ".png"), False),
                        (os.path.join(out, _name("BackingW") + ".exr"), False),
                        (os.path.join(out, _name("Beauty") + "_BackingT.png"), False),
                        (beauty_path, keep_beauty)]
            if geomask_mat is not None:
                core.delete_temp_geomask_material()
            removals += [(cp, False) for _lbl, cp in objid_cloud_entries]
            for p, keep in removals:
                if p and not keep and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            # H5 の合成ソースは削除せず _cloudsrc/ へ退避（EXR+雲なしBeauty が
            # 残っていれば、再レンダなしで濃度パラメータを変えて再合成できる）
            if cloud_alpha_needed:
                srcdir = os.path.join(out, "_cloudsrc")
                try:
                    os.makedirs(srcdir, exist_ok=True)
                except Exception:
                    pass
                for _sf in (_name("CloudMatte") + ".exr",
                            _name("CloudMatte") + "_GeoMask.exr",
                            _name("CloudVeil") + ".exr",
                            _name("CloudVeil0") + ".exr",
                            _name("CloudBG") + ".png"):
                    _p = os.path.join(out, _sf)
                    if os.path.isfile(_p):
                        try:
                            os.replace(_p, os.path.join(srcdir, _sf))
                        except Exception:
                            pass
            # 雲抜き/マットは濃度設定依存のため、設定タグをファイル名へ付与
            # （例 ..._CloudMatte_019_H5d1.5.png。テイク番号検出 _(\d{3})(?=[._]) は
            # 後置サフィックスでも壊れない）
            if cloud_applied:
                gtag = core.vis_cloud_gain_tag()
                gt_names = []
                if want_cloudmatte:
                    gt_names.append("CloudMatte")
                if normal_pp_mat is not None:
                    gt_names.append("Normal")
                if depth_black_ok:
                    gt_names.append("Depth")
                for _bn in gt_names:
                    _src = os.path.join(out, _name(_bn) + ".png")
                    if os.path.isfile(_src):
                        try:
                            os.replace(_src, os.path.join(
                                out, "%s_%s.png" % (_name(_bn), gtag)))
                        except Exception:
                            skip_notes.append("%s: Gタグ付与失敗" % _bn)
            _restore_fb()
            if pa0 is not None:
                unreal.SystemLibrary.execute_console_command(
                    None, "r.PostProcessing.PropagateAlpha %d" % (1 if pa0 else 0))
            self.status_var.set("完了" if not skip_notes
                                else "完了（%s）" % " / ".join(skip_notes))

        def _run_jobs():
            if not jobs:
                _finalize()
                return
            j = jobs.pop(0)

            def _jdone(ok, od, _j=j):
                nonlocal cloud_matte_path
                # バッキングジョブ: 白1レンダから T を計算して合成へ
                if _j.get("cloud_kind") == "backing_w":
                    bp = os.path.join(out, _j["base"] + ".exr")
                    if not ok or not os.path.isfile(bp):
                        skip_notes.append("雲マット: バッキングレンダ失敗 (%s)" % _j["base"])
                    else:
                        t_png, _sc = core.backing_t_png(
                            bp, os.path.join(out, _name("Beauty") + "_BackingT.png"),
                            ffmpeg=core.find_ffmpeg(getattr(self, "_ffmpeg_hint", None)))
                        if t_png:
                            backing["t"] = t_png
                        else:
                            skip_notes.append("雲マット: バッキングTの計算に失敗")
                    if want_mfront:
                        # バッキングが失敗しても板のみで MatteBeauty を必ず確定させる
                        # （backing["t"] が無ければ _compose_mfront は板マスクのみで合成）
                        try:
                            _compose_mfront()
                        except Exception as e:
                            self.status_var.set("雲マット合成エラー: %s" % e)
                # CloudMatte / 雲ObjectID ジョブ: αを回収して合成へ
                elif _j.get("cloud_kind") in ("cloudvis", "cloudveil"):
                    # H5 の入力レンダ（白T/黒ベール）。合成は _finalize で行う
                    if not ok or not os.path.isfile(
                            os.path.join(out, _j["base"] + ".exr")):
                        skip_notes.append("雲マット: 入力レンダ失敗 (%s)" % _j["base"])
                elif _j.get("cloud_kind"):
                    cp = os.path.join(out, _j["base"] + ".png")
                    if not ok or not os.path.isfile(cp):
                        skip_notes.append("%s: レンダ失敗" %
                                          ("雲マット" if _j["cloud_kind"] == "matte"
                                           else "雲ObjectID(%s)" % _j.get("cloud_label", "?")))
                    elif _j["cloud_kind"] == "matte":
                        cloud_matte_path = cp
                        if want_mfront:
                            try:
                                _compose_mfront()
                            except Exception as e:
                                self.status_var.set("雲マット合成エラー: %s" % e)
                    else:
                        objid_cloud_entries.append(
                            (_j.get("cloud_label", _j["base"]), cp))
                # Raw Lighting Direct ジョブ: LightingOnly を最終名へ
                # （<base>_LightingOnly.* → <base>.*。os.replace は既存を上書き）
                if _j.get("raw_light"):
                    if not ok:
                        skip_notes.append("Raw Lighting Direct: レンダ失敗")
                    else:
                        ext = _FMT_EXT[_j.get("fmt", "png")]
                        lp = os.path.join(out, _j["base"] + "_LightingOnly" + ext)
                        try:
                            if os.path.isfile(lp):
                                os.replace(lp, os.path.join(out, _j["base"] + ext))
                            else:
                                skip_notes.append("Raw Lighting Direct: 出力が見つかりません")
                        except Exception as e:
                            skip_notes.append("Raw Lighting Direct: 後処理エラー")
                            self.status_var.set("Raw Lighting Direct 後処理エラー: %s" % e)
                # behind ジョブはマットシルエットで通常 Beauty と合成し behindmatte.png を作る
                if ok and _j.get("composite"):
                    try:
                        c = self._current_camera()
                        inter = os.path.join(out, _j["base"] + ".png")
                        core.composite_behind_in_matte(
                            core._get_editor_world(), core.get_camera_settings(c),
                            _j["matte"], comp_beauty, inter,
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
                capture_mrq.render_beauty(cam, out, W, H,
                                          image_format=j.get("fmt", "png"),
                                          # CloudMatte 系はライティング非依存のαのみ。
                                          # TS>1 だと空のサブフレームが平均されて
                                          # αが 1/TS に希釈される（TS=2 で最大128 実測）
                                          temporal_samples=(1 if (j.get("cloud_kind")
                                                                  or j.get("ts1"))
                                                            else ts),
                                          # 雲ヘルパー系は露出固定・適応なしなので
                                          # ウォームアップ 8 で足りる（固定費削減）
                                          warmup=(min(warm, 8)
                                                  if (j.get("cloud_kind")
                                                      or j.get("ts1"))
                                                  else warm),
                                          file_basename=j["base"],
                                          hidden_actors=j.get("hidden"),
                                          near_clip_cm=j.get("near_clip"),
                                          light_pass=j.get("raw_light", False),
                                          light_direct=j.get("raw_light", False),
                                          cloud_matte_actors=j.get("cloud"),
                                          cloud_visible=(j.get("cloud_kind")
                                                         in ("cloudvis",
                                                             "cloudveil")),
                                          cloud_backing=j.get("cloud_backing"),
                                          cloud_sources_off=j.get("sources_off",
                                                                  False),
                                          geomask_material=j.get("geomask"),
                                          sky_matte=j.get("sky", False),
                                          backing_actors=j.get("backing"),
                                          # 空マットはフォグ散乱が空を埋めるため
                                          # fog_off 強制（実績スクリプトと同条件）
                                          backing_white=j.get("backing_white", False),
                                          fog_off=(True if j.get("sky")
                                                   else self.fog_off_var.get()),
                                          scene_sequence=scene_seq,
                                          scene_frame=scene_frame, on_done=_jdone)
            except Exception as e:
                # チェーンを死なせず _finalize まで確定させる（中間物の掃除・
                # filmback/PropagateAlpha の復元・保留中の合成の後始末）
                skip_notes.append("追加ジョブ起動失敗 (%s)" % j["base"])
                self.status_var.set("追加 MRQ 失敗: %s" % e)
                _finalize()

        def _after_beauty(ok, od):
            nonlocal matte_path
            if matte_mat_still is not None:
                core.delete_temp_matte_material()
            if depth_pp_mat is not None:
                core.delete_temp_depth_material()
            if normal_pp_mat is not None:
                core.delete_temp_normal_material()
            if ok:
                try:
                    if want_mfront and (matte_mat_still is not None or matte_vols):
                        if not matte_vols and not use_backing:
                            _compose_mfront()   # 板のみ: ここで確定
                        # 雲対象/バッキングあり: 後続ジョブ完了時（_jdone）に合成する
                    if depth_pp_mat is not None:
                        dp = os.path.join(out, _name("Beauty") + "_Depth.png")
                        if os.path.isfile(dp):
                            os.replace(dp, os.path.join(out, _name("Depth") + ".png"))
                        else:
                            skip_notes.append("Z-Depth: PP パス出力が見つかりません")
                    if normal_pp_mat is not None:
                        npf = os.path.join(out, _name("Beauty") + "_Normal.png")
                        if os.path.isfile(npf):
                            os.replace(npf, os.path.join(out, _name("Normal") + ".png"))
                        else:
                            skip_notes.append("Normal: PP パス出力が見つかりません")
                    if want_rlfull:
                        # LightingOnly パスを最終名へ（<base>_LightingOnly.* → RawLightingFull）
                        ext = _FMT_EXT[img_fmt]
                        lp = os.path.join(out, _name("Beauty") + "_LightingOnly" + ext)
                        if os.path.isfile(lp):
                            os.replace(lp, os.path.join(
                                out, _name("RawLightingFull") + ext))
                            # EXR + 内部 PNG 併用時は LightingOnly の PNG 片割れも出る
                            twin = os.path.join(out, _name("Beauty") + "_LightingOnly.png")
                            if ext != ".png" and os.path.isfile(twin):
                                os.remove(twin)
                        else:
                            skip_notes.append("Raw Lighting Full: 出力が見つかりません")
                except Exception as e:
                    _restore_fb()
                    self.status_var.set("Beautyブレンドでエラー: %s" % e)
                    return
                _run_jobs()
                return
            _restore_fb()
            self.status_var.set("MRQ 失敗: " + od)

        if not beauty_needed:
            # Raw Lighting Direct のみ: メインジョブ無しで専用ジョブだけ回す
            _run_jobs()
            return

        # 静止画（時間凍結）の TS>1 は、同一ジョブの Matte/Depth PP パスが
        # サブフレームの片方にしか写らず 50% に希釈される（TS=2 でマスク黒が
        # sRGB(0.5)=187 になる実測）。凍結静止画はサブフレーム内容が同一で
        # TS>1 に画質上の意味も無いため、PP パスがあるときは TS=1 に強制する。
        ts_main = ts
        if ts > 1 and (matte_mat_still is not None or depth_pp_mat is not None
                       or normal_pp_mat is not None):
            ts_main = 1
            skip_notes.append("Matte/Depth/Normal 同一ジョブパスのため TS=1 で実行")

        self.status_var.set("MRQ Beauty レンダ中… (PIEに入ります / 完了まで待機)")
        self.root.update()
        try:
            capture_mrq.render_beauty(cam, out, W, H, image_format=img_fmt,
                                      also_png=aux_png,
                                      light_pass=want_rlfull,
                                      temporal_samples=ts_main, warmup=warm,
                                      file_basename=_name("Beauty"),
                                      hidden_actors=(None if matte_mat_still is not None
                                                     else beauty_hidden),
                                      matte_material=matte_mat_still,
                                      matte_actors=(matte_prims if matte_mat_still is not None
                                                    else None),
                                      depth_material=depth_pp_mat,
                                      normal_material=normal_pp_mat,
                                      fog_off=self.fog_off_var.get(),
                                      scene_sequence=scene_seq,
                                      scene_frame=scene_frame, on_done=_after_beauty)
        except Exception as e:
            if matte_mat_still is not None:
                core.delete_temp_matte_material()
            if depth_pp_mat is not None:
                core.delete_temp_depth_material()
            if normal_pp_mat is not None:
                core.delete_temp_normal_material()
            if geomask_mat is not None:
                core.delete_temp_geomask_material()
            _restore_fb()
            self.status_var.set("MRQ 起動失敗: %s" % e)

    # ------------------------------------------------------ sequence render
    def _current_sequence(self):
        """Sequencer で現在開いている LevelSequence（無ければ None）。"""
        try:
            return unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()
        except Exception:
            return None

    def _vdb_enabled(self):
        """VDB雲セレクタの実効値。Auto はレベル内の HV(VDB雲) 検出で決める。"""
        sel = (self.vdb_var.get() or "Auto").lower()
        if sel == "on":
            return True
        if sel == "off":
            return False
        return core.level_has_volumetrics()

    def _sequence_camera_at(self, seq, frame):
        """プレイヘッドを frame に合わせ、カメラカットに束縛されたカメラアクターを返す。
        バインディング解決に失敗した場合はレベル内の先頭カメラにフォールバック。"""
        try:
            unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(int(frame))
        except Exception:
            pass
        try:
            import capture_mrq
            cams = capture_mrq._camera_cut_camera_actors(seq, core._get_editor_world())
            if cams:
                return cams[0]
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
        self._sync_seq_h()

    def _seq_camera_aspect(self):
        """現在のシーケンスのカメラカットに束縛されたカメラのアスペクト比。
        解決できなければ 0.0（Cine は filmback 由来、素の CameraActor は
        aspect_ratio プロパティ。どちらも aspect_ratio に反映される）。"""
        seq = self._current_sequence()
        if seq is None:
            return 0.0
        try:
            import capture_mrq
            cams = capture_mrq._camera_cut_camera_actors(seq, core._get_editor_world())
            if not cams:
                # スポーナブルはカメラカットから解決できないので、ワールド上の
                # カメラ（list_cameras 側でスポーナブルも拾っている）で代替する。
                cams = core.list_cameras()
            if cams:
                return core.get_camera_settings(cams[0]).get("aspect_ratio", 0.0)
        except Exception:
            pass
        return 0.0

    def _sync_seq_h(self, *a):
        """「カメラのアスペクト」ON のとき、シーケンスのカメラの aspect から
        高さ = 幅 / aspect を自動算出して反映する（高さ入力は無効化）。"""
        try:
            on = bool(self.seq_camasp_var.get())
            self.seq_h_entry.config(state=("disabled" if on else "normal"))
            if not on:
                return
            asp = self._seq_camera_aspect()
            if asp <= 0.1:
                return
            w = self._int_var(self.seq_w_var, 0)
            if w > 0:
                h = str(int(round(w / asp)))
                if self.seq_h_var.get() != h:
                    self.seq_h_var.set(h)
        except Exception:
            pass

    def _on_seq_render(self):
        """Sequencer で開いている LevelSequence をレンダ（非同期・PIE）。
        MRQ は PNG 連番（マスター）のみを出力し、MP4 はシーケンスの Display Rate で
        ffmpeg エンコードする（fps を確実に一致させるため）。余剰フレームは
        エンコード前にトリムする。素材ごとに PNG連番/MP4 を選択できる。"""
        import capture_mrq
        # _on_mrq と同じ理由で reload 前に busy チェック（実行中チェーンの保護）
        if (unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem).is_rendering()
                or capture_mrq._KEEP.get("executor") is not None):
            self.status_var.set("MRQ は既にレンダリング中です。完了までお待ちください。")
            return
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
        if self.seq_camasp_var.get():
            # カメラカットのカメラのアスペクトに従う（素の CameraActor も可）
            asp = self._seq_camera_aspect()
            H = int(round(W / asp)) if asp > 0.1 else self._int_var(self.seq_h_var, 1080)
        else:
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
        normal_needed = _need("normal")
        objid_needed = _need("objid")
        rlfull_needed = _need("rlfull")
        rldir_needed = _need("rldir")
        cloudmatte_needed = _need("cloudmatte")
        skymatte_needed = _need("skymatte")
        # Direct だけならメインジョブ自体を直射レンダにする（全編2回レンダの回避）
        only_direct = rldir_needed and not (
            _need("beauty") or depth_needed or normal_needed or matte_needed
            or objid_needed or rlfull_needed)

        seq_notes = []
        vdb = self._vdb_enabled()
        matte_actors = None
        matte_prims, matte_vols = [], []
        if matte_needed or self.seq_matte_hide_var.get():
            matte_actors = core._resolve_target_actors(
                None, self._pick_targets_resolved(self.matte_pick) or None)
            if not matte_actors:
                self.status_var.set("シーケンスレンダ: Matte 対象が見つかりません"
                                    "（画像タブの Matte targets か選択を確認）")
                return
            # 板(プリミティブ)/雲(HVボリューム) 分割。雲は専用 CloudMatte ジョブで扱う。
            matte_prims, matte_vols = core.split_volumetric_targets(matte_actors)
            if not vdb and matte_vols:
                # OFF: 雲は対象から外す（従来の書き出しと同一の流れにする）
                seq_notes.append("VDB雲モード OFF: 雲対象は無視されます")
                matte_vols = []
            if matte_vols and _need("mfront") and not matte_prims:
                seq_notes.append("雲マット: 分離モード（手前ジオメトリの遮蔽は反映されない）")
        objid_actors = None
        if objid_needed:
            objid_actors = core._resolve_target_actors(
                None, self._pick_targets_resolved(self.objid_pick) or None)
            if not objid_actors:
                self.status_var.set("シーケンスレンダ: ObjectID 対象が見つかりません"
                                    "（画像タブの Object ID targets か選択を確認）")
                return
            _op, _ov = core.split_volumetric_targets(objid_actors)
            if _ov:
                seq_notes.append("ObjectID: 雲(ボリューム)対象は映像では未対応（静止画のみ）")
                objid_actors = _op
                if not objid_actors:
                    wants["objid"] = (False, False)
                    objid_needed = False

        take_str = "%03d" % core.next_take_number(base_out)
        parts = []
        if self.seq_usecustom_var.get():
            c = self.seq_custom_var.get().strip()
            if c:
                parts.append(core._safe_name(c))
        parts.append(core._safe_name(seq.get_name()))
        name_body = "_".join(parts)
        final_out = base_out
        if self.seq_subdir_var.get():
            final_out = os.path.join(base_out, "%s_%s" % (name_body, take_str))
        # 中間ファイル（内部素材・EXR・トリム前連番）は作業フォルダに集約し、
        # 完了時に成果物だけを final_out へ移して作業フォルダごと削除する
        out = os.path.join(final_out, "_work_%s" % take_str)
        try:
            os.makedirs(out, exist_ok=True)
        except Exception:
            pass
        self._save_ui_state()

        depth_mat = matte_mat = matte_sil_mat = objid_mat = normal_mat = None
        hide_actors = None
        try:
            if depth_needed:
                depth_mat = core.create_temp_depth_material(
                    self._float_var(self.seq_near_var, 0.0),
                    self._float_var(self.seq_far_var, 10000.0),
                    invert=True)   # 手前=白 / 奥=黒 固定
            if normal_needed:
                normal_mat = core.create_temp_normal_material(
                    camera_space=(self.seq_normal_space_var.get() != "ワールド"))
            if _need("mfront") and matte_prims:
                matte_mat = core.create_temp_matte_material()
            if _need("behind"):
                if matte_prims:
                    # シルエットはレンダ後にスクラブ+show-only深度で生成する
                    # （静止画側と同一手法。メインジョブの MatteSil PP パスは
                    # ObjectID ステンシルに穴を開けられるため廃止）
                    if matte_vols:
                        seq_notes.append("Matteの奥: 雲対象はシルエット非対応（板のみで合成）")
                else:
                    seq_notes.append("Matteの奥: 板(プリミティブ)対象が無いためスキップ")
                    wants["behind"] = (False, False)
            if not matte_needed and self.seq_matte_hide_var.get():
                hide_actors = matte_actors
            if objid_needed:
                objid_mat = core.create_temp_objid_material()
        except Exception as e:
            self.status_var.set("一時マテリアル生成失敗: %s" % e)
            return

        def _cleanup_materials():
            if depth_mat is not None:
                core.delete_temp_depth_material()
            if normal_mat is not None:
                core.delete_temp_normal_material()
            if matte_mat is not None:
                core.delete_temp_matte_material()
            if matte_sil_mat is not None:
                core.delete_temp_matte_sil_material()
            if objid_mat is not None:
                core.delete_temp_objid_material()

        def _final(ok, od):
            _cleanup_materials()
            if pa0 is not None:
                # 雲ジョブが残す r.PostProcessing.PropagateAlpha=1 を元値へ（実測リーク）
                unreal.SystemLibrary.execute_console_command(
                    None, "r.PostProcessing.PropagateAlpha %d" % (1 if pa0 else 0))
            # 成功時: 作業フォルダに残った成果物（素材サブフォルダ/MP4/JSON）を
            # final_out へ移し、作業フォルダごと削除。失敗時は診断用に残す
            if ok:
                try:
                    for f in os.listdir(out):
                        os.replace(os.path.join(out, f),
                                   os.path.join(final_out, f))
                    os.rmdir(out)
                except Exception as e:
                    seq_notes.append("作業フォルダの後片付けに失敗（%s に残置）"
                                     % os.path.basename(out))
                    unreal.log_warning("[SceneCapture] 作業フォルダ移動失敗: %s" % e)
            else:
                seq_notes.append("中間ファイルは %s に残置（診断用）"
                                 % os.path.basename(out))
            msg = (("シーケンスレンダ完了: %s" % final_out) if ok
                   else "シーケンスレンダ失敗 (Output Log 参照)")
            if seq_notes:
                msg += "（%s）" % " / ".join(seq_notes)
            self.status_var.set(msg)

        # 雲マット: 板あり＝バッキング差分（何も隠さない・遮蔽正確）/
        # 板なし（雲のみ対象）＝従来の分離モード（CloudMatte ジョブ）
        backing_seq = {"run": vdb and bool(matte_prims) and _need("mfront")}
        mfront_cloud = (vdb and bool(matte_vols) and not matte_prims
                        and _need("mfront"))
        # 雲マット独立出力: 対象はレベル内の全VDB雲。Matteの前の雲ジョブと重なる場合は
        # 1本で共用（対象は Matte 対象の雲のまま＝Matteの前の合成の意味を変えない）。
        # Normal / Depth 選択時も雲αを自動レンダして連番の雲領域を黒に落とす。
        cm_vols = []
        cloud_for_black = False
        if cloudmatte_needed or normal_needed or depth_needed:
            if not vdb:
                if cloudmatte_needed:
                    seq_notes.append("雲マット: VDB雲モード OFF のためスキップ")
                    wants["cloudmatte"] = (False, False)
                    cloudmatte_needed = False
            else:
                cm_vols = core.level_volumetrics()
                if not cm_vols:
                    if cloudmatte_needed:
                        seq_notes.append("雲マット: レベルに VDB雲が見つからずスキップ")
                        wants["cloudmatte"] = (False, False)
                        cloudmatte_needed = False
                else:
                    cloud_for_black = normal_needed or depth_needed
        cloud_vols_job = matte_vols if mfront_cloud else cm_vols
        if ((cloudmatte_needed or cloud_for_black) and mfront_cloud
                and {a.get_path_name() for a in matte_vols}
                != {a.get_path_name() for a in cm_vols}):
            seq_notes.append("雲マット: Matte対象の雲のみ（Matteの前と共用）")
        cloud_seq = {"run": mfront_cloud or cloudmatte_needed or cloud_for_black,
                     "mfront": mfront_cloud}
        pa0 = (unreal.SystemLibrary.get_console_variable_bool_value(
            "r.PostProcessing.PropagateAlpha") if cloud_seq["run"] else None)
        if backing_seq["run"]:
            seq_notes.append("雲マット: バッキング差分（板の手前の雲を遮蔽どおり反映）")
        pass_files_main = [] if only_direct else ["Beauty"]
        if depth_needed:
            pass_files_main.append("Depth")
        if normal_needed:
            pass_files_main.append("Normal")
        if matte_mat is not None:
            pass_files_main.append("Matte")
        if matte_sil_mat is not None:
            pass_files_main.append("MatteSil")
        if objid_needed:
            pass_files_main.append("ObjectID")
        if rlfull_needed:
            pass_files_main.append("RawLightingFull")
        if only_direct:
            pass_files_main.append("RawLightingDirect")

        def _finish_outputs(ok, od):
            """トリム → 合成 → マニフェスト → MP4 エンコード → 不要 PNG 削除。"""
            if not ok:
                _final(False, od)
                return
            try:
                trim_list = list(pass_files_main)
                if skymatte_needed:
                    trim_list.append("SkyMatte")
                if _need("behind"):
                    trim_list.append("BehindPlate")
                if rldir_needed and not only_direct:
                    trim_list.append("RawLightingDirect")
                if cloud_seq["run"]:
                    trim_list.append("CloudMatte")
                core.trim_sequence_frames(out, name_body, take_str,
                                          trim_list, cs_eff, ce_eff)
                if backing_seq["run"]:
                    nb = core.backing_t_sequence(
                        out, name_body, take_str, cs_eff, ce_eff,
                        core.find_ffmpeg(getattr(self, "_ffmpeg_hint", None)))
                    backing_seq["run"] = nb > 0
                    if nb <= 0:
                        seq_notes.append("雲マット: バッキングTの計算に失敗")
                if _need("mfront"):
                    core.composite_mattefront_sequence(out, name_body, take_str,
                                                       use_cloud=(cloud_seq["run"]
                                                                  and cloud_seq["mfront"]),
                                                       use_backing=backing_seq["run"])
                if _need("behind"):
                    ns = core.render_matte_sil_sequence(
                        out, name_body, take_str, cs_eff, ce_eff, matte_prims, W, H,
                        camera_for_frame=lambda f: self._sequence_camera_at(seq, f))
                    if ns > 0:
                        core.composite_behind_sequence(out, name_body, take_str)
                    else:
                        seq_notes.append("Matteの奥: シルエット生成に失敗")
                if cloud_for_black and cloud_seq["run"]:
                    # Normal / Depth 連番の雲領域を黒に（CloudMatte が α のままのうちに乗算）
                    if normal_needed:
                        core.apply_cloud_black_to_pass_frames(
                            out, name_body, take_str, "Normal")
                    if depth_needed:
                        core.apply_cloud_black_to_pass_frames(
                            out, name_body, take_str, "Depth")
                if cloudmatte_needed and cloud_seq["run"]:
                    # 雲マット独立出力: α連番を白黒マスク連番（雲=黒）へ変換
                    # （MP4 エンコード可能に）。Matteの前の合成が α を使い終わってから。
                    if core.cloudmatte_frames_to_mask(out, name_body, take_str) <= 0:
                        seq_notes.append("雲マット: 白黒変換で対象フレームなし")
                if objid_needed and objid_actors:
                    man = {}
                    # ステンシルは 1..MATTE_STENCIL-1（MATTE_STENCIL はマット用予約）
                    for i, a in enumerate(objid_actors[:core.MATTE_STENCIL - 1]):
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
                drop = ["BehindPlate", "BackingT"]   # 中間素材は削除
                # CloudMatte は _SEQ_OUTPUTS 行になったので下の汎用ループが
                # 「PNG連番のチェックが無いときだけ」削除する
                if matte_mat is not None:
                    drop.append("Matte")
                if _need("behind"):
                    drop.append("MatteSil")
                if rldir_needed:
                    drop.append("DirectPlate")       # 直射ジョブの Beauty（内部素材）
                for key, _label, pass_name in _SEQ_OUTPUTS:
                    if not wants[key][0]:
                        drop.append(pass_name)
                core.delete_pass_frames(out, name_body, take_str, drop)
                # PNG連番を選んだ素材は素材毎のサブフォルダへ自動移動（MP4は直下のまま）
                png_passes = [pass_name for key, _label, pass_name in _SEQ_OUTPUTS
                              if wants[key][0]]
                if png_passes:
                    core.move_pass_frames_to_subdirs(out, name_body, take_str, png_passes)
                # 濃度タグ付与（静止画と同じ _H5d1.5。雲抜き/マットは濃度設定依存の
                # ため。テイク番号検出 _(\d{3})(?=[._]) は後置サフィックスでも壊れない）
                if cloud_seq["run"] and not cloud_seq["mfront"]:
                    gtag = core.vis_cloud_gain_tag()
                    tag_passes = []
                    if cloudmatte_needed:
                        tag_passes.append("CloudMatte")
                    if cloud_for_black:
                        if normal_needed:
                            tag_passes.append("Normal")
                        if depth_needed:
                            tag_passes.append("Depth")
                    for pn in tag_passes:
                        mp4 = os.path.join(out, "%s_%s_%s.mp4"
                                           % (name_body, pn, take_str))
                        if os.path.isfile(mp4):
                            try:
                                os.replace(mp4, os.path.join(
                                    out, "%s_%s_%s_%s.mp4"
                                    % (name_body, pn, take_str, gtag)))
                            except Exception:
                                seq_notes.append("%s: MP4のGタグ付与失敗" % pn)
                        sub = os.path.join(out, "%s_%s" % (pn, take_str))
                        if os.path.isdir(sub):
                            pre = "%s_%s_%s." % (name_body, pn, take_str)
                            for f in os.listdir(sub):
                                if f.startswith(pre):
                                    try:
                                        os.replace(
                                            os.path.join(sub, f),
                                            os.path.join(sub, "%s_%s_%s_%s.%s"
                                                         % (name_body, pn, take_str,
                                                            gtag, f[len(pre):])))
                                    except Exception:
                                        pass
                            try:
                                os.replace(sub, os.path.join(
                                    out, "%s_%s_%s" % (pn, take_str, gtag)))
                            except Exception:
                                seq_notes.append("%s: フォルダのGタグ付与失敗" % pn)
                _final(enc_ok, od)

            if cmds:
                self._run_ffmpeg_jobs(cmds, _after_encode)
            else:
                _after_encode(True)

        def _run_cloud_matte(ok, od):
            """雲マットの専用ジョブ群。Matteの前と共用のときは従来の分離モードα
            （遮蔽なし全投影・1ジョブ）、雲マット/Normal 用の単独出力なら静止画タブと
            同じ H5 ペア方式（白W+GeoMask → ベールAll → ベールNone → 雲なしBG の
            4ジョブ・UDS雲レイヤー/フォグ板の雲も拾う・深度順序どおり）。
            失敗してもメイン素材の後処理は完走する（注記のみ）。"""
            if not (ok and cloud_seq["run"]):
                _run_backing_w(ok, od)
                return
            vis_mode = not cloud_seq["mfront"]
            geo_mat = None
            if vis_mode:
                try:
                    geo_mat = core.create_temp_geomask_material()
                except Exception:
                    seq_notes.append("雲マット: GeoMask 生成失敗（遮蔽反映なしで続行）")

            def _cleanup_geo():
                if geo_mat is not None:
                    core.delete_temp_geomask_material()

            def _abort(note, od):
                cloud_seq["run"] = False
                seq_notes.append(note)
                _cleanup_geo()
                _run_backing_w(True, od)

            # αのみ/バッキング系パスは TS=1 固定（TS>1 は空サブフレーム平均でαが
            # 1/TS に希釈される・実測）。モーションブラーは付かないが雲はソフト
            # エッジなので Beauty とのエッジ差は許容範囲。
            # ウォームアップは 8 にキャップ（露出固定・適応なしのパスで長い
            # ウォームアップは不要。ヘルパー4本×24フレーム分の固定費を削る）。
            def _render(label, cb, half_res=False, **kw):
                self.status_var.set("雲マット: %s をレンダ中…" % label)
                self.root.update()
                rw, rh = (W, H)
                if half_res:
                    # CloudBG は H5 式の分母（なだらかな背景輝度）専用。合成側が
                    # 自動リサイズするので半分解像度で画素コスト 1/4 に
                    rw, rh = max(W // 2, 2) & ~1, max(H // 2, 2) & ~1
                try:
                    capture_mrq.render_sequence(
                        seq, out, rw, rh, name_body, take_str,
                        do_png=True, do_mp4=False,
                        temporal_samples=1, warmup=min(warm, 8),
                        custom_start=cs, custom_end=ce,
                        fog_off=self.seq_fog_var.get(), on_done=cb, **kw)
                except Exception as e:
                    self.status_var.set("雲マット起動失敗: %s" % e)
                    _abort("雲マット: 起動失敗 (%s)" % label, od)

            if not vis_mode:
                # 従来の分離モードα（Matteの前の合成と共用・意味を変えない）
                def _cloud_done(cok, cod):
                    if not cok:
                        cloud_seq["run"] = False
                        seq_notes.append("雲マット: レンダ失敗")
                    _cleanup_geo()
                    _run_backing_w(True, od)
                _render("CloudMatte(分離α)", _cloud_done,
                        cloud_matte_actors=cloud_vols_job,
                        beauty_label="CloudMatte")
                return

            # H5 ペア方式 4ジョブチェーン（静止画タブの cloudvis/cloudveil/
            # CloudVeil0/CloudBG と同構成）
            def _bg_done(cok, cod):
                if not cok:
                    _abort("雲マット: CloudBG レンダ失敗", od)
                    return
                n = core.compose_visible_cloud_h5_sequence(
                    out, name_body, take_str,
                    ffmpeg=core.find_ffmpeg(getattr(self, "_ffmpeg_hint", None)))
                if n <= 0:
                    _abort("雲マット: H5合成失敗", od)
                    return
                _cleanup_geo()
                _run_backing_w(True, od)

            def _veil0_done(cok, cod):
                if not cok:
                    _abort("雲マット: CloudVeil0 レンダ失敗", od)
                    return
                _render("CloudBG(雲なし背景)", _bg_done, half_res=True,
                        hidden_actors=list(cloud_vols_job),
                        beauty_label="CloudBG")

            def _veil_done(cok, cod):
                if not cok:
                    _abort("雲マット: CloudVeil レンダ失敗", od)
                    return
                _render("CloudVeil0(雲なしベール)", _veil0_done,
                        cloud_matte_actors=cloud_vols_job,
                        cloud_visible=True, cloud_backing="black",
                        cloud_sources_off=True,
                        hidden_actors=list(cloud_vols_job),
                        use_exr=True, beauty_label="CloudVeil0")

            def _w_done(cok, cod):
                if not cok:
                    _abort("雲マット: CloudMatte(白) レンダ失敗", od)
                    return
                _render("CloudVeil(ベール)", _veil_done,
                        cloud_matte_actors=cloud_vols_job,
                        cloud_visible=True, cloud_backing="black",
                        use_exr=True, beauty_label="CloudVeil")

            _render("CloudMatte(白バッキングT)", _w_done,
                    cloud_matte_actors=cloud_vols_job,
                    cloud_visible=True, cloud_backing="white",
                    geomask_material=geo_mat,
                    use_exr=True, beauty_label="CloudMatte")

        def _run_backing_w(ok, od):
            """バッキングレンダ（板=白発光100・露出固定・EXR・TS=1・何も隠さない）。
            線形色 W ≈ 透過率T×素板レベル（手前の発光は 1/100 で無視できる）。"""
            if not (ok and backing_seq["run"]):
                _finish_outputs(ok, od)
                return

            def _bw_done(bok, bod):
                if not bok:
                    backing_seq["run"] = False
                    seq_notes.append("雲マット: バッキングレンダ失敗")
                _finish_outputs(True, od)

            self.status_var.set("バッキング(白)をレンダ中…")
            self.root.update()
            try:
                capture_mrq.render_sequence(
                    seq, out, W, H, name_body, take_str,
                    do_png=True, do_mp4=False,
                    temporal_samples=1, warmup=warm,
                    custom_start=cs, custom_end=ce,
                    backing_actors=matte_prims, backing_white=True,
                    use_exr=True, beauty_label="BackingW",
                    fog_off=self.seq_fog_var.get(), on_done=_bw_done)
            except Exception as e:
                backing_seq["run"] = False
                seq_notes.append("雲マット: バッキング起動失敗")
                self.status_var.set("バッキング起動失敗: %s" % e)
                _finish_outputs(True, od)

        def _run_skymatte(ok, od):
            """空マット（空=黒・大気光/雲維持）の専用ジョブ。UDS Sky_Sphere→黒ドーム
            差替は render_sequence(sky_matte=True) が行い、レンダ後に自動復元される。
            失敗してもメイン素材の後処理は完走する（注記のみ）。"""
            if not (ok and skymatte_needed):
                _run_cloud_matte(ok, od)
                return

            def _sm_done(sok, sod):
                if not sok:
                    wants["skymatte"] = (False, False)
                    seq_notes.append("空マット: レンダ失敗")
                _run_cloud_matte(True, od)

            self.status_var.set("空マット(SkyMatte)をレンダ中… (空=黒・大気光維持)")
            self.root.update()
            try:
                capture_mrq.render_sequence(
                    seq, out, W, H, name_body, take_str,
                    do_png=True, do_mp4=False,
                    temporal_samples=ts, warmup=warm,
                    custom_start=cs, custom_end=ce,
                    sky_matte=True, beauty_label="SkyMatte",
                    fog_off=True, on_done=_sm_done)
            except Exception as e:
                wants["skymatte"] = (False, False)
                seq_notes.append("空マット: 起動失敗")
                self.status_var.set("空マット起動失敗: %s" % e)
                _run_cloud_matte(True, od)

        def _run_direct(ok, od):
            """Raw Lighting Direct の専用ジョブ（GI/Sky/AO を切った直射のみ）。
            only_direct のときはメインジョブが直射レンダ済みなのでスキップ。
            このジョブの失敗ではレンダ済みのメイン素材の後処理を放棄しない
            （rldir を無効化して完走し、完了メッセージに注記する）。"""
            if not (ok and rldir_needed and not only_direct):
                _run_skymatte(ok, od)
                return

            def _direct_done(dok, dod):
                if not dok:
                    wants["rldir"] = (False, False)
                    seq_notes.append("Raw Lighting Direct: レンダ失敗")
                _run_skymatte(True, od)

            self.status_var.set("Raw Lighting Direct をレンダ中… (GI/Sky/AO off)")
            self.root.update()
            try:
                capture_mrq.render_sequence(
                    seq, out, W, H, name_body, take_str,
                    do_png=True, do_mp4=False,
                    temporal_samples=ts, warmup=warm,
                    custom_start=cs, custom_end=ce,
                    hidden_actors=matte_actors,
                    light_pass=True, light_direct=True,
                    light_label="RawLightingDirect",
                    beauty_label="DirectPlate",
                    fog_off=self.seq_fog_var.get(), on_done=_direct_done)
            except Exception as e:
                wants["rldir"] = (False, False)
                seq_notes.append("Raw Lighting Direct: 起動失敗")
                self.status_var.set("Raw Lighting Direct 起動失敗: %s" % e)
                _run_skymatte(True, od)

        def _after_main(ok, od):
            if not (ok and _need("behind")):
                _run_direct(ok, od)
                return
            # Matteの奥: near-clip した2本目ジョブ。カメラは動く前提で、範囲内の
            # 数フレームをサンプリングした最小距離を使う（fronto-parallel 近似）
            try:
                ncs = []
                cs_cam = None
                for f in sorted({cs_eff, (cs_eff + ce_eff) // 2,
                                 max(cs_eff, ce_eff - 1)}):
                    try:
                        ca = self._sequence_camera_at(seq, f)
                    except Exception:
                        ca = None
                    if ca is None:
                        continue
                    c = {"transform": ca.get_actor_transform()}
                    if cs_cam is None:
                        cs_cam = c
                    ncs.append(core.matte_near_clip_cm(matte_prims, c))
                if not ncs:
                    raise RuntimeError("シーケンスカメラを特定できません")
                nc = min(ncs)
                # HV(雲)は near-clip を無視して写り込むため手前の雲はアクター単位で隠す
                front_vols = core.volumetrics_nearer_than(nc, cs_cam) if vdb else []
            except Exception as e:
                # Behind だけ諦めて、レンダ済みのメイン素材の後処理は続行する
                seq_notes.append("Matteの奥: near-clip 計算失敗のためスキップ")
                self.status_var.set("Matteの奥: near-clip 計算失敗: %s" % e)
                wants["behind"] = (False, False)
                _run_direct(True, od)
                return
            self.status_var.set("Matteの奥プレートをレンダ中… (near-clip %.0fcm)" % nc)
            self.root.update()
            try:
                capture_mrq.render_sequence(
                    seq, out, W, H, name_body, take_str,
                    do_png=True, do_mp4=False,
                    temporal_samples=ts, warmup=warm,
                    custom_start=cs, custom_end=ce,
                    hidden_actors=list(matte_prims) + front_vols, near_clip_cm=nc,
                    beauty_label="BehindPlate",
                    fog_off=self.seq_fog_var.get(), on_done=_run_direct)
            except Exception as e:
                seq_notes.append("Matteの奥: プレート起動失敗のためスキップ")
                self.status_var.set("Matteの奥プレート起動失敗: %s" % e)
                wants["behind"] = (False, False)
                _run_direct(True, od)

        if (cloudmatte_needed or skymatte_needed) and not (
                _need("beauty") or depth_needed or normal_needed or matte_needed
                or objid_needed or rlfull_needed or rldir_needed):
            # 雲マット/空マットのみ: 全編の Beauty メインジョブを省いて
            # 専用ジョブチェーンから開始する（従来は空マット単独でも捨て
            # Beauty を全編レンダしていた）
            self.status_var.set("マット素材のみをレンダ中…")
            self.root.update()
            _run_skymatte(True, out)
            return

        self.status_var.set("シーケンスレンダ中… (PIE / %d〜%dF @%gfps)"
                            % (cs_eff, ce_eff - 1, float(fps_num) / fps_den))
        self.root.update()
        try:
            capture_mrq.render_sequence(
                seq, out, W, H, name_body, take_str,
                do_png=True, do_mp4=False,
                temporal_samples=ts, warmup=warm,
                custom_start=cs, custom_end=ce,
                depth_material=depth_mat, normal_material=normal_mat,
                matte_material=matte_mat, matte_actors=matte_prims,
                objid_material=objid_mat, objid_actors=objid_actors,
                # Matteの奥のみ（Matteの前なし）のとき: MatteSil はプレートジョブ側に
                # 移したため、メインの Beauty から板を消すには単純非表示にする
                hidden_actors=(hide_actors if hide_actors
                               else (matte_prims if (_need("behind")
                                                     and matte_mat is None
                                                     and matte_prims)
                                     else None)),
                fog_off=self.seq_fog_var.get(),
                light_pass=(rlfull_needed or only_direct),
                light_direct=only_direct,
                light_label=("RawLightingDirect" if only_direct
                             else "RawLightingFull"),
                beauty_label=("DirectPlate" if only_direct else "Beauty"),
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
        added = []
        for a in sel:
            try:
                path = a.get_path_name()
                p.setdefault("labels", {})[path] = a.get_actor_label()
            except Exception:
                continue
            if path not in p["all"]:
                p["all"].append(path)
                added.append(path)
        self._pick_refresh(p)
        self.status_var.set("Added %d (list total %d)" % (len(added), len(p["all"])))
        cb = p.get("on_added")
        if cb and added:
            cb(added)

    def _pick_clear(self, p):
        """リスト上で選択（ハイライト）した項目を行インデックスで削除する。"""
        idx = sorted(p["list"].curselection(), reverse=True)
        if not idx:
            self.status_var.set("Clear: リスト内で消したい項目を選択してください")
            return
        removed = []
        for i in idx:
            removed.append(p["all"][i])
            del p["all"][i]
        self._pick_refresh(p)
        self.status_var.set("Removed %d (list total %d)" % (len(idx), len(p["all"])))
        cb = p.get("on_removed")
        if cb and removed:
            cb(removed)

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
        """選択カメラのアスペクトと、現在の幅から算出した解像度を表示する。
        失敗時は空欄にせず理由を出す（「たまに機能しない」の診断性確保）。"""
        try:
            cam = self._current_camera()
            if cam is None:
                self.cam_res_var.set("(no camera)")
                return
            # 選択ラベルのカメラが消えて先頭カメラへフォールバックした場合は明示する
            # （Sequencer スポーナブルはシーケンスを閉じるとレベルから消える）
            note = ""
            try:
                if cam.get_actor_label() != self.cam_var.get():
                    note = "  ※選択カメラ不在→%s" % cam.get_actor_label()
            except Exception:
                pass
            asp = core.get_camera_settings(cam).get("aspect_ratio", 0.0)
            W = self._int_var(self.w_var, 0)
            if asp > 0.1 and W > 0:
                self.cam_res_var.set("→ %d×%d  (%.3f:1)%s"
                                     % (W, int(round(W / asp)), asp, note))
            elif asp > 0.1:
                self.cam_res_var.set("(%.3f:1)%s" % (asp, note))
            else:
                self.cam_res_var.set("(アスペクト未取得)%s" % note)
        except Exception as e:
            self.cam_res_var.set("(取得失敗)")
            unreal.log_warning("[SceneCapture] カメラ解像度表示の更新に失敗: %s" % e)

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
                "normal": self.normal_var.get(),
                "cloudmatte": self.cloudmatte_var.get(),
                "skymatte": self.skymatte_var.get(),
                "beauty": self.beauty_var.get(),
                "mfront": self.mfront_var.get(),
                "behind": self.behind_var.get(),
                "objid": self.objid_var.get(),
                "rlfull": self.rlfull_var.get(),
                "rldir": self.rldir_var.get(),
                "matte_names": self._pick_targets(self.matte_pick),
                "objid_names": self._pick_targets(self.objid_pick),
                "matte_labels": self.matte_pick.get("labels", {}),
                "objid_labels": self.objid_pick.get("labels", {}),
                "depth_bit": self.depth_bit_var.get(),
                "near": self.near_var.get(), "far": self.far_var.get(),
                "normal_space": self.normal_space_var.get(),
                "seq_normal_space": self.seq_normal_space_var.get(),
                "mrq_warmup": self.mrq_warmup_var.get(),
                "mrq_ts": self.mrq_ts_var.get(),
                "beauty_fmt": self.beauty_fmt_var.get(),
                "mrq_camasp": self.mrq_camasp_var.get(),
                "fog_off": self.fog_off_var.get(),
                "vdb_mode": self.vdb_var.get(),
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
                "seq_camasp": self.seq_camasp_var.get(),
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
        _setvar(self.normal_var, "normal")
        _setvar(self.cloudmatte_var, "cloudmatte")
        _setvar(self.skymatte_var, "skymatte")
        _setvar(self.beauty_var, "beauty")
        _setvar(self.mfront_var, "mfront")
        _setvar(self.behind_var, "behind")
        _setvar(self.objid_var, "objid")
        _setvar(self.rlfull_var, "rlfull")
        _setvar(self.rldir_var, "rldir")

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
        # リスト内のマット対象は常にライティング分離、の不変条件を起動時にも適用
        # （set_matte_unlit は差替え済みスロットをスキップするため冪等）
        try:
            if self.matte_pick["all"]:
                self._matte_paths_neutralize(self._pick_targets_resolved(self.matte_pick))
        except Exception:
            pass
        _restore_picker(self.objid_pick, "objid_names", "objid_labels")
        if st.get("depth_bit") in ("8bit PNG", "16bit PNG", "EXR float"):
            self.depth_bit_var.set(st["depth_bit"])
        _setvar(self.near_var, "near"); _setvar(self.far_var, "far")
        if st.get("normal_space") in ("カメラ", "ワールド"):
            self.normal_space_var.set(st["normal_space"])
        if st.get("seq_normal_space") in ("カメラ", "ワールド"):
            self.seq_normal_space_var.set(st["seq_normal_space"])
        _setvar(self.mrq_warmup_var, "mrq_warmup")
        _setvar(self.mrq_ts_var, "mrq_ts")
        if st.get("beauty_fmt") in ("PNG 8bit", "JPG 8bit", "EXR 16bit (float)"):
            self.beauty_fmt_var.set(st["beauty_fmt"])
        _setvar(self.mrq_camasp_var, "mrq_camasp")
        _setvar(self.fog_off_var, "fog_off")
        _v = st.get("vdb_mode")
        if _v in ("Auto", "ON", "OFF"):
            self.vdb_var.set(_v)
        elif _v is False:            # 旧チェックボックス形式の互換
            self.vdb_var.set("OFF")
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
        _setvar(self.seq_camasp_var, "seq_camasp")
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
