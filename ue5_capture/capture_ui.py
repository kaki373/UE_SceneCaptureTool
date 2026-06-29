# -*- coding: utf-8 -*-
"""
capture_ui.py  --  UE5.7 Scene Capture GUI (tkinter)

Python 標準の tkinter でウィンドウを描画する。tkinter の mainloop は UE の
メインスレッドをブロックするため使わず、register_slate_post_tick_callback で
毎フレーム root.update() を呼ぶ「非ブロッキング統合」にする。

tkinter が利用できない環境（UE 同梱 Python に tcl/tk が無い等）では
ImportError を送出するので、呼び出し側（capture_tool.py）が CONFIG/CUI に
フォールバックする。
"""

import os
import json

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


class CaptureWindow(object):
    def __init__(self):
        if not _HAS_TK:
            raise ImportError("tkinter が利用できません。")

        self._tick_handle = None
        self._cameras = core.list_cameras()

        self.root = tk.Tk()
        self.root.title("Scene Capture Tool (UE5.7) ★Beauty版★")
        self.root.geometry("480x980")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._register_tick()
        _window_registry().append(self)   # オーファン対策の登録

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        row = 0
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)

        # Camera（Refresh で現在のレベルのカメラに更新）
        ttk.Label(frm, text="Camera:").grid(row=row, column=0, sticky="w", **pad)
        self.cam_var = tk.StringVar()
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
        self.res_mode = tk.StringVar(value="camera")
        ttk.Radiobutton(camrow, text="Use Camera Setting", variable=self.res_mode,
                        value="camera").pack(side="left")
        self.cam_res_var = tk.StringVar(value="")
        ttk.Label(camrow, textvariable=self.cam_res_var, foreground="#0a7").pack(side="left", padx=(8, 0))
        camrow.grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1
        ovr = ttk.Frame(frm)
        ttk.Radiobutton(ovr, text="Override:", variable=self.res_mode,
                        value="override").pack(side="left")
        self.w_var = tk.StringVar(value="3840")
        self.h_var = tk.StringVar(value="2160")
        tk.Entry(ovr, textvariable=self.w_var, width=6).pack(side="left", padx=2)
        ttk.Label(ovr, text="x").pack(side="left")
        self.h_entry = tk.Entry(ovr, textvariable=self.h_var, width=6)
        self.h_entry.pack(side="left", padx=2)
        self.aspect_lock_var = tk.BooleanVar(value=False)
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
        self.overscan_on_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(osf, text="ON", variable=self.overscan_on_var).pack(side="left")
        self.overscan_mode_var = tk.StringVar(value="percent")
        ttk.Radiobutton(osf, text="%", variable=self.overscan_mode_var,
                        value="percent").pack(side="left", padx=(8, 0))
        self.overscan_var = tk.StringVar(value="0")
        tk.Entry(osf, textvariable=self.overscan_var, width=5).pack(side="left", padx=(2, 8))
        ttk.Radiobutton(osf, text="px", variable=self.overscan_mode_var,
                        value="pixels").pack(side="left")
        ttk.Label(osf, text="X").pack(side="left", padx=(4, 1))
        self.overscan_x_var = tk.StringVar(value="0")
        tk.Entry(osf, textvariable=self.overscan_x_var, width=5).pack(side="left")
        ttk.Label(osf, text="Y").pack(side="left", padx=(4, 1))
        self.overscan_y_var = tk.StringVar(value="0")
        tk.Entry(osf, textvariable=self.overscan_y_var, width=5).pack(side="left")
        osf.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        row += 1

        # anti-aliasing（旧 AA）
        ttk.Label(frm, text="anti-aliasing:").grid(row=row, column=0, sticky="w", **pad)
        self.aa_var = tk.StringVar(value="2x")
        ttk.Combobox(frm, textvariable=self.aa_var, values=["1x", "2x", "4x"],
                     state="readonly", width=8).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        # 露出は MRQ(実カメラの物理露出+PostProcessVolume) が担当するため UI からは廃止。
        # 互換のため変数だけ保持（SceneCapture 旧Color は使わない）。
        self.exp_mode_var = tk.StringVar(value="Auto")
        self.exp_ev_var = tk.StringVar(value="-8")
        self.exp_target_var = tk.StringVar(value="45")

        # Output dir
        ttk.Label(frm, text="Output Dir:").grid(row=row, column=0, sticky="w", **pad)
        default_dir = os.path.normpath(
            os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
        self.out_var = tk.StringVar(value=default_dir)
        tk.Entry(frm, textvariable=self.out_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        ttk.Button(frm, text="...", width=3, command=self._browse).grid(
            row=row, column=2, sticky="w")
        row += 1

        # ファイル名: [任意名]_[カメラ名]_素材名_001（任意名/カメラ名は下のチェックで含める）
        self.name_usecustom_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="任意名を付ける:", variable=self.name_usecustom_var).grid(
            row=row, column=0, sticky="w", **pad)
        self.name_custom_var = tk.StringVar(value="")
        tk.Entry(frm, textvariable=self.name_custom_var, width=28).grid(
            row=row, column=1, sticky="we", **pad)
        row += 1
        self.name_usecam_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="カメラ名を付ける", variable=self.name_usecam_var).grid(
            row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        ttk.Label(frm, text="  ファイル名: [任意名]_[カメラ名]_素材名_001",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=8)
        row += 1
        ttk.Label(frm, text="Passes（Color/Beauty は下の MRQ ボタンで出力）").grid(
            row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1

        # 旧 Color(SceneCapture) は廃止。互換のため変数だけ False で保持。
        self.color_var = tk.BooleanVar(value=False)

        # Depth
        self.depth_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Z-Depth", variable=self.depth_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        depth_frm = ttk.Frame(frm)
        ttk.Label(depth_frm, text="Format:").pack(side="left")
        self.depth_bit_var = tk.StringVar(value="16bit PNG")
        ttk.Combobox(depth_frm, textvariable=self.depth_bit_var,
                     values=["8bit PNG", "16bit PNG", "EXR float"], state="readonly",
                     width=11).pack(side="left", padx=4)
        ttk.Label(depth_frm, text="Near:").pack(side="left")
        self.near_var = tk.StringVar(value="0")
        tk.Entry(depth_frm, textvariable=self.near_var, width=6).pack(side="left", padx=2)
        ttk.Label(depth_frm, text="cm").pack(side="left")
        ttk.Label(depth_frm, text="Far:").pack(side="left", padx=(6, 0))
        self.far_var = tk.StringVar(value="10000")
        tk.Entry(depth_frm, textvariable=self.far_var, width=7).pack(side="left", padx=2)
        ttk.Label(depth_frm, text="cm").pack(side="left")
        depth_frm.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        ttk.Label(frm, text="(Z-Depth 距離は cm 単位＝Unreal世界単位。1m = 100cm)",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        # Depth invert（手前=白/奥=黒）
        self.depth_invert_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="Invert (near=white / far=black)  ※PNGのみ",
                        variable=self.depth_invert_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1

        # Matte（独立した対象ピッカー）
        self.matte_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Matte B/W png image",
                        variable=self.matte_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        # Matte は 選択=黒/周囲=白 で固定（Invert トグルは廃止）。
        self.matte_fill_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="  + Beauty + Matte alpha PNG",
                        variable=self.matte_fill_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        ttk.Label(frm, text="  ※ Matte ON のとき Beauty から対象を自動で隠します（クリーンプレート）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        self.behind_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="  + Behind matte   マットオブジェクトの奥を描画",
                        variable=self.behind_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        self.matte_pick, row = self._make_picker(frm, row, "Matte targets")

        # Object ID（Matte とは別の対象ピッカー）
        self.objid_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Object ID (color-coded + manifest .json)",
                        variable=self.objid_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.objid_fill_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="  + Beauty + ObjectID mask",
                        variable=self.objid_fill_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        self.objid_hide_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="  + Hide-render (対象を非表示にして Beauty をレンダ＝クリーンプレート)",
                        variable=self.objid_hide_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        self.objid_pick, row = self._make_picker(frm, row, "Object ID targets")

        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=8)
        row += 1

        # ---- Beauty (MRQ / シーケンサ品質) ----
        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="we", pady=6)
        row += 1
        ttk.Label(frm, text="Beauty (MRQ = ビューポート露出＋シーケンサ品質)").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1
        self.fog_off_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="  Fogなし", variable=self.fog_off_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        mrqf = ttk.Frame(frm)
        ttk.Label(mrqf, text="ウォームアップ:").pack(side="left")
        self.mrq_warmup_var = tk.StringVar(value="32")
        tk.Entry(mrqf, textvariable=self.mrq_warmup_var, width=5).pack(side="left", padx=2)
        ttk.Label(mrqf, text="サンプリングフレーム:").pack(side="left", padx=(8, 0))
        self.mrq_ts_var = tk.StringVar(value="8")
        tk.Entry(mrqf, textvariable=self.mrq_ts_var, width=5).pack(side="left", padx=2)
        self.mrq_exr_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mrqf, text="EXR", variable=self.mrq_exr_var).pack(side="left", padx=(8, 0))
        self.mrq_camasp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(mrqf, text="カメラのアスペクト", variable=self.mrq_camasp_var).pack(side="left", padx=(8, 0))
        mrqf.grid(row=row, column=0, columnspan=3, sticky="w", padx=24)
        row += 1
        ttk.Label(frm, text="（Matte/Object ID/Depth を一緒に出すには各チェック＋対象を Add Sel。"
                            "matte_fill/objid_fill/objid_hidden は全て Beauty 合成）",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        self.status_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.status_var, foreground="#0a7").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8)
        row += 1

        try:
            ttk.Style().configure("Big.TButton", font=("", 14, "bold"), padding=12)
        except Exception:
            pass
        self.capture_btn = ttk.Button(
            frm, text="Capture", style="Big.TButton", command=self._on_mrq)
        self.capture_btn.grid(row=row, column=0, columnspan=3,
                              pady=14, padx=24, ipady=6, sticky="we")

        frm.columnconfigure(1, weight=1)

        # 前回の入力を復元
        self._load_ui_state()
        self._update_cam_res()

    # ------------------------------------------------------------- handlers
    def _basename(self, pass_type, suf, cam):
        """MRQ 出力名を 任意名_カメラ名_素材名_NNN で組む（SceneCapture 側と同じ規則）。"""
        parts = []
        if self.name_usecustom_var.get():
            c = self.name_custom_var.get().strip()
            if c:
                parts.append(core._safe_name(c))
        if self.name_usecam_var.get() and cam is not None:
            parts.append(core._safe_name(cam.get_actor_label()))
        parts.append(pass_type)
        parts.append(suf)
        return "_".join(parts)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or "/")
        if d:
            self.out_var.set(os.path.normpath(d))

    def _on_mrq(self):
        """Movie Render Queue で Beauty を高品質レンダ（非同期・PIE）。"""
        import capture_mrq
        importlib = __import__("importlib")
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
        try:
            W = int(self.w_var.get())
        except ValueError:
            W = 1920
        if self.mrq_camasp_var.get():
            asp = core.get_camera_settings(cam).get("aspect_ratio", 0.0)
            H = int(round(W / asp)) if asp > 0.1 else 1080
        else:
            try:
                H = int(self.h_var.get())
            except ValueError:
                H = 1080
        # Overscan: ON のとき fx(横)/fy(縦) を決める。% は一律、px は X/Y 別。
        # カメラの filmback を一時拡大して FOV を縦横独立に広げ、解像度も ×(1+f) に拡大。
        fx = fy = 0.0
        if self.overscan_on_var.get():
            if self.overscan_mode_var.get() == "pixels":
                try:
                    pxx = max(0.0, float(self.overscan_x_var.get()))
                except ValueError:
                    pxx = 0.0
                try:
                    pxy = max(0.0, float(self.overscan_y_var.get()))
                except ValueError:
                    pxy = 0.0
                fx = pxx / W if W > 0 else 0.0
                fy = pxy / H if H > 0 else 0.0
            else:
                try:
                    p = max(0.0, float(self.overscan_var.get()) / 100.0)
                except ValueError:
                    p = 0.0
                fx = fy = p
        if fx > 0.0 or fy > 0.0:
            W = int(round(W * (1.0 + fx)))
            H = int(round(H * (1.0 + fy)))
        try:
            warm = int(self.mrq_warmup_var.get())
        except ValueError:
            warm = 32
        try:
            ts = int(self.mrq_ts_var.get())
        except ValueError:
            ts = 8
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
        # ① マスク系データ(Matte白黒 / ObjectID色 / Depth)を同フレーム・同解像度で先に出す。
        #    旧Color(SceneCapture Fill)は一切出さない。matte_fill/objid_fill/objid_hidden は
        #    全て MRQ Beauty を使うので、ここでは作らない。
        matte_path = objid_path = None
        want_matte_fill = self.matte_fill_var.get()
        want_objid_fill = self.objid_fill_var.get()
        want_hidden = self.objid_hide_var.get()
        objid_names = self._pick_targets(self.objid_pick)
        try:
            s = self._collect_settings()
            s.camera_actor = cam
            s.do_color = False
            s.use_camera_resolution = False
            s.override_width, s.override_height = W, H
            s.matte_fill_alpha = False         # Beauty と後段で合成
            s.objid_fill_alpha = False
            s.objid_hide_render = False         # 非表示レンダも Beauty(2回目MRQ)で行う
            s.do_behind_matte = False           # behind は下の MRQ near-clip ジョブで高品質に行う
            s.take_suffix = suf                 # SceneCapture 系の出力にも同じ通し番号
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

        beauty_path = os.path.join(out, self._basename("Beauty", suf, cam) + ".png")
        exr = self.mrq_exr_var.get()

        # Matte ON のときは Beauty から対象を常に隠す（クリーンプレート）。
        beauty_hidden = None
        if self.matte_var.get():
            matte_names = self._pick_targets(self.matte_pick)
            beauty_hidden = core._resolve_target_actors(None, matte_names or None)
            if beauty_hidden:
                self.status_var.set("Beauty: Matte 対象 %d 個を隠して撮影（クリーンプレート）" % len(beauty_hidden))
            else:
                self.status_var.set("Matte ON ですが対象が見つかりません（Beauty は全表示で撮ります）")


        # 後続 MRQ ジョブのキュー
        jobs = []
        if want_hidden and objid_names and objid_path:
            jobs.append(dict(hidden=core._resolve_target_actors(None, objid_names),
                             base=self._basename("ObjectIDClean", suf, cam)))
        # Behind matte: マット面までの距離で near-clip して手前を除去（MRQ Beauty 品質）
        if self.behind_var.get():
            mt = core._resolve_target_actors(None, self._pick_targets(self.matte_pick) or None)
            if mt:
                nc = core.matte_near_clip_cm(mt, core.get_camera_settings(cam))
                jobs.append(dict(hidden=mt, base=self._basename("BehindPlate", suf, cam),
                                 near_clip=nc, composite=True, matte=mt))
            else:
                self.status_var.set("Behind matte: Matte 対象が見つかりません")

        def _run_jobs():
            if not jobs:
                _restore_fb()
                self.status_var.set("完了（Beauty合成・クリーンプレート出力済）")
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
                            os.path.join(out, self._basename("Behind", suf, cam) + ".png"),
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
                    core.blend_with_beauty(
                        beauty_path,
                        matte_path if want_matte_fill else None,
                        objid_path if want_objid_fill else None,
                        matte_out=os.path.join(out, self._basename("MatteBeauty", suf, cam) + ".png"),
                        objid_out=os.path.join(out, self._basename("ObjectIDBeauty", suf, cam) + ".png"))
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
                                      file_basename=self._basename("Beauty", suf, cam),
                                      hidden_actors=beauty_hidden,
                                      fog_off=self.fog_off_var.get(), on_done=_after_beauty)
        except Exception as e:
            _restore_fb()
            self.status_var.set("MRQ 起動失敗: %s" % e)

    def _make_picker(self, frm, row, label):
        """対象アクターのリストを作る。リストの中身＝対象。
        Add Sel: アウトライナ/ビューポートの選択を追加 / Clear: リストで選択した項目を削除。"""
        p = {"all": []}
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
        p2l = self._path2label()
        p["list"].delete(0, "end")
        for path in p["all"]:
            lab = p2l.get(path)
            short = path.rsplit(".", 1)[-1]    # 末尾の内部名だけ補助表示
            p["list"].insert("end", "%s  [%s]" % (lab, short) if lab else "%s  (レベルに無し)" % short)

    def _pick_add_selection(self, p):
        """選択中アクターをリストへ追加。キーはフルパス名（get_path_name()）で重複無視。"""
        sel = core.get_selected_actors()
        added = 0
        for a in sel:
            try:
                path = a.get_path_name()
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
                "color": self.color_var.get(),
                "depth": self.depth_var.get(),
                "matte": self.matte_var.get(),
                "matte_fill": self.matte_fill_var.get(),
                "behind": self.behind_var.get(),
                "objid": self.objid_var.get(),
                "objid_fill": self.objid_fill_var.get(),
                "objid_hide": self.objid_hide_var.get(),
                "matte_names": self._pick_targets(self.matte_pick),
                "objid_names": self._pick_targets(self.objid_pick),
                "depth_bit": self.depth_bit_var.get(),
                "depth_invert": self.depth_invert_var.get(),
                "near": self.near_var.get(), "far": self.far_var.get(),
                "exp_mode": self.exp_mode_var.get(),
                "exp_ev": self.exp_ev_var.get(),
                "exp_target": self.exp_target_var.get(),
                "mrq_warmup": self.mrq_warmup_var.get(),
                "mrq_ts": self.mrq_ts_var.get(),
                "mrq_exr": self.mrq_exr_var.get(),
                "mrq_camasp": self.mrq_camasp_var.get(),
                "fog_off": self.fog_off_var.get(),
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
        _setvar(self.color_var, "color"); _setvar(self.depth_var, "depth")
        _setvar(self.matte_var, "matte")
        _setvar(self.matte_fill_var, "matte_fill")
        _setvar(self.behind_var, "behind")
        _setvar(self.objid_var, "objid")
        _setvar(self.objid_fill_var, "objid_fill")
        _setvar(self.objid_hide_var, "objid_hide")

        def _restore_picker(p, names_key):
            names = st.get(names_key)
            if isinstance(names, str):   # 旧形式互換
                names = [x.strip() for x in names.split(",") if x.strip()]
            if names:
                # 旧設定はラベル/内部名を保存していた。ラベル一致するものはフルパス名へ移行する。
                label2path = {v: k for k, v in self._path2label().items()}
                p["all"] = [label2path.get(n, n) for n in names]
                self._pick_refresh(p)
        _restore_picker(self.matte_pick, "matte_names")
        _restore_picker(self.objid_pick, "objid_names")
        if st.get("depth_bit") in ("8bit PNG", "16bit PNG", "EXR float"):
            self.depth_bit_var.set(st["depth_bit"])
        _setvar(self.depth_invert_var, "depth_invert")
        _setvar(self.near_var, "near"); _setvar(self.far_var, "far")
        if st.get("exp_mode") in ("Auto", "Scene (viewport)", "Manual EV"):
            self.exp_mode_var.set(st["exp_mode"])
        _setvar(self.exp_ev_var, "exp_ev")
        _setvar(self.exp_target_var, "exp_target")
        _setvar(self.mrq_warmup_var, "mrq_warmup")
        _setvar(self.mrq_ts_var, "mrq_ts")
        _setvar(self.mrq_exr_var, "mrq_exr")
        _setvar(self.mrq_camasp_var, "mrq_camasp")
        _setvar(self.fog_off_var, "fog_off")

    def _collect_settings(self):
        s = core.CaptureSettings()
        s.camera_actor = self._current_camera()
        s.use_camera_resolution = (self.res_mode.get() == "camera")
        try:
            s.override_width = int(self.w_var.get())
            s.override_height = int(self.h_var.get())
        except ValueError:
            pass
        s.aa_factor = {"1x": 1, "2x": 2, "4x": 4}.get(self.aa_var.get(), 2)
        s.output_dir = self.out_var.get().strip()
        s.name_prefix = self.name_custom_var.get().strip() if self.name_usecustom_var.get() else ""
        s.name_include_camera = self.name_usecam_var.get()
        s.fog_off = self.fog_off_var.get()
        s.do_color = self.color_var.get()
        s.do_depth = self.depth_var.get()
        s.do_matte = self.matte_var.get()
        s.matte_invert = True              # 選択=黒/周囲=白 で固定
        s.matte_fill_alpha = self.matte_fill_var.get()
        s.depth_hide_matte = self.matte_var.get()   # Matte ON なら Z-Depth からも対象を除外
        s.do_behind_matte = self.behind_var.get()
        s.do_object_id = self.objid_var.get()
        s.objid_fill_alpha = self.objid_fill_var.get()
        s.objid_hide_render = self.objid_hide_var.get()
        # Matte 対象（リストの中身＝対象。空ならエディタ選択にフォールバック）
        s.matte_actors = None
        s.matte_actor_names = self._pick_targets(self.matte_pick) or None
        # Object ID 対象（Matte とは別リスト）
        s.objid_actors = None
        s.objid_actor_names = self._pick_targets(self.objid_pick) or None
        dsel = self.depth_bit_var.get()
        if dsel.startswith("8"):
            s.depth_bit = "8bit"
        elif dsel.startswith("16"):
            s.depth_bit = "16bit"
        else:
            s.depth_bit = "exr"
        s.depth_invert = self.depth_invert_var.get()
        try:
            s.depth_near = float(self.near_var.get())
            s.depth_far = float(self.far_var.get())
        except ValueError:
            pass
        # 露出
        em = self.exp_mode_var.get()
        if em.startswith("Scene"):
            s.exposure_mode = "scene"
        elif em.startswith("Manual"):
            s.exposure_mode = "manual"
        else:
            s.exposure_mode = "auto"
        try:
            s.exposure_bias = float(self.exp_ev_var.get())
        except ValueError:
            pass
        try:
            s.exposure_target = float(self.exp_target_var.get())
        except ValueError:
            pass
        s.matte_actors = None  # 実行時に選択アクター取得
        return s

    def _on_capture(self):
        s = self._collect_settings()
        problems = s.validate()
        if problems:
            self.status_var.set("NG: " + " / ".join(problems))
            return
        if s.output_dir and not os.path.isdir(s.output_dir):
            try:
                os.makedirs(s.output_dir)
            except Exception:
                pass
        self.status_var.set("Capturing...")
        self.root.update()
        self._save_ui_state()      # 入力内容を保持（次回復元用）
        outs = core.run_capture(s)
        self.status_var.set("Done: %d file(s)" % len(outs) if outs else "Failed (see Output Log)")

    # ------------------------------------------------------------ UE tick
    def _register_tick(self):
        def _tick(dt):
            try:
                self.root.update()
            except Exception:
                self._unregister_tick()
        self._tick_handle = unreal.register_slate_post_tick_callback(_tick)

    def _unregister_tick(self):
        if self._tick_handle is not None:
            try:
                unreal.unregister_slate_post_tick_callback(self._tick_handle)
            except Exception:
                pass
            self._tick_handle = None

    def _on_close(self):
        self._save_ui_state()
        self._unregister_tick()
        try:
            _window_registry().remove(self)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


_window_ref = None  # GC 防止


def _window_registry():
    """reload をまたいで残るウィンドウ登録簿（unreal モジュールに保持）。"""
    if not hasattr(unreal, "_ue5capture_windows"):
        unreal._ue5capture_windows = []
    return unreal._ue5capture_windows


def close_all_windows():
    """これまでに開いた全ツールウィンドウを閉じる（オーファン対策）。"""
    reg = _window_registry()
    for w in list(reg):
        try:
            w._on_close()
        except Exception:
            pass
    reg[:] = []


def show():
    """GUI を表示。既存ウィンドウは全て閉じてから1枚だけ開く。"""
    global _window_ref
    close_all_windows()
    _window_ref = CaptureWindow()
    return _window_ref
