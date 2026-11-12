# -*- coding: utf-8 -*-
"""
capture_ui.py  --  UE5.7/5.8 Scene Capture GUI (tkinter)

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
    from tkinter import ttk, filedialog, messagebox
    _HAS_TK = True
except Exception:
    tk = None
    ttk = None
    filedialog = None
    messagebox = None
    _HAS_TK = False


# 映像タブの出力素材: (キー, UI ラベル, ファイル素材名)
# 「露出固定」: 自動露出(Eye Adaptation)をレンダ中だけ切る。MRQ ジョブ内 cvar なので
# レンダ後にエンジンが復元し、PPV/カメラは変更しない。固定値は適応が収束した明るさに
# ほぼ一致する（AV024 実測 ±2 L*）。チェーンの全ジョブ（Behind/空マット/雲ヘルパー）に効く。
_EXPOSURE_LOCK_CVARS = [("r.EyeAdaptationQuality", 0)]
_EXPOSURE_LOCK_TIP = ("自動露出(Eye Adaptation)をレンダ中だけ切り、フレーム間の明るさの揺れを止める。"
                      "PPV/カメラは変更せずレンダ後に復元")


# 「ビューポートのルック」: エディタのビューポートで見えている見た目（フォグ/露出/ポスプロ）で撮る
_VIEWPORT_LOOK_TIP = ("ビューポートで見えているルック（フォグ/露出/ポスプロ）で Beauty を撮る。"
                      "ビューポートがこのカメラをパイロット中なら、そのビューポートを指定解像度で撮影"
                      "（撮影中はエディタを前面に）。パイロットしていない時・EXR 形式は MRQ 近似"
                      "（高品質 cvar なし・スカラビリティはエディタのまま）。他の素材は従来どおり MRQ。"
                      "パイロット中でなければカメラ自身のポスプロ（露出補正・ビネット）は外す。"
                      "Fog/グローなし・露出固定とは排他（ON の間は無効）")
_SEQ_VIEWPORT_LOOK_TIP = ("ビューポートの見た目に近づけて Beauty を撮る MRQ 近似（完全一致ではない）。"
                          "高品質 cvar を使わずスカラビリティはエディタのまま、パイロット中でないカメラの"
                          "ポスプロ（露出補正・ビネット）を外す。外せないカメラ（スポーナブル）の露出補正は"
                          "レベルの値から求めて打ち消す（試し撮りなし）。"
                          "他の素材は従来どおり。"
                          "Fog/グローなし・露出固定とは排他（ON の間は無効）")
# ビューポート撮影（HighResShot）の完了待ちの上限（秒）。シェーダ/ストリーミング待ちを含む
_VIEWPORT_GRAB_TIMEOUT = 60.0
# 静止画のビューポート撮影の一時名（タイムアウト後に遅れて書かれると残るので次回開始時に消す）
_VPLOOK_TMP = "_vplook_tmp.png"


def _apply_exposure_lock(on):
    """capture_mrq が全ジョブに合流させる追加 cvar（reload をまたいで残る属性）を設定。
    「モーションブラーなし」もここで一旦外す（映像タブは直後に _apply_no_motion_blur で入れ直す）。"""
    unreal._ue5capture_extra_cvars = list(_EXPOSURE_LOCK_CVARS) if on else []
    unreal._ue5capture_no_motion_blur = False


def _apply_no_motion_blur(on):
    """映像タブの「モーションブラーなし」。_apply_exposure_lock の後に呼ぶ。
    エンジンのブラーは cvar、時間サンプルのブレ（シャッター）は capture_mrq が PIE 側で止める。"""
    if on:
        unreal._ue5capture_extra_cvars = (list(getattr(unreal, "_ue5capture_extra_cvars", None) or [])
                                          + [("r.MotionBlurQuality", 0)])
    unreal._ue5capture_no_motion_blur = bool(on)


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

# 映像タブの出力素材の並び（表示グループ, キー）。_SEQ_OUTPUTS の順序は処理側が使う
_SEQ_GROUPS = [
    ("基本", ("beauty", "rlfull", "rldir")),
    ("ジオメトリ", ("depth", "normal", "objid")),
    ("マット", ("mfront", "behind", "cloudmatte", "skymatte")),
]
_SEQ_SHORT = {
    "normal": "Normal", "mfront": "Matteの前", "cloudmatte": "雲マット",
    "skymatte": "空マット", "rlfull": "Raw Lighting Full",
}
_SEQ_TIPS = {
    "depth": "手前=白 / 奥=黒",
    "normal": "法線 RGB = XYZ*0.5+0.5。VDB雲の領域は黒",
    "objid": "Object ID targets を色分け・他は黒。色↔名前の JSON 付き",
    "mfront": "Beauty にマットをアルファとして焼いた RGBA（マット板より手前だけ）",
    "behind": "マット板の手前を取り除いて奥だけを描画（2本目のレンダ＋合成）",
    "cloudmatte": "VDB雲=黒 / 背景=白",
    "skymatte": "空=黒・大気光/雲は維持",
    "rlfull": "ライティングのみ素材（落ち影+シェーディング）。Sun+GI+Sky",
    "rldir": "GI/スカイライト/AO なしの直射のみ",
}

# パネルの配色（UE エディタに合わせたダーク）
_THEME = {
    "bg": "#1b1b1b", "card": "#282828", "field": "#161616", "fg": "#e4e4e4",
    "sub": "#9b9b9b", "border": "#3c3c3c", "btn": "#3d3d3d", "btn_hi": "#4d4d4d",
    "accent": "#2f7de1", "accent_hi": "#4a93f0", "ok": "#4cc38a", "tip": "#111111",
}

# 画像タブの出力形式 → 拡張子（Beauty/Raw Lighting 系で共用）
_FMT_EXT = {"png": ".png", "jpg": ".jpg", "exr": ".exr"}

# MP4 レートプリセット（H.264 の CRF。小さいほど高品質・大容量）
_MP4_RATE_PRESETS = {
    "最高 (CRF 17)": 17,
    "高 (CRF 20)": 20,
    "標準 (CRF 24)": 24,
    "軽量 (CRF 28)": 28,
}



_NAME_TIP = ("ファイル名テンプレート。使えるトークン:\n"
             "  {level}  永続レベル名（例 LV_047_MASTER）\n"
             "  {cut}    カット番号（表示中サブレベルの親フォルダ名とサブレベル名に共通の番号。例 047）\n"
             "  {camera} カメラ名（映像はカメラカット先頭のカメラ）\n"
             "  {seq}    シーケンス名\n"
             "空のトークンは消え、区切りの _ は詰める。出力は 名前_素材名_NNN")
_OVERWRITE_TIP = ("ON: テイク番号を進めず、この名前の最新テイク（無ければ 001）の既存出力を消して\n"
                  "同じ番号で出し直す。OFF: 既存を残して次の番号で出す")
_OUT_DIR_TIP = ("出力フォルダ。ファイル名と同じトークン {level} {cut} {camera} {seq} が使える\n"
                "（例 D:\\Captures\\{level}\\{cut}）。撮る時に展開し、無ければ作る。\n"
                "設定には展開前のまま保存する")
_NO_MBLUR_TIP = ("全素材をモーションブラーなしでレンダする。エンジンのブラーを切り、シャッターも閉じる" + chr(10) +
                 "（時間サンプルは同じ時刻に揃うので、アンチエイリアスには効いたままブレだけ無くなる）")
_BG_TIP = ("別のエディタ（画面に出ない）を裏で起動してレンダする。このエディタはその間も使える。\n"
           "保存済みのレベル/シーケンスを撮る（未保存の変更は写らない）。\n"
           "2 台目のエディタぶんの VRAM/メモリが要る。複数回押すとキューに積んで順番に撮る"
           "（キューリストで確認・中断）。\n"
           "裏のエディタが動いている間は、そのジョブのレベル・シーケンス・参照アセットを手元で"
           "保存できない（ファイルを掴んでいるため。キューが空になると裏のエディタは終了する）。\n"
           "ビューポートのルックは MRQ 近似（ビューポートのパイロット状態は引き継がない）")
# バックグラウンドの待ち行列の表示を更新する間隔（秒）
_BG_POLL_SEC = 1.0
# 動いているジョブも待ちも無い間の更新間隔（秒。Saved がネットワーク上でも tick を重くしない）
_BG_IDLE_POLL_SEC = 5.0


class CaptureWindow(object):
    def __init__(self, headless=False, settings_path=None):
        """headless=True: バックグラウンドのワーカーが使う非表示パネル（表示・tick 登録なし）。
        settings_path: 設定ファイルの差し替え（ワーカーはジョブの設定スナップショットを読み、
        ユーザーの UE5Capture_ui_settings.json には書かない）。"""
        if not _HAS_TK:
            raise ImportError("tkinter が利用できません。")
        self._headless = bool(headless)
        self._settings_override = settings_path

        self._cameras = core.list_cameras()

        self.root = _persistent_root()
        if self._headless:
            self.root.withdraw()  # 一度も表示しない（最初の update より前に withdraw）
        for child in self.root.winfo_children():
            child.destroy()      # 子ウィジェットの破棄は安全（ルートだけは破棄禁止）
        self.root.title("Scene Capture Tool")
        self.root.geometry("580x%d" % min(920, self.root.winfo_screenheight() - 120))
        self.root.minsize(560, 420)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        if self._headless:
            return
        self.root.deiconify()
        self._apply_topmost()       # 再表示でも設定どおり（ルートは使い回しなので前回の属性が残る）
        self._dark_titlebar()
        self._register_tick()

    # ------------------------------------------------------------------ UI
    def _build(self):
        self._apply_theme()
        outer = ttk.Frame(self.root, style="Body.TFrame")
        outer.pack(fill="both", expand=True)
        # ステータスはタブ共通で最下部に表示（先に pack して常に見える位置に固定）
        self.status_var = tk.StringVar(master=self.root, value="")
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel",
                  wraplength=520, justify="left").pack(
            side="bottom", fill="x", padx=12, pady=(2, 8))
        # 対象（Matte / ObjectID）は両タブで同じリストを使うので Notebook の外に置く
        # （タブを切り替えても見える。status の上・タブの下）
        self._build_shared_targets(outer)
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        # ⚙: パネルの表示設定（タブ列の右端の空き。既定は従来どおり＝手前に固定しない）
        self.topmost_var = tk.BooleanVar(master=self.root, value=False)
        self._tip(ttk.Button(outer, text="⚙", width=2, style="Fold.TButton",
                             command=self._open_settings_menu), "パネルの表示設定").place(
            relx=1.0, x=-12, y=10, anchor="ne")
        tab_img = ttk.Frame(nb, style="Body.TFrame")
        tab_seq = ttk.Frame(nb, style="Body.TFrame")
        nb.add(tab_img, text="画像キャプチャ")
        nb.add(tab_seq, text="映像キャプチャ")
        self._scroll_canvases = []
        self._build_image_tab(tab_img)
        self._build_seq_tab(tab_seq)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        # 前回の入力を復元
        self._load_ui_state()
        self._update_cam_res()
        self._refresh_sequence()

    # ---- テーマ / レイアウト部品 ----
    def _dark_titlebar(self):
        """Windows のタイトルバーをダークにする（効かない環境では何もしない）。"""
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(   # DWMWA_USE_IMMERSIVE_DARK_MODE
                hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
        except Exception:
            pass

    def _apply_theme(self):
        """UE エディタに馴染むダークテーマ。色とフォントはここだけで決める。"""
        c = _THEME
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg=c["bg"])
        st.configure(".", background=c["card"], foreground=c["fg"],
                     fieldbackground=c["field"], bordercolor=c["border"],
                     lightcolor=c["card"], darkcolor=c["card"],
                     troughcolor=c["bg"], focuscolor=c["card"])
        st.configure("Body.TFrame", background=c["bg"])
        st.configure("Title.TLabel", font=("", 10, "bold"))
        st.configure("Group.TLabel", foreground=c["sub"], font=("", 8, "bold"))
        st.configure("Sub.TLabel", foreground=c["sub"])
        st.configure("Val.TLabel", foreground=c["ok"])
        st.configure("Status.TLabel", background=c["bg"], foreground=c["ok"])
        st.configure("TButton", background=c["btn"], borderwidth=0, padding=(8, 3))
        st.map("TButton", background=[("disabled", c["card"]), ("active", c["btn_hi"])],
               foreground=[("disabled", c["sub"])])
        st.configure("Accent.TButton", background=c["accent"], foreground="#ffffff",
                     font=("", 12, "bold"), padding=10)
        st.map("Accent.TButton",
               background=[("disabled", c["btn"]), ("active", c["accent_hi"])])
        st.configure("Fold.TButton", background=c["bg"], foreground=c["sub"],
                     anchor="w", padding=(4, 4))
        st.map("Fold.TButton", background=[("active", c["bg"])],
               foreground=[("active", c["fg"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, indicatorforeground="#ffffff", indicatormargin=(1, 1, 6, 1),
                         upperbordercolor=c["border"], lowerbordercolor=c["border"])
            st.map(w, background=[("active", c["card"])],
                   indicatorbackground=[("selected", c["accent"]),
                                        ("!selected", c["field"])],
                   upperbordercolor=[("selected", c["accent"])],
                   lowerbordercolor=[("selected", c["accent"])],
                   foreground=[("disabled", c["sub"])])
        st.configure("TEntry", insertcolor=c["fg"], padding=2)
        st.map("TEntry", fieldbackground=[("disabled", c["card"])],
               foreground=[("disabled", c["sub"])])
        st.configure("TCombobox", arrowcolor=c["fg"], background=c["btn"], padding=2,
                     lightcolor=c["field"], darkcolor=c["field"])
        st.map("TCombobox", fieldbackground=[("readonly", c["field"])],
               background=[("active", c["btn_hi"]), ("readonly", c["btn"])],
               foreground=[("readonly", c["fg"])],
               selectbackground=[("readonly", c["field"])],
               selectforeground=[("readonly", c["fg"])])
        st.configure("TNotebook", background=c["bg"], borderwidth=0,
                     bordercolor=c["bg"], lightcolor=c["bg"], darkcolor=c["bg"],
                     tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=c["bg"], foreground=c["sub"],
                     padding=(20, 7), borderwidth=0, bordercolor=c["bg"],
                     lightcolor=c["bg"], darkcolor=c["bg"], font=("", 10, "bold"))
        st.map("TNotebook.Tab", background=[("selected", c["card"])],
               foreground=[("selected", c["fg"])],
               lightcolor=[("selected", c["accent"])],
               padding=[("selected", (20, 7))], expand=[("selected", (0, 0, 0, 0))])
        st.configure("Vertical.TScrollbar", background=c["btn"], arrowcolor=c["sub"],
                     borderwidth=0, bordercolor=c["bg"], lightcolor=c["btn"],
                     darkcolor=c["btn"], troughcolor=c["bg"])
        st.map("Vertical.TScrollbar", background=[("active", c["btn_hi"])])
        for k, v in (("background", c["field"]), ("foreground", c["fg"]),
                     ("selectBackground", c["accent"]), ("selectForeground", "#ffffff")):
            self.root.option_add("*TCombobox*Listbox." + k, v)
        # ホイールはページのスクロールに使う（コンボの値が意図せず変わるのを防ぐ）
        self.root.unbind_class("TCombobox", "<MouseWheel>")

    def _open_settings_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=_THEME["card"], fg=_THEME["fg"],
                    activebackground=_THEME["accent"], activeforeground="#ffffff",
                    selectcolor=_THEME["fg"])
        m.add_checkbutton(label="手前に表示", variable=self.topmost_var,
                          command=self._on_topmost_change)
        try:
            m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            m.grab_release()

    def _on_topmost_change(self):
        self._apply_topmost()
        self._save_ui_state()

    def _apply_topmost(self, win=None):
        """「手前に表示」をパネル（と待ち行列ウィンドウ）へ反映。非表示パネル（ワーカー）は無視。"""
        if getattr(self, "_headless", False):
            return
        on = bool(self.topmost_var.get())
        bw = getattr(self, "_bg_win", None)
        targets = [win] if win is not None else [self.root] + ([bw["top"]] if bw else [])
        for w in targets:
            try:
                w.attributes("-topmost", on)
            except Exception:
                pass

    def _scrollable(self, parent):
        """縦スクロールする本文領域。中身を置くフレームを返す。"""
        canvas = tk.Canvas(parent, bg=_THEME["bg"], highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas, style="Body.TFrame")
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        self._scroll_canvases.append(canvas)
        return body

    def _on_mousewheel(self, event):
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return
        while w is not None:
            if isinstance(w, tk.Listbox):
                return                      # リストは自前でスクロールする
            if w in self._scroll_canvases:
                first, last = w.yview()
                if first > 0.0 or last < 1.0:
                    w.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return
            w = w.master

    def _card(self, parent, title=None):
        """見出し付きのカード。ウィジェットを置く中身フレームを返す。"""
        card = ttk.Frame(parent, padding=(12, 8, 12, 10))
        card.pack(fill="x", padx=4, pady=(0, 8))
        if title:
            ttk.Label(card, text=title, style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        body = ttk.Frame(card)
        body.pack(fill="x")
        return body

    def _fold(self, parent, title):
        """折りたたみ（初期状態は閉）。中身フレームを返す。"""
        holder = ttk.Frame(parent, style="Body.TFrame")
        holder.pack(fill="x")
        state = {"open": False}
        btn = ttk.Button(holder, style="Fold.TButton", text="▸  " + title)
        btn.pack(fill="x", padx=4)
        card = ttk.Frame(holder, padding=(12, 8, 12, 10))
        body = ttk.Frame(card)
        body.pack(fill="x")

        def _toggle():
            state["open"] = not state["open"]
            if state["open"]:
                card.pack(fill="x", padx=4, pady=(0, 8))
            else:
                card.pack_forget()
            btn.configure(text=("▾  " if state["open"] else "▸  ") + title)

        btn.configure(command=_toggle)
        return body

    def _row(self, parent, label=None, pady=3):
        """1行ぶんの横並びフレーム。label があれば左端に固定幅で置く。"""
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=pady)
        if label:
            ttk.Label(r, text=label, width=11, style="Sub.TLabel").pack(side="left")
        return r

    def _group(self, parent, text):
        ttk.Label(parent, text=text, style="Group.TLabel").pack(anchor="w", pady=(8, 2))

    def _tip(self, widget, text):
        """マウスを乗せると出る説明（常時表示の注記の置き換え）。"""
        state = {"job": None, "win": None}

        def _show():
            state["job"] = None
            try:
                x = widget.winfo_rootx() + 16
                y = widget.winfo_rooty() + widget.winfo_height() + 4
                tw = tk.Toplevel(self.root)
                tw.wm_overrideredirect(True)
                if getattr(self, "topmost_var", None) is not None and self.topmost_var.get():
                    tw.attributes("-topmost", True)   # 手前に表示のパネルの下に隠れない
                tw.wm_geometry("+%d+%d" % (x, y))
                tk.Label(tw, text=text, justify="left", wraplength=380,
                         bg=_THEME["tip"], fg=_THEME["fg"], padx=8, pady=5,
                         highlightthickness=1,
                         highlightbackground=_THEME["border"]).pack()
                state["win"] = tw
            except Exception:
                state["win"] = None

        def _enter(e):
            if state["job"] is None and state["win"] is None:
                state["job"] = self.root.after(450, _show)

        def _leave(e):
            if state["job"] is not None:
                self.root.after_cancel(state["job"])
                state["job"] = None
            if state["win"] is not None:
                try:
                    state["win"].destroy()
                except Exception:
                    pass
                state["win"] = None

        widget.bind("<Enter>", _enter, add="+")
        widget.bind("<Leave>", _leave, add="+")
        widget.bind("<ButtonPress>", _leave, add="+")
        return widget

    def _check(self, parent, text, var, tip=None, **kw):
        cb = ttk.Checkbutton(parent, text=text, variable=var, **kw)
        if tip:
            self._tip(cb, tip)
        return cb

    def _entry(self, parent, var, width):
        return ttk.Entry(parent, textvariable=var, width=width)

    def _build_shared_targets(self, outer):
        """Matte / Object ID の対象リスト。画像・映像の両タブが同じリストを参照するので
        Notebook の外（下部）に常時表示する。2 列（Matte | ObjectID）で縦を節約。"""
        holder = ttk.Frame(outer, style="Body.TFrame")
        holder.pack(side="bottom", fill="x", padx=8, pady=(4, 0))
        card = ttk.Frame(holder, padding=(12, 8, 12, 10))
        card.pack(fill="x", padx=4)
        head = ttk.Frame(card)
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="対象", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="画像 / 映像 共通", style="Sub.TLabel").pack(
            side="left", padx=(8, 0))
        cols = ttk.Frame(card)
        cols.pack(fill="x")
        cols.columnconfigure(0, weight=1, uniform="pick")
        cols.columnconfigure(1, weight=1, uniform="pick")
        left = ttk.Frame(cols)
        right = ttk.Frame(cols)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.matte_pick = self._make_picker(
            left, "Matte targets",
            "Matteの前 / Matteの奥 の対象（両タブ共通）。空ならエディタ選択を使う。"
            "リストに入れた対象は自動で 影/AO/受光なし になり、外すと元に戻る。"
            "出力時は Beauty から自動で隠す")
        # マット対象はリストに入れた時点でライティングから分離、外したら復元する
        self.matte_pick["on_added"] = self._matte_paths_neutralize
        self.matte_pick["on_removed"] = self._matte_paths_restore
        self.objid_pick = self._make_picker(
            right, "Object ID targets",
            "ObjectID で色分けする対象（両タブ共通）。空ならエディタ選択を使う")

    def _build_image_tab(self, frm):
        """単発キャプチャ。上から カメラ / 出力先 / 出力素材 / 詳細設定、
        実行ボタンは下端に固定（対象リストはタブ外の共通カード）。"""
        foot = ttk.Frame(frm, style="Body.TFrame")
        foot.pack(side="bottom", fill="x")
        self.capture_btn = ttk.Button(foot, text="キャプチャ", style="Accent.TButton",
                                      command=self._on_mrq)
        self.capture_btn.pack(fill="x", padx=4, pady=(6, 4))
        body = self._scrollable(frm)
        ttk.Frame(body, style="Body.TFrame", height=8).pack()

        # ---- カメラ ----
        camf = self._card(body, "カメラ")
        r = self._row(camf, "カメラ")
        self.cam_var = tk.StringVar(master=self.root)
        cam_labels = [c.get_actor_label() for c in self._cameras] or ["(no camera)"]
        self.cam_combo = ttk.Combobox(r, textvariable=self.cam_var,
                                      values=cam_labels, state="readonly", width=28)
        self.cam_combo.current(0)
        self.cam_combo.pack(side="left", fill="x", expand=True)
        self._tip(ttk.Button(r, text="⟳", width=3, command=self._refresh_cameras),
                  "現在のレベルのカメラを取り直す").pack(side="left", padx=(6, 0))
        self.cam_combo.bind("<<ComboboxSelected>>", lambda e: self._on_camera_change())

        r = self._row(camf, "解像度")
        self.res_mode = tk.StringVar(master=self.root, value="camera")
        ttk.Radiobutton(r, text="カメラ設定", variable=self.res_mode,
                        value="camera").pack(side="left")
        self.cam_res_var = tk.StringVar(master=self.root, value="")
        ttk.Label(r, textvariable=self.cam_res_var, style="Val.TLabel").pack(
            side="left", padx=(8, 0))
        r = self._row(camf, " ")
        ttk.Radiobutton(r, text="指定", variable=self.res_mode,
                        value="override").pack(side="left")
        self.w_var = tk.StringVar(master=self.root, value="1920")
        self.h_var = tk.StringVar(master=self.root, value="1080")
        self._entry(r, self.w_var, 6).pack(side="left", padx=(8, 2))
        ttk.Label(r, text="×").pack(side="left")
        self.h_entry = self._entry(r, self.h_var, 6)
        self.h_entry.pack(side="left", padx=2)
        self.aspect_lock_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "アスペクト維持", self.aspect_lock_var,
                    "幅を変えると高さを自動で合わせる",
                    command=self._on_width_change).pack(side="left", padx=(10, 0))
        self._aspect_guard = False  # W↔H 相互更新のループ防止
        self.w_var.trace_add("write", lambda *a: self._on_width_change())
        self.h_var.trace_add("write", lambda *a: self._on_height_change())
        # Override に入る直前の解像度を覚えて、Camera Setting に戻したら復元する
        self._prev_res_mode = self.res_mode.get()
        self._saved_cam_wh = None        # Camera Setting 用に退避した解像度
        self._saved_override_wh = None   # Override 入力値（カメラ切替まで維持）
        self.res_mode.trace_add("write", lambda *a: self._on_res_mode_change())

        # ---- 出力先 ----
        outf = self._card(body, "出力先")
        r = self._row(outf, "フォルダ")
        default_dir = os.path.normpath(
            os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
        self.out_var = tk.StringVar(master=self.root, value=default_dir)
        self._tip(self._entry(r, self.out_var, 28), _OUT_DIR_TIP).pack(
            side="left", fill="x", expand=True)
        ttk.Button(r, text="…", width=3, command=self._browse).pack(
            side="left", padx=(6, 0))
        r = self._row(outf, "ファイル名")
        self.name_tpl_var = tk.StringVar(master=self.root, value=core.DEFAULT_NAME_TEMPLATE)
        e = self._entry(r, self.name_tpl_var, 26)
        e.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self._tip(e, _NAME_TIP)
        self.overwrite_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "上書き", self.overwrite_var, _OVERWRITE_TIP).pack(side="left")
        self.name_preview_var = tk.StringVar(master=self.root, value="")
        ttk.Label(outf, textvariable=self.name_preview_var,
                  style="Sub.TLabel").pack(anchor="w", padx=(80, 0))
        self.name_tpl_var.trace_add("write", lambda *a: self._update_name_preview())

        # ---- 出力素材 ----
        matf = self._card(body, "出力素材")
        r = self._row(matf, "形式")
        self.beauty_fmt_var = tk.StringVar(master=self.root, value="PNG 8bit")
        self._tip(ttk.Combobox(r, textvariable=self.beauty_fmt_var, state="readonly",
                               width=16,
                               values=["PNG 8bit", "JPG 8bit", "EXR 16bit (float)"]),
                  "Beauty / Raw Lighting の形式。Matte系合成と ObjectID は常に PNG"
                  ).pack(side="left")
        self.fog_off_var = tk.BooleanVar(master=self.root, value=False)
        self.fog_off_cb = self._check(r, "Fog/グローなし", self.fog_off_var,
                                      "フォグとポスプロのグロー(ブルーム)を切ってレンダする")
        self.fog_off_cb.pack(side="left", padx=(14, 0))
        self.exposure_lock_var = tk.BooleanVar(master=self.root, value=False)
        self.exposure_lock_cb = self._check(r, "露出固定", self.exposure_lock_var,
                                            _EXPOSURE_LOCK_TIP)
        self.exposure_lock_cb.pack(side="left", padx=(8, 0))
        r = self._row(matf, " ")
        self.viewport_look_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "ビューポートのルック", self.viewport_look_var,
                    _VIEWPORT_LOOK_TIP).pack(side="left")
        self.viewport_look_var.trace_add("write", lambda *a: self._sync_viewport_look())

        self._group(matf, "基本")
        self.beauty_var = tk.BooleanVar(master=self.root, value=True)
        self._check(matf, "Beauty", self.beauty_var,
                    "MRQ でレンダ（ビューポート露出＋シーケンサ品質）").pack(anchor="w")
        self.rlfull_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "Raw Lighting Full", self.rlfull_var,
                    "ライティングのみ素材（落ち影+シェーディング）。Sun+GI+Sky").pack(
            anchor="w")
        self.rldir_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "Raw Lighting Direct", self.rldir_var,
                    "GI/スカイライト/AO なしの直射のみ。追加レンダ1回").pack(anchor="w")

        self._group(matf, "ジオメトリ")
        self.depth_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "Z-Depth", self.depth_var, "手前=白 / 奥=黒").pack(anchor="w")
        r = ttk.Frame(matf)
        r.pack(fill="x", padx=(22, 0), pady=(0, 3))
        self.depth_bit_var = tk.StringVar(master=self.root, value="16bit PNG")
        ttk.Combobox(r, textvariable=self.depth_bit_var,
                     values=["8bit PNG", "16bit PNG", "EXR float"], state="readonly",
                     width=10).pack(side="left")
        ttk.Label(r, text="Near", style="Sub.TLabel").pack(side="left", padx=(8, 2))
        self.near_var = tk.StringVar(master=self.root, value="0")
        self._entry(r, self.near_var, 7).pack(side="left")
        ttk.Label(r, text="Far", style="Sub.TLabel").pack(side="left", padx=(8, 2))
        self.far_var = tk.StringVar(master=self.root, value="10000")
        self._entry(r, self.far_var, 8).pack(side="left")
        ttk.Label(r, text="cm", style="Sub.TLabel").pack(side="left", padx=(3, 0))
        # 自動: キャプチャ時に可視ジオメトリの最近/最遠を実測して Near/Far を決める
        # （測った値は欄に書き戻す＝結果が見える。OFF なら欄の手動値）
        self.depth_auto_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "自動", self.depth_auto_var,
                    "キャプチャ時に見えているジオメトリの最近/最遠を実測して Near/Far を"
                    "決める（測った値は欄に書き戻す）").pack(side="left", padx=(10, 0))
        r = ttk.Frame(matf)
        r.pack(fill="x")
        self.normal_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "Normal", self.normal_var,
                    "法線 RGB = XYZ*0.5+0.5。VDB雲の領域は黒").pack(side="left")
        self.normal_space_var = tk.StringVar(master=self.root, value="カメラ")
        self._tip(ttk.Combobox(r, textvariable=self.normal_space_var, state="readonly",
                               width=7, values=["カメラ", "ワールド"]),
                  "カメラ=正対面が青 / ワールド=上向きが青").pack(side="left", padx=(10, 0))
        self.objid_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "ObjectID", self.objid_var,
                    "Object ID targets を色分け・他は黒。色↔名前の JSON 付き").pack(
            anchor="w")

        self._group(matf, "マット")
        self.mfront_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "Matteの前", self.mfront_var,
                    "Beauty にマットをアルファとして焼いた RGBA（マット板より手前だけ）"
                    ).pack(anchor="w")
        self.behind_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "Matteの奥", self.behind_var,
                    "マット板の手前を取り除いて奥だけを描画（追加レンダ＋合成）").pack(
            anchor="w")
        self.cloudmatte_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "雲マット", self.cloudmatte_var,
                    "VDB雲=黒 / 背景=白。対象はレベル内の全VDB雲").pack(anchor="w")
        self.skymatte_var = tk.BooleanVar(master=self.root, value=False)
        self._check(matf, "空マット", self.skymatte_var,
                    "空=黒・大気光/雲は維持。PPマテリアル/ブルームOFFの追加レンダ1回"
                    ).pack(anchor="w")

        # ---- 詳細設定 ----
        advf = self._fold(body, "詳細設定（品質 / Overscan / VDB雲）")
        r = self._row(advf, "品質")
        ttk.Label(r, text="ウォームアップ").pack(side="left")
        self.mrq_warmup_var = tk.StringVar(master=self.root, value="32")
        self._entry(r, self.mrq_warmup_var, 5).pack(side="left", padx=(4, 12))
        ttk.Label(r, text="サンプリング").pack(side="left")
        self.mrq_ts_var = tk.StringVar(master=self.root, value="8")
        self._entry(r, self.mrq_ts_var, 5).pack(side="left", padx=(4, 12))
        ttk.Label(r, text="AA").pack(side="left")
        self.aa_var = tk.StringVar(master=self.root, value="2x")
        ttk.Combobox(r, textvariable=self.aa_var, values=["1x", "2x", "4x"],
                     state="readonly", width=4).pack(side="left", padx=(4, 0))
        # Overscan（ON のとき % か 直接ピクセルで余白を追加。元フレームは中央維持・全パス共通）
        r = self._row(advf, "Overscan")
        self.overscan_on_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "ON", self.overscan_on_var,
                    "画面の外側に余白を足してレンダする（元フレームは中央のまま・全素材共通）"
                    ).pack(side="left")
        self.overscan_mode_var = tk.StringVar(master=self.root, value="percent")
        ttk.Radiobutton(r, text="%", variable=self.overscan_mode_var,
                        value="percent").pack(side="left", padx=(10, 0))
        self.overscan_var = tk.StringVar(master=self.root, value="0")
        self._entry(r, self.overscan_var, 5).pack(side="left", padx=(2, 10))
        ttk.Radiobutton(r, text="px", variable=self.overscan_mode_var,
                        value="pixels").pack(side="left")
        ttk.Label(r, text="X", style="Sub.TLabel").pack(side="left", padx=(6, 2))
        self.overscan_x_var = tk.StringVar(master=self.root, value="0")
        self._entry(r, self.overscan_x_var, 5).pack(side="left")
        ttk.Label(r, text="Y", style="Sub.TLabel").pack(side="left", padx=(6, 2))
        self.overscan_y_var = tk.StringVar(master=self.root, value="0")
        self._entry(r, self.overscan_y_var, 5).pack(side="left")
        r = self._row(advf, "その他")
        self.mrq_camasp_var = tk.BooleanVar(master=self.root, value=True)
        self._check(r, "カメラのアスペクト", self.mrq_camasp_var).pack(side="left")
        # VDB雲モード: Auto=レベルに HV(VDB雲) があれば自動で雲対応（バッキングT/
        # 雲ObjectID/Behindの手前雲hide）。ON=検出に関わらず強制有効。
        # OFF=雲を完全無視した従来の書き出し。画像/映像タブ共通の1セレクタ。
        ttk.Label(r, text="VDB雲").pack(side="left", padx=(14, 4))
        self.vdb_var = tk.StringVar(master=self.root, value="Auto")
        self._tip(ttk.Combobox(r, textvariable=self.vdb_var, width=5, state="readonly",
                               values=("Auto", "ON", "OFF")),
                  "Auto=レベルに VDB雲があれば自動で雲対応 / ON=常に有効 / "
                  "OFF=雲を無視。画像・映像タブ共通").pack(side="left")
        ttk.Frame(body, style="Body.TFrame", height=6).pack()

    def _build_seq_tab(self, frm):
        """映像キャプチャ（シーケンスレンダ）タブ。設定は画像タブから独立していて、
        「設定をコピー」ボタンで画像タブの値を一括コピーできる。"""
        foot = ttk.Frame(frm, style="Body.TFrame")
        foot.pack(side="bottom", fill="x")
        # バックグラウンド（裏のエディタで撮る・待ち行列）
        bgr = ttk.Frame(foot, style="Body.TFrame")
        bgr.pack(fill="x", padx=4, pady=(6, 0))
        self.seq_bg_var = tk.BooleanVar(master=self.root, value=False)
        self._check(bgr, "バックグラウンドでキュー", self.seq_bg_var, _BG_TIP).pack(side="left")
        self._tip(ttk.Button(bgr, text="キューリスト", command=self._bg_open_window),
                  "キューリスト（順番・状態・中断/取り消し）を開く").pack(
            side="left", padx=(8, 0))
        self.bg_status_var = tk.StringVar(master=self.root, value="")
        ttk.Label(foot, textvariable=self.bg_status_var, style="Sub.TLabel",
                  wraplength=520, justify="left").pack(fill="x", padx=6)
        self.seq_btn = ttk.Button(foot, text="シーケンスレンダ", style="Accent.TButton",
                                  command=self._on_seq_render)
        self.seq_btn.pack(fill="x", padx=4, pady=(6, 4))
        body = self._scrollable(frm)
        ttk.Frame(body, style="Body.TFrame", height=8).pack()

        # ---- シーケンス ----
        seqf = self._card(body, "シーケンス")
        r = self._row(seqf, "シーケンス")
        self.seq_name_var = tk.StringVar(master=self.root, value="(未取得)")
        self._tip(ttk.Label(r, textvariable=self.seq_name_var, style="Val.TLabel"),
                  "Sequencer で開いているシーケンスをカメラカットでレンダする。"
                  "fps はシーケンスの Display Rate").pack(side="left")
        ttk.Button(r, text="⟳", width=3, command=self._refresh_sequence).pack(
            side="left", padx=(8, 0))
        r = self._row(seqf, "範囲")
        self.seq_range_mode = tk.StringVar(master=self.root, value="auto")
        ttk.Radiobutton(r, text="全体", variable=self.seq_range_mode,
                        value="auto").pack(side="left")
        ttk.Radiobutton(r, text="指定", variable=self.seq_range_mode,
                        value="custom").pack(side="left", padx=(12, 0))
        self.seq_start_var = tk.StringVar(master=self.root, value="0")
        self._entry(r, self.seq_start_var, 6).pack(side="left", padx=(6, 2))
        ttk.Label(r, text="〜").pack(side="left")
        self.seq_end_var = tk.StringVar(master=self.root, value="0")
        self._tip(self._entry(r, self.seq_end_var, 6),
                  "終了フレームも含めてレンダする").pack(side="left", padx=2)
        r = self._row(seqf, "解像度")
        self.seq_w_var = tk.StringVar(master=self.root, value="1920")
        self._entry(r, self.seq_w_var, 6).pack(side="left", padx=(0, 2))
        ttk.Label(r, text="×").pack(side="left")
        self.seq_h_var = tk.StringVar(master=self.root, value="1080")
        self.seq_h_entry = self._entry(r, self.seq_h_var, 6)
        self.seq_h_entry.pack(side="left", padx=2)
        # カメラカットのカメラの aspect_ratio（Cine は filmback 由来・素のカメラは
        # プロパティ値）から高さを自動算出する。ON の間は高さ入力を無効化。
        self.seq_camasp_var = tk.BooleanVar(master=self.root, value=True)
        self._check(r, "カメラのアスペクト", self.seq_camasp_var,
                    "カメラカットのカメラのアスペクト比から高さを自動で決める",
                    command=self._sync_seq_h).pack(side="left", padx=(10, 0))
        self.seq_w_var.trace_add("write", lambda *a: self._sync_seq_h())

        # ---- 出力先 ----
        outf = self._card(body, "出力先")
        r = self._row(outf, "フォルダ")
        default_dir = os.path.normpath(
            os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
        self.seq_out_var = tk.StringVar(master=self.root, value=default_dir)
        self._tip(self._entry(r, self.seq_out_var, 28), _OUT_DIR_TIP).pack(
            side="left", fill="x", expand=True)
        ttk.Button(r, text="…", width=3,
                   command=lambda: self._browse(self.seq_out_var)).pack(
            side="left", padx=(6, 0))
        r = self._row(outf, "ファイル名")
        self.seq_name_tpl_var = tk.StringVar(master=self.root, value=core.DEFAULT_NAME_TEMPLATE)
        e = self._entry(r, self.seq_name_tpl_var, 26)
        e.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self._tip(e, _NAME_TIP)
        self.seq_overwrite_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "上書き", self.seq_overwrite_var, _OVERWRITE_TIP).pack(side="left")
        self.seq_name_preview_var = tk.StringVar(master=self.root, value="")
        ttk.Label(outf, textvariable=self.seq_name_preview_var,
                  style="Sub.TLabel").pack(anchor="w", padx=(80, 0))
        self.seq_name_tpl_var.trace_add("write", lambda *a: self._update_name_preview())
        r = self._row(outf, " ")
        self.seq_subdir_var = tk.BooleanVar(master=self.root, value=True)
        self._check(r, "テイク毎のサブフォルダに出力", self.seq_subdir_var,
                    "OFF にすると指定フォルダへ直接書き出す").pack(side="left")

        # ---- 出力素材 ----
        # 画像タブと同じ並び: 形式 → Fog/露出 → 素材。連番の形式（Beauty / Raw Lighting）と
        # MP4 レートを 1 行目に、Fog/露出固定/ビューポートのルックを 2 行目に置く
        matf = self._card(body, "出力素材")
        r = self._row(matf, "形式")
        self.seq_beauty_fmt_var = tk.StringVar(master=self.root, value="PNG 8bit")
        self._tip(ttk.Combobox(r, textvariable=self.seq_beauty_fmt_var, state="readonly",
                               width=16,
                               values=["PNG 8bit", "JPG 8bit", "EXR 16bit (float)"]),
                  "連番の形式（Beauty / Raw Lighting）。Matte系合成・ObjectID・各マットは"
                  "常に PNG。MP4 は形式によらず内部 PNG からエンコード").pack(side="left")
        ttk.Label(r, text="MP4", style="Sub.TLabel").pack(side="left", padx=(14, 4))
        self.seq_rate_var = tk.StringVar(master=self.root, value="高 (CRF 20)")
        self._tip(ttk.Combobox(r, textvariable=self.seq_rate_var, state="normal",
                               width=13, values=list(_MP4_RATE_PRESETS.keys())),
                  "H.264 の CRF。数値を直接入力してもよい（小さいほど高品質）。"
                  "MP4 はシーケンスの fps で ffmpeg エンコード").pack(side="left")
        r = self._row(matf, " ")
        self.seq_fog_var = tk.BooleanVar(master=self.root, value=False)
        self.seq_fog_cb = self._check(r, "Fog/グローなし", self.seq_fog_var,
                                      "フォグとポスプロのグロー(ブルーム)を切ってレンダする")
        self.seq_fog_cb.pack(side="left")
        self.seq_exposure_lock_var = tk.BooleanVar(master=self.root, value=False)
        self.seq_exposure_lock_cb = self._check(r, "露出固定", self.seq_exposure_lock_var,
                                                _EXPOSURE_LOCK_TIP)
        self.seq_exposure_lock_cb.pack(side="left", padx=(8, 0))
        self.seq_no_mblur_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "モーションブラーなし", self.seq_no_mblur_var,
                    _NO_MBLUR_TIP).pack(side="left", padx=(8, 0))
        self.seq_viewport_look_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "ビューポートのルック", self.seq_viewport_look_var,
                    _SEQ_VIEWPORT_LOOK_TIP).pack(side="left", padx=(8, 0))
        self.seq_viewport_look_var.trace_add("write", lambda *a: self._sync_viewport_look())

        mtx = ttk.Frame(matf)
        mtx.pack(fill="x", pady=(4, 0))
        mtx.columnconfigure(3, weight=1)
        labels = {k: lab for k, lab, _p in _SEQ_OUTPUTS}
        self.seq_out_vars = {}
        mrow = 0
        for gi, (gname, keys) in enumerate(_SEQ_GROUPS):
            ttk.Label(mtx, text=gname, style="Group.TLabel").grid(
                row=mrow, column=0, sticky="w", pady=(8 if gi else 0, 2))
            if gi == 0:
                ttk.Label(mtx, text="連番", style="Sub.TLabel").grid(
                    row=mrow, column=1, padx=8)
                ttk.Label(mtx, text="MP4", style="Sub.TLabel").grid(
                    row=mrow, column=2, padx=8)
            mrow += 1
            for key in keys:
                lab = ttk.Label(mtx, text=_SEQ_SHORT.get(key, labels[key]))
                lab.grid(row=mrow, column=0, sticky="w", pady=1)
                if key in _SEQ_TIPS:
                    self._tip(lab, _SEQ_TIPS[key])
                pv = tk.BooleanVar(master=self.root, value=(key == "beauty"))
                mv = tk.BooleanVar(master=self.root, value=(key == "beauty"))
                ttk.Checkbutton(mtx, variable=pv).grid(row=mrow, column=1)
                ttk.Checkbutton(mtx, variable=mv).grid(row=mrow, column=2)
                self.seq_out_vars[key] = (pv, mv)
                if key == "depth":
                    depf = ttk.Frame(mtx)
                    # ビット深度（画像タブと同じ 3 択）。8bit は Beauty と同一ジョブの
                    # PP パス、16bit PNG / EXR float はフレーム毎の SceneCapture 実測
                    self.seq_depth_bit_var = tk.StringVar(master=self.root, value="8bit PNG")
                    self._tip(ttk.Combobox(depf, textvariable=self.seq_depth_bit_var,
                                           values=["8bit PNG", "16bit PNG", "EXR float"],
                                           state="readonly", width=10),
                              "8bit PNG=Beauty と同一ジョブの PP パス（風で揺れる前景も画素整合）/ "
                              "16bit PNG・EXR float=フレーム毎に SceneCapture で実測した厳密リニア"
                              "（EXR は生 cm。MP4 と雲抜きは無し）").pack(side="left", padx=(0, 8))
                    ttk.Label(depf, text="Near", style="Sub.TLabel").pack(side="left")
                    self.seq_near_var = tk.StringVar(master=self.root, value="0")
                    self._entry(depf, self.seq_near_var, 7).pack(side="left", padx=(2, 0))
                    ttk.Label(depf, text="Far", style="Sub.TLabel").pack(
                        side="left", padx=(8, 2))
                    self.seq_far_var = tk.StringVar(master=self.root, value="10000")
                    self._entry(depf, self.seq_far_var, 8).pack(side="left")
                    ttk.Label(depf, text="cm", style="Sub.TLabel").pack(
                        side="left", padx=(3, 0))
                    # 自動: 先頭/中間/終端フレームをサンプリングして全編を包む
                    # Near/Far を実測（測った値は欄へ書き戻し）
                    self.seq_depth_auto_var = tk.BooleanVar(master=self.root, value=False)
                    self._check(depf, "自動", self.seq_depth_auto_var,
                                "先頭/中間/終端フレームを測って全編を包む Near/Far を決める"
                                "（測った値は欄に書き戻す）").pack(side="left", padx=(8, 0))
                    depf.grid(row=mrow, column=3, sticky="w", padx=(10, 0))
                elif key == "normal":
                    self.seq_normal_space_var = tk.StringVar(master=self.root, value="カメラ")
                    self._tip(ttk.Combobox(mtx, textvariable=self.seq_normal_space_var,
                                           state="readonly", width=7,
                                           values=["カメラ", "ワールド"]),
                              "カメラ=正対面が青 / ワールド=上向きが青").grid(
                        row=mrow, column=3, sticky="w", padx=(10, 0))
                mrow += 1

        # ---- 詳細設定 ----
        advf = self._fold(body, "詳細設定（品質 / VDB雲 / 画像タブからコピー）")
        r = self._row(advf, "品質")
        ttk.Label(r, text="ウォームアップ").pack(side="left")
        self.seq_warm_var = tk.StringVar(master=self.root, value="32")
        self._entry(r, self.seq_warm_var, 5).pack(side="left", padx=(4, 12))
        ttk.Label(r, text="サンプリング").pack(side="left")
        self.seq_ts_var = tk.StringVar(master=self.root, value="8")
        self._entry(r, self.seq_ts_var, 5).pack(side="left", padx=(4, 12))
        ttk.Label(r, text="VDB雲").pack(side="left")
        self._tip(ttk.Combobox(r, textvariable=self.vdb_var, width=5, state="readonly",
                               values=("Auto", "ON", "OFF")),
                  "Auto=レベルに VDB雲があれば自動で雲対応 / ON=常に有効 / "
                  "OFF=雲を無視。画像・映像タブ共通").pack(side="left", padx=(4, 0))
        r = self._row(advf, "その他")
        self.seq_matte_hide_var = tk.BooleanVar(master=self.root, value=False)
        self._check(r, "Matte 対象を隠す", self.seq_matte_hide_var,
                    "クリーンプレート用。Matte系の出力を選んだときは自動で隠れる").pack(
            side="left")
        r = self._row(advf, " ")
        self._tip(ttk.Button(r, text="画像タブの設定をコピー",
                             command=self._transfer_from_image_tab),
                  "解像度 / 出力先 / 任意名 / 形式 / 品質 / Depth / Fog / ルックを画像タブからコピーする"
                  ).pack(side="left")
        ttk.Frame(body, style="Body.TFrame", height=6).pack()

    def _transfer_from_image_tab(self):
        """画像キャプチャタブの設定（解像度/出力先/任意名/形式/品質/Depth/Fog/ルック）を映像タブへコピー。"""
        W = self._int_var(self.w_var, 1920)
        if self.mrq_camasp_var.get():
            asp = self._aspect_ratio()
            H = int(round(W / asp)) if asp > 0.1 else self._int_var(self.h_var, 1080)
        else:
            H = self._int_var(self.h_var, 1080)
        self.seq_w_var.set(str(W))
        self.seq_h_var.set(str(H))
        self.seq_out_var.set(self.out_var.get())
        self.seq_name_tpl_var.set(self.name_tpl_var.get())
        self.seq_overwrite_var.set(self.overwrite_var.get())
        self.seq_warm_var.set(self.mrq_warmup_var.get())
        self.seq_ts_var.set(self.mrq_ts_var.get())
        self.seq_near_var.set(self.near_var.get())
        self.seq_far_var.set(self.far_var.get())
        self.seq_depth_bit_var.set(self.depth_bit_var.get())
        self.seq_beauty_fmt_var.set(self.beauty_fmt_var.get())
        self.seq_normal_space_var.set(self.normal_space_var.get())
        self.seq_fog_var.set(self.fog_off_var.get())
        self.seq_exposure_lock_var.set(self.exposure_lock_var.get())
        self.seq_viewport_look_var.set(self.viewport_look_var.get())
        self.status_var.set("画像キャプチャの設定を映像タブへ転送しました")

    def _sync_viewport_look(self):
        """「ビューポートのルック」と Fog/グローなし・露出固定の排他。ON にしたタブの
        2 チェックを外して無効化し、OFF で有効に戻す（値は戻さない）。"""
        for vp, pairs in (
                ("viewport_look_var", (("fog_off_var", "fog_off_cb"),
                                       ("exposure_lock_var", "exposure_lock_cb"))),
                ("seq_viewport_look_var", (("seq_fog_var", "seq_fog_cb"),
                                           ("seq_exposure_lock_var", "seq_exposure_lock_cb")))):
            vpv = getattr(self, vp, None)
            if vpv is None:
                continue
            on = bool(vpv.get())
            for var_name, cb_name in pairs:
                var, cb = getattr(self, var_name, None), getattr(self, cb_name, None)
                if var is None or cb is None:
                    continue
                if on and var.get():
                    var.set(False)
                cb.state(["disabled"] if on else ["!disabled"])

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
        init = var.get() or "/"
        if "{" in init:      # トークン入りはトークンより前の実在フォルダから選ぶ
            init = os.path.dirname(init.split("{", 1)[0]) or "/"
        # parent を明示しないと tkinter._default_root（別ツールの withdraw 済み窓の
        # こともある）が親になり、ダイアログがエディタの背後に隠れて固まって見える
        d = filedialog.askdirectory(parent=self.root, initialdir=init)
        if d:
            var.set(os.path.normpath(d))

    # ---- 多重実行ガード（レンダ・後処理・MP4 エンコードの間は両ボタンを無効化） ----
    def _is_busy(self):
        return bool(getattr(unreal, "_ue5capture_busy", False))

    def _set_busy(self, on):
        """チェーン開始〜完了（_finalize / _final）までのフラグ。ハンドラ内の
        root.update() が溜まったクリックを発火させても再入しないようにする。
        reload をまたいで残るよう unreal モジュール上に持つ。"""
        unreal._ue5capture_busy = bool(on)
        for b in (getattr(self, "capture_btn", None), getattr(self, "seq_btn", None)):
            if b is None:
                continue
            try:
                b.state(["disabled"] if on else ["!disabled"])
            except Exception:
                pass
        if not on:
            # 無効化中に溜まったクリックを捨ててから有効化する
            try:
                self.root.update()
            except Exception:
                pass

    def _run_guarded(self, impl):
        """ハンドラ本体 impl() を busy ガード付きで実行する。impl は非同期チェーンを
        開始したら True を返す（完了側の _finalize/_final が busy を下ろす）。
        False/例外なら即座に busy を下ろし、登録済みの復元フックを実行する。"""
        import capture_mrq
        if self._is_busy():
            self.status_var.set("実行中です。完了までお待ちください。")
            return
        # reload はレンダ中に行うと実行中ジョブの executor/復元コールバックを
        # 破壊する（シーンが隠れたまま残る）。必ず busy チェックを先に行う。
        # 5.8 では is_rendering() がレンダ中でも False を返す実測があるため
        # _KEEP の executor も併せて見る。
        if (unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem).is_rendering()
                or capture_mrq._KEEP.get("executor") is not None):
            self.status_var.set("MRQ は既にレンダリング中です。完了までお待ちください。")
            return
        importlib.reload(capture_mrq)
        self._restore_hooks = []
        self._set_busy(True)
        started = False
        try:
            started = bool(impl())
        except Exception as e:
            self.status_var.set("エラー: %s" % e)
            unreal.log_error("[SceneCapture] ハンドラ例外: %s" % e)
            raise
        finally:
            if not started:
                self._run_restore_hooks()
                self._set_busy(False)

    def _run_restore_hooks(self):
        """途中で登録された復元処理（Overscan filmback / PropagateAlpha 等）を実行する。
        正常完了時は _finalize/_final が自分で復元してから空にする。"""
        hooks = getattr(self, "_restore_hooks", None) or []
        self._restore_hooks = []
        for h in hooks:
            try:
                h()
            except Exception as e:
                unreal.log_warning("[SceneCapture] 復元フック失敗: %s" % e)

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
        # 起動時の再適用はパス失効時にラベルで解決している（_pick_targets_resolved）ので、
        # 復元側も同じ規則で解決しないと、ラベル経由で無効化した対象が戻らない
        p2l = self._path2label()
        labels = self.matte_pick.get("labels", {}) if hasattr(self, "matte_pick") else {}
        names = [pth if pth in p2l else (labels.get(pth) or pth) for pth in paths]
        actors = core._resolve_target_actors(None, names or None)
        if not actors:
            return
        prims, _vols = core.split_volumetric_targets(actors)
        if not prims:
            return
        n = core.set_matte_shadow_occlusion(prims, True)
        m = core.restore_matte_materials(prims)
        self.status_var.set("マット対象を復元: 影/AO ON %d comp・マテリアル復元 %d slot" % (n, m))

    def _on_mrq(self):
        """Movie Render Queue で Beauty を高品質レンダ（非同期・PIE）。
        本体は _on_mrq_impl。busy ガード / capture_mrq の reload / 失敗時の復元は
        _run_guarded が担う。"""
        self._run_guarded(self._on_mrq_impl)

    def _on_mrq_impl(self):
        """戻り値: 非同期チェーンを開始したら True（完了側の _finalize が後始末）。"""
        import capture_mrq
        _apply_exposure_lock(self.exposure_lock_var.get())
        cam = self._current_camera()
        if cam is None:
            self.status_var.set("MRQ: カメラが選択されていません")
            return False
        out = self._expand_out_dir(self.out_var.get().strip(), cam, self._current_sequence())
        if not out:
            self.status_var.set("MRQ: 出力先を指定してください")
            return False
        if not os.path.isdir(out):
            try:
                os.makedirs(out)
            except Exception as e:
                self.status_var.set("MRQ: 出力先を作成できません: %s" % e)
                return False
        # クリック時点の値を確定する（レンダ中にウィジェットを触っても、後続ジョブや
        # 後処理が途中から別の値を読まないように）
        fog_off = self.fog_off_var.get()
        vp_look = self.viewport_look_var.get()
        keep_beauty0 = self.beauty_var.get()
        W = self._int_var(self.w_var, 1920)
        if self.mrq_camasp_var.get():
            asp = core.get_camera_settings(cam).get("aspect_ratio", 0.0)
            H = int(round(W / asp)) if asp > 0.1 else 1080
        else:
            H = self._int_var(self.h_var, 1080)
        if W < 16 or H < 16:
            self.status_var.set("MRQ: 解像度が不正です (%d×%d)" % (W, H))
            return False
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
        warm = max(1, self._int_var(self.mrq_warmup_var, 32))
        ts = max(1, self._int_var(self.mrq_ts_var, 8))
        auto_rng_failed = False
        if self.depth_var.get() and self.depth_auto_var.get():
            # Depth 自動レンジ: 現在フレームの可視ジオメトリの最近/最遠を実測して
            # Near/Far を決定（値は欄へ書き戻す。失敗時は欄の手動値のまま）。
            # _collect_settings() が欄を読む前に行う（16bit PNG / EXR の SceneCapture
            # 深度は settings の Near/Far で正規化されるため）
            rng = None
            try:
                rng = core.measure_depth_range(cam)
            except Exception as e:
                unreal.log_warning("[SceneCapture] Depth自動レンジ失敗: %s" % e)
            if rng:
                self.near_var.set("%g" % rng[0])
                self.far_var.set("%g" % rng[1])
            else:
                auto_rng_failed = True
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
                except Exception as e:
                    unreal.log_warning("[SceneCapture] filmback 復元失敗: %s" % e)
        # 途中で例外が出ても _run_guarded / _finalize が必ず戻す
        self._restore_hooks.append(_restore_fb)
        # 名前本体（テンプレート解決）と通し番号。通常は次の番号を振り、設定違いを上書きしない。
        # 「上書き」ON ではこの名前の最新テイクの既存出力を消して同じ番号で出し直す。
        name_body = self._resolve_name(self.name_tpl_var.get(), cam, self._current_sequence())
        if self.overwrite_var.get():
            suf = "%03d" % (core.latest_take_number(out, name_body) or 1)
            removed = core.delete_take_outputs(out, name_body, suf)
            if removed:
                unreal.log("[SceneCapture] 上書き: %s テイク %s の既存出力 %d 件を削除"
                           % (name_body, suf, removed))
        else:
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
        s.name_body = name_body
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
        if auto_rng_failed:
            skip_notes.append("Depth自動レンジ: 測定失敗（手動値を使用）")
        if self.depth_var.get() and s.depth_far <= s.depth_near:
            skip_notes.append("Z-Depth: Far が Near 以下のため既定値 (0〜10000cm) を使用")
            s.depth_near, s.depth_far = 0.0, 10000.0
            self.near_var.set("0")
            self.far_var.set("10000")
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
            skip_notes.append("データパス(Depth/ObjectID)出力でエラー")
            self.status_var.set("データパス出力でエラー: %s" % e)
            unreal.log_error("[SceneCapture] データパス出力でエラー: %s" % e)

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
        # Matte 系合成は PIL で読める画像が必要。EXR / JPG のときは PNG も内部出力して
        # 使う（Depth / Normal / Matte の PP パスは PNG で受け取る前提のため）。
        need_comp = want_mfront or want_behind
        aux_png = (img_fmt != "png") and (need_comp or depth_pp_mat is not None
                                          or normal_pp_mat is not None)
        comp_beauty = (os.path.join(out, _name("Beauty") + ".png")
                       if img_fmt != "png" else beauty_path)
        # ビューポートのルック: Beauty はビューポート撮影（経路A）。EXR は撮れないので
        # 最初から MRQ 近似（経路B）。撮影結果は一時名で受け、チェーン完了時に Beauty 名へ
        vp_try = vp_look and keep_beauty0 and (
            img_fmt == "png" or (img_fmt == "jpg" and core._HAS_PIL))
        # ビューポート撮影が見た目どおりなのは、ビューポートがすでにこのカメラを見続けている時だけ。
        # その場でパイロットして数秒で撮ると、ボリュメトリックフォグ/Lumen の履歴が育っておらず
        # 灰色で光の筋の無い画になる（森 047 実測: パイロット2秒後 vs 見続けた後で −0.6EV・差 11.6/255）。
        # パイロットしていなければ MRQ 近似で撮る
        if vp_try and not core.viewport_pilots(cam):
            vp_try = False
            skip_notes.append("ビューポートのルック: ビューポートがこのカメラをパイロットしていないため MRQ 近似で撮影")
        vp = {"ok": False, "tmp": os.path.join(out, _VPLOOK_TMP)}
        try:
            # 前回の撮影がタイムアウト後に書いた一時ファイルの残り
            if os.path.isfile(vp["tmp"]):
                os.remove(vp["tmp"])
        except OSError:
            pass
        if vp_look and keep_beauty0 and not (img_fmt == "png"
                                             or (img_fmt == "jpg" and core._HAS_PIL)):
            skip_notes.append("ビューポートのルック: %s は MRQ 近似で撮影" % img_fmt.upper())

        def _place_vp_beauty():
            if vp["ok"] and os.path.isfile(vp["tmp"]):
                try:
                    core.finish_viewport_grab_file(vp["tmp"], beauty_path)
                except Exception as e:
                    skip_notes.append("ビューポートのルック: Beauty の保存に失敗")
                    unreal.log_warning("[SceneCapture] ビューポート撮影の保存失敗: %s" % e)

        # Beauty（MRQ）は Beauty 指定時・Matte 系合成・同一ジョブ深度・Raw Lighting Full
        # が要るときにレンダする（Raw Lighting Direct はメイン無しでも専用ジョブで出せる）
        beauty_needed = (keep_beauty0 or want_mfront or want_behind
                         or depth_pp_mat is not None or normal_pp_mat is not None
                         or want_rlfull)
        if (not beauty_needed and not want_rldir and not objid_cloud_vols
                and not self.cloudmatte_var.get() and not self.skymatte_var.get()):
            msg = ("完了（データパスのみ出力）" if (s.do_depth or s.do_object_id)
                   else "出力が選ばれていません")
            if skip_notes:
                msg += "（%s）" % " / ".join(skip_notes)
            self.status_var.set(msg)
            return False

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
            pa0 = unreal.SystemLibrary.get_console_variable_int_value(
                "r.PostProcessing.PropagateAlpha")

            def _restore_pa():
                unreal.SystemLibrary.execute_console_command(
                    None, "r.PostProcessing.PropagateAlpha %d" % int(pa0))
            self._restore_hooks.append(_restore_pa)
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
                # 傾いた板の手前（near clip より奥）に残る物もアクター単位で隠す
                front = core.actors_in_front_of_matte(mt_p, [cs_cam])
                jobs.append(dict(hidden=list(mt_p) + front_vols + front,
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

        def _fail_finish(msg):
            """チェーンを途中で打ち切る: 一時材の削除・filmback/PropagateAlpha の復元・
            露出固定の解除・busy 解除をまとめて行う。"""
            try:
                if matte_mat_still is not None:
                    core.delete_temp_matte_material()
                if depth_pp_mat is not None:
                    core.delete_temp_depth_material()
                if normal_pp_mat is not None:
                    core.delete_temp_normal_material()
                if geomask_mat is not None:
                    core.delete_temp_geomask_material()
            except Exception as e:
                unreal.log_warning("[SceneCapture] 一時マテリアル削除失敗: %s" % e)
            _place_vp_beauty()      # 撮影済みの Beauty は MRQ 側の失敗でも残す
            self._run_restore_hooks()
            _apply_exposure_lock(False)
            self._set_busy(False)
            self.status_var.set(msg)

        def _finalize():
            """後処理の本体 _finalize_body が例外で抜けても、復元と busy 解除は必ず行う。"""
            try:
                _finalize_body()
            except Exception as e:
                skip_notes.append("後処理エラー")
                unreal.log_error("[SceneCapture] 後処理エラー: %s" % e)
            finally:
                _place_vp_beauty()
                self._run_restore_hooks()       # filmback / PropagateAlpha
                _apply_exposure_lock(False)     # 露出固定の cvar を他スクリプトへ持ち越さない
                self._set_busy(False)
            self.status_var.set("完了" if not skip_notes
                                else "完了（%s）" % " / ".join(skip_notes))

        def _finalize_body():
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
            # ビューポート撮影の Beauty があるときメインの MRQ Beauty は内部素材（後で置換）
            keep_beauty = (keep_beauty0 and not vp["ok"]) or bool(skip_notes)
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
            # 白黒変換前に行う）
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
                        core.apply_cloud_black(_p, cm_png)
                    except Exception as e:
                        skip_notes.append("%s: 雲抜き失敗" % _lbl)
                        self.status_var.set("%s 雲抜き失敗: %s" % (_lbl, e))
            # Normal のマット対象を黒に落とす。板マスク単体（Beauty_Matte）を使う:
            # 雲は直前の雲抜きが _CLOUD_MUL_POW の強度込みで担当しているので、
            # 雲αを統合した merged を掛けると雲領域が純黒に上書きされてしまう。
            _mesh_matte = os.path.join(out, _name("Beauty") + "_Matte.png")
            _normal_png = os.path.join(out, _name("Normal") + ".png")
            if (normal_pp_mat is not None and os.path.isfile(_mesh_matte)
                    and os.path.isfile(_normal_png)):
                try:
                    core.apply_matte_black(_normal_png, _mesh_matte)
                except Exception as e:
                    skip_notes.append("Normal: マット黒塗り失敗")
                    self.status_var.set("Normal マット黒塗り失敗: %s" % e)
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
            # H5 の合成ソース: 完成品が出たら削除（ユーザー指定 2026-08-27）。
            # 合成失敗時のみ診断・手動再合成用に _cloudsrc/ へ退避して残す
            if cloud_alpha_needed:
                srcs = (_name("CloudMatte") + ".exr",
                        _name("CloudMatte") + "_GeoMask.exr",
                        _name("CloudVeil") + ".exr",
                        _name("CloudVeil0") + ".exr",
                        _name("CloudBG") + ".png")
                if cloud_applied:
                    for _sf in srcs:
                        _p = os.path.join(out, _sf)
                        if os.path.isfile(_p):
                            try:
                                os.remove(_p)
                            except Exception:
                                pass
                else:
                    srcdir = os.path.join(out, "_cloudsrc")
                    try:
                        os.makedirs(srcdir, exist_ok=True)
                    except Exception:
                        pass
                    for _sf in srcs:
                        _p = os.path.join(out, _sf)
                        if os.path.isfile(_p):
                            try:
                                os.replace(_p, os.path.join(srcdir, _sf))
                            except Exception:
                                pass
                    skip_notes.append("雲マット: 合成失敗のためソースを _cloudsrc に残置")
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

        def _run_jobs():
            if not jobs:
                _finalize()
                return
            j = jobs.pop(0)

            def _jdone(ok, od, _j=j):
                nonlocal cloud_matte_path
                if not ok and (_j.get("composite") or _j.get("sky")):
                    # Behind プレート / 空マットは下に専用の失敗分岐が無いのでここで注記
                    skip_notes.append("%s: レンダ失敗" % (
                        "Matteの奥" if _j.get("composite") else "空マット"))
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
                            skip_notes.append("Matteの前: 合成エラー")
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
                                skip_notes.append("Matteの前: 合成エラー")
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
                        # カメラはクリック時に確定した cam（レンダ中にコンボを触っても
                        # 別カメラで合成しない）
                        inter = os.path.join(out, _j["base"] + ".png")
                        core.composite_behind_in_matte(
                            core._get_editor_world(), core.get_camera_settings(cam),
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
                        skip_notes.append("Matteの奥: 合成エラー")
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
                                          fog_off=(True if j.get("sky") else fog_off),
                                          scene_sequence=scene_seq,
                                          scene_frame=scene_frame, on_done=_jdone)
            except Exception as e:
                # 1 本の起動失敗でキューの残りを捨てない（注記して次のジョブへ）。
                # バッキングが起動できなかった場合は板のみで Matteの前を確定させる
                skip_notes.append("追加ジョブ起動失敗 (%s)" % j["base"])
                self.status_var.set("追加 MRQ 失敗: %s" % e)
                unreal.log_warning("[SceneCapture] 追加ジョブ起動失敗 %s: %s" % (j["base"], e))
                if j.get("cloud_kind") == "backing_w" and want_mfront:
                    try:
                        _compose_mfront()
                    except Exception as e2:
                        skip_notes.append("Matteの前: 合成エラー")
                        unreal.log_warning("[SceneCapture] Matteの前 合成エラー: %s" % e2)
                _run_jobs()

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
                    unreal.log_error("[SceneCapture] Beauty 後処理エラー: %s" % e)
                    _fail_finish("Beauty 後処理でエラー: %s" % e)
                    return
                _run_jobs()
                return
            # od は出力フォルダ（on_done の契約は (success, output_dir)）であって原因ではない
            _fail_finish("MRQ Beauty レンダ失敗（Output Log 参照）")

        def _launch():
            """メイン MRQ ジョブ（Beauty+同一ジョブの PP パス）以降のチェーンを開始する。
            ビューポート撮影で Beauty が撮れていればメインは PP パス等が要るときだけ回す。"""
            main_needed = beauty_needed
            if vp["ok"]:
                main_needed = (want_mfront or want_behind or depth_pp_mat is not None
                               or normal_pp_mat is not None or want_rlfull)
            if not main_needed:
                # Raw Lighting Direct のみ / ビューポート撮影の Beauty のみ:
                # メインジョブ無しで専用ジョブだけ回す
                _run_jobs()
                return True

            # 静止画（時間凍結）の TS>1 は、同一ジョブの Matte/Depth PP パスが
            # サブフレームの片方にしか写らず 50% に希釈される（TS=2 でマスク黒が
            # sRGB(0.5)=187 になる実測）。凍結静止画はサブフレーム内容が同一で
            # TS>1 に画質上の意味も無いため、PP パスがあるときは TS=1 に強制する。
            ts_main = ts
            if ts > 1 and (matte_mat_still is not None or depth_pp_mat is not None
                           or normal_pp_mat is not None):
                ts_main = 1
                skip_notes.append("Matte/Depth/Normal 同一ジョブパスのため TS=1 で実行")

            vp_mrq = vp_look and keep_beauty0 and not vp["ok"]
            if vp_mrq:
                unreal.log("[SceneCapture] ビューポートのルック: 経路B（MRQ 近似）で Beauty をレンダ")
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
                                          fog_off=fog_off,
                                          scene_sequence=scene_seq,
                                          scene_frame=scene_frame, on_done=_after_beauty,
                                          viewport_look=vp_mrq)
            except Exception as e:
                _fail_finish("MRQ 起動失敗: %s" % e)
                return False
            return True

        if not vp_try:
            return _launch()

        def _grab_done(ok, err):
            vp["ok"] = ok
            if ok:
                unreal.log("[SceneCapture] ビューポートのルック: 経路A（ビューポート撮影）で Beauty を取得 %d×%d"
                           % (W, H))
            else:
                skip_notes.append("ビューポートのルック: ビューポート撮影に失敗し MRQ 近似で撮影")
                unreal.log_warning("[SceneCapture] ビューポートのルック: ビューポート撮影失敗（%s）"
                                   "→ 経路B（MRQ 近似）へ" % err)
            _launch()

        self.status_var.set("ビューポートのルック: ビューポートで Beauty を撮影中…")
        self.root.update()
        self._viewport_grab(cam, W, H, vp["tmp"], beauty_hidden, _grab_done)
        return True

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
        """プレイヘッドを frame に合わせ、その時刻のカメラカットに束縛されたカメラ
        アクターを返す。frame を含むカメラカットセクションを選び、その binding を
        LevelSequenceEditorBlueprintLibrary.get_bound_objects で解決する（開いている
        Sequencer 経由なのでスポーナブルも返る）。解決できなければ従来どおり
        全カットのカメラ → レベル内の先頭カメラへフォールバック（警告ログ付き）。"""
        try:
            unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(int(frame))
        except Exception as e:
            unreal.log_warning("[SceneCapture] スクラブ失敗 (F%d): %s" % (frame, e))
        cam = self._camera_cut_camera_for_frame(seq, int(frame))
        if cam is not None:
            return cam
        try:
            import capture_mrq
            cams = capture_mrq._camera_cut_camera_actors(seq, core._get_editor_world())
            if cams:
                if len(cams) > 1:
                    unreal.log_warning("[SceneCapture] F%d のカメラカットを解決できず、"
                                       "先頭カットのカメラ %s を使用" % (frame, cams[0].get_actor_label()))
                return cams[0]
        except Exception:
            pass
        cams = core.list_cameras()
        if cams:
            unreal.log_warning("[SceneCapture] F%d のカメラカットを解決できず、レベル内の"
                               "カメラ %s を使用" % (frame, cams[0].get_actor_label()))
        return cams[0] if cams else None

    @staticmethod
    def _camera_cut_camera_for_frame(seq, frame):
        """frame を含むカメラカットセクションのカメラアクター（無ければ None）。"""
        try:
            tracks = unreal.MovieSceneSequenceExtensions.find_tracks_by_exact_type(
                seq, unreal.MovieSceneCameraCutTrack)
        except Exception:
            return None
        for tr in list(tracks or []):
            try:
                sections = list(tr.get_sections())
            except Exception:
                continue
            for sec in sections:
                try:
                    if not (sec.get_start_frame() <= frame < sec.get_end_frame()):
                        continue
                    bid = sec.get_camera_binding_id()
                    objs = unreal.LevelSequenceEditorBlueprintLibrary.get_bound_objects(bid)
                except Exception:
                    continue
                for o in list(objs or []):
                    if isinstance(o, unreal.CameraActor):
                        return o
        return None

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
        self._update_name_preview()

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
        if not getattr(self, "_headless", False) and self.seq_bg_var.get():
            self._bg_enqueue_seq()
            return
        self._run_guarded(self._on_seq_render_impl)

    # ---- バックグラウンド（裏のエディタ・待ち行列。capture_bg / capture_bg_worker） ----
    def _bg_qdir(self):
        import capture_bg
        return capture_bg.queue_dir(
            unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()))

    def _bg_start_worker(self):
        import capture_bg
        engine = unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir())
        uproject = unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())
        pid = capture_bg.launch_worker(self._bg_qdir(), capture_bg.default_engine_exe(engine),
                                       os.path.normpath(uproject), capture_bg.worker_script_path())
        unreal.log("[SceneCapture/BG] ワーカー起動 pid %d" % pid)
        return pid

    def _bg_enqueue_seq(self):
        """映像タブの今の設定をジョブとして待ち行列へ追加し、ワーカーが居なければ起動する。"""
        import capture_bg
        seq = self._current_sequence()
        if seq is None:
            self.status_var.set("シーケンスレンダ: Sequencer でシーケンスを開いてください")
            return
        world = core._get_editor_world()
        if world is None:        # PIE 中（手元でレンダ中）はエディタのワールドが取れない
            self.status_var.set("バックグラウンド: 手元のレンダが終わってからキューに積んでください")
            return
        map_pkg = world.get_path_name().split(".")[0]
        seq_pkg = seq.get_path_name().split(".")[0]
        if map_pkg.startswith("/Temp/"):
            self.status_var.set("バックグラウンド: 保存されていないレベルは撮れません（保存してから）")
            return
        dirty = []
        for fn in ("get_dirty_map_packages", "get_dirty_content_packages"):
            try:
                dirty += [p.get_name() for p in getattr(unreal.EditorLoadingAndSavingUtils, fn)()]
            except Exception:
                pass
        if dirty and not messagebox.askokcancel(
                "バックグラウンドでキュー",
                "未保存の変更があります。バックグラウンドは保存済みの状態を撮るため、"
                "これらの変更は写りません。\n\n%s\n\nこのままキューに追加しますか？"
                % "\n".join(dirty[:12] + (["…"] if len(dirty) > 12 else [])),
                parent=self.root):
            self.status_var.set("バックグラウンド: 取り消しました（未保存の変更あり）")
            return
        self._save_ui_state()
        q = self._bg_qdir()
        capture_bg.recover(q)
        capture_bg.prune(q)
        passes = [_SEQ_SHORT.get(k, lab) for k, lab, _p in _SEQ_OUTPUTS
                  if any(v.get() for v in self.seq_out_vars[k])]
        snap = self._ui_state_dict()
        # 出力フォルダのトークンは今のレベル/カットで確定させる（後でエディタが別レベルを開いても同じ）
        snap["seq_out"] = self._expand_out_dir(snap.get("seq_out") or "",
                                               self._seq_first_camera(seq), seq)
        job = capture_bg.enqueue(q, {
            "kind": "sequence", "name": "%s  %s" % (seq.get_name(), "+".join(passes)),
            "map_name": "/".join(map_pkg.split("/")[-2:]),   # 同名レベルを見分けるためフォルダも
            "map": map_pkg, "sequence": seq_pkg, "settings": snap})
        self._bg_open_window()
        note = ""
        if self.seq_viewport_look_var.get():
            note = "（ビューポートのルックは MRQ 近似・パイロット状態は引き継がない）"
        if capture_bg.live_worker(q) is None:
            self._bg_start_worker()
            self.status_var.set("バックグラウンド: 追加して裏のエディタを起動しました: %s%s"
                                % (job["name"], note))
        else:
            self.status_var.set("バックグラウンド: キューに追加しました: %s%s" % (job["name"], note))
        self._bg_last_poll = 0.0

    def _bg_cancel_running(self):
        import capture_bg
        jid = capture_bg.request_cancel_running(self._bg_qdir())
        self.status_var.set("バックグラウンド: 実行中のジョブを中断しています…" if jid
                            else "バックグラウンド: 実行中のジョブはありません")
        self._bg_last_poll = 0.0

    def _bg_cancel_pending(self):
        import capture_bg
        q = self._bg_qdir()
        n = capture_bg.cancel_pending(q, capture_bg.live_worker(q) is not None)
        self.status_var.set("バックグラウンド: 待機中のジョブ %d 件を取り消しました" % n)
        self._bg_last_poll = 0.0

    def _bg_selected_job(self):
        w = getattr(self, "_bg_win", None)
        try:
            sel = w["tree"].selection()
        except Exception:
            return None
        if not sel:
            return None
        import capture_bg
        return next((j for j in capture_bg.list_jobs(self._bg_qdir()) if j["id"] == sel[0]), None)

    def _bg_cancel_selected(self):
        import capture_bg
        j = self._bg_selected_job()
        q = self._bg_qdir()
        if j is None or j.get("state") != "queued":
            self.status_var.set("バックグラウンド: 待機中のジョブを選んでください")
            return
        capture_bg.cancel_job(q, j["id"], capture_bg.live_worker(q) is not None)
        self.status_var.set("バックグラウンド: 取り消しました: %s" % j.get("name"))
        self._bg_last_poll = 0.0

    def _bg_open_output(self):
        j = self._bg_selected_job()
        out = ((j or {}).get("result") or {}).get("output_dir")
        if not out or not os.path.isdir(out):
            self.status_var.set("バックグラウンド: 出力のある完了済みジョブを選んでください")
            return
        os.startfile(out)

    def _bg_open_window(self):
        """待ち行列ウィンドウ（パネルと同じ配色の Toplevel）。開いていれば前に出すだけ。
        中身は _bg_poll がキューのフォルダから更新する（他のエディタが積んだジョブも出る）。"""
        if getattr(self, "_headless", False):
            return
        w = getattr(self, "_bg_win", None)
        if w is not None:
            try:
                if w["top"].winfo_exists():
                    w["top"].deiconify()
                    return
            except Exception:
                pass
        c = _THEME
        top = tk.Toplevel(self.root)
        top.title("キューリスト")
        top.configure(bg=c["bg"])
        top.geometry("620x300")
        st = ttk.Style(self.root)
        st.configure("BG.Treeview", background=c["field"], fieldbackground=c["field"],
                     foreground=c["fg"], bordercolor=c["border"], rowheight=20)
        st.configure("BG.Treeview.Heading", background=c["card"], foreground=c["sub"])
        st.map("BG.Treeview", background=[("selected", c["accent"])],
               foreground=[("selected", "#ffffff")])
        tree = ttk.Treeview(top, style="BG.Treeview", selectmode="browse",
                            columns=("state", "name", "map", "time"), show="headings")
        for col, txt, wd in (("state", "状態", 70), ("name", "ジョブ", 280),
                             ("map", "レベル", 120), ("time", "時間", 70)):
            tree.heading(col, text=txt)
            tree.column(col, width=wd, anchor="w", stretch=(col == "name"))
        tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        bar = ttk.Frame(top, style="Body.TFrame")
        bar.pack(fill="x", padx=8, pady=(0, 8))
        for txt, fn, tip in (
                ("実行中を中断", self._bg_cancel_running,
                 "裏で実行中のジョブを中断する（応答が無ければ裏のエディタを停止）"),
                ("選択を取り消し", self._bg_cancel_selected, "選んだ待機中のジョブを取り消す"),
                ("待機中を全て取り消し", self._bg_cancel_pending, "まだ始まっていないジョブを全て取り消す"),
                ("出力フォルダ", self._bg_open_output, "選んだ完了済みジョブの出力フォルダを開く")):
            self._tip(ttk.Button(bar, text=txt, command=fn), tip).pack(side="left", padx=(0, 6))
        top.protocol("WM_DELETE_WINDOW", top.withdraw)
        self._bg_win = {"top": top, "tree": tree}
        apply_top = getattr(self, "_apply_topmost", None)
        if apply_top:
            apply_top(top)
        self._bg_last_poll = 0.0

    def _bg_refresh_window(self, jobs, now):
        w = getattr(self, "_bg_win", None)
        if w is None:
            return
        try:
            if not w["top"].winfo_exists() or w["top"].state() == "withdrawn":
                return
        except Exception:
            return
        names = {"running": "実行中", "queued": "待機中", "done": "完了", "failed": "失敗",
                 "cancelled": "取り消し"}
        tree = w["tree"]
        import capture_bg
        q = self._bg_qdir()
        keep = set()
        for j in jobs:
            state = names.get(j.get("state"), j.get("state"))
            if j.get("state") == "queued" and capture_bg.cancel_requested(q, j["id"]):
                state = "取り消し"
            if j.get("state") == "running":
                t = "%d 秒" % (now - float(j.get("started") or now))
            elif j.get("finished") and j.get("started"):
                t = "%d 秒" % (float(j["finished"]) - float(j["started"]))
            else:
                t = ""
            vals = (state, j.get("name") or "", j.get("map_name") or
                    (j.get("map") or "").rsplit("/", 1)[-1], t)
            if tree.exists(j["id"]):
                tree.item(j["id"], values=vals)
            else:
                tree.insert("", "end", iid=j["id"], values=vals)
            keep.add(j["id"])
        for iid in tree.get_children():
            if iid not in keep:
                tree.delete(iid)

    def _bg_poll(self):
        """tick から 1 秒毎: 待ち行列の表示・完了の通知・死んだワーカーの後始末・中断の強制停止。"""
        import time
        now = time.time()
        gap = _BG_IDLE_POLL_SEC if getattr(self, "_bg_idle", False) else _BG_POLL_SEC
        if now - getattr(self, "_bg_last_poll", 0.0) < gap:
            return
        self._bg_last_poll = now
        import capture_bg
        q = self._bg_qdir()
        if not os.path.isdir(q):
            return
        s = capture_bg.poll(q, now=now)
        if s["needs_start"]:
            self._bg_start_worker()
            s["worker"] = "starting"
        self._bg_idle = not (s["worker"] or s["running"] or s["waiting"])
        parts = []
        r = s["running"]
        if r is not None:
            parts.append("実行中: %s（%d 秒）" % (r.get("name"), now - float(r.get("started") or now)))
        elif s["worker"] == "starting":
            parts.append("裏のエディタを起動中…")
        if s["waiting"]:
            parts.append("待機 %d 件" % s["waiting"])
        if s["start_blocked"]:
            parts.append("裏のエディタが起動に失敗しました（Saved/UE5Capture_bg/logs）")
        fin = sorted(s["finished"], key=lambda j: float(j.get("finished") or 0))
        seen = getattr(self, "_bg_seen", None)
        if seen is None:
            seen = self._bg_seen = set(j["id"] for j in fin)   # 開いた時点の完了済みは通知しない
        for j in fin:
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            res = j.get("result") or {}
            label = {"done": "完了", "failed": "失敗", "cancelled": "中断"}.get(j.get("state"), "")
            self.status_var.set("バックグラウンド%s: %s %s" % (
                label, j.get("name"), res.get("output_dir") if j.get("state") == "done"
                else (j.get("message") or "")))
        if fin:
            last = fin[-1]
            parts.append("直近: %s %s" % ({"done": "完了", "failed": "失敗", "cancelled": "中断"}.get(
                last.get("state"), last.get("state")), last.get("name")))
        self.bg_status_var.set(" / ".join(parts))
        self._bg_refresh_window(capture_bg.list_jobs(q), now)

    def _on_seq_render_impl(self):
        """戻り値: 非同期チェーンを開始したら True（完了側の _final が後始末）。"""
        import capture_mrq
        _apply_exposure_lock(self.seq_exposure_lock_var.get())
        _apply_no_motion_blur(self.seq_no_mblur_var.get())
        seq = self._current_sequence()
        if seq is None:
            self.status_var.set("シーケンスレンダ: Sequencer でシーケンスを開いてください")
            return False
        self._refresh_sequence()
        base_out = self._expand_out_dir(self.seq_out_var.get().strip(),
                                        self._seq_first_camera(seq), seq)
        if not base_out:
            self.status_var.set("シーケンスレンダ: 出力先を指定してください")
            return False
        if not os.path.isdir(base_out):
            try:
                os.makedirs(base_out)
            except Exception as e:
                self.status_var.set("シーケンスレンダ: 出力先を作成できません: %s" % e)
                return False
        # クリック時点の値を確定（レンダ中にウィジェットを触っても後続ジョブに混ざらない）
        seq_fog = self.seq_fog_var.get()
        seq_vp = self.seq_viewport_look_var.get()
        seq_fmt = {"PNG 8bit": "png", "JPG 8bit": "jpg",
                   "EXR 16bit (float)": "exr"}.get(self.seq_beauty_fmt_var.get(), "png")
        # Depth のビット深度: 8bit は Beauty と同一ジョブの PP パス（従来）。
        # 16bit PNG / EXR float はフレーム毎に SceneCapture で実測する（厳密リニア）
        seq_depth_mode = {"8bit PNG": "8bit", "16bit PNG": "16bit",
                          "EXR float": "exr"}.get(self.seq_depth_bit_var.get(), "8bit")
        W = self._int_var(self.seq_w_var, 1920)
        if self.seq_camasp_var.get():
            # カメラカットのカメラのアスペクトに従う（素の CameraActor も可）
            asp = self._seq_camera_aspect()
            H = int(round(W / asp)) if asp > 0.1 else self._int_var(self.seq_h_var, 1080)
        else:
            H = self._int_var(self.seq_h_var, 1080)
        if W < 16 or H < 16:
            self.status_var.set("シーケンスレンダ: 解像度が不正です (%d×%d)" % (W, H))
            return False
        warm = max(1, self._int_var(self.seq_warm_var, 32))
        ts = max(1, self._int_var(self.seq_ts_var, 8))
        ext = unreal.MovieSceneSequenceExtensions
        cs = ce = None
        if self.seq_range_mode.get() == "custom":
            cs = self._int_var(self.seq_start_var, 0)
            ce = self._int_var(self.seq_end_var, cs) + 1   # UI は End含む → 排他へ
            if ce <= cs:
                self.status_var.set("シーケンスレンダ: フレーム範囲が不正です (End は Start 以上)")
                return False
            cs_eff, ce_eff = cs, ce
        else:
            cs_eff = ext.get_playback_start(seq)
            ce_eff = ext.get_playback_end(seq)
        fr = ext.get_display_rate(seq)
        fps_num, fps_den = fr.numerator, max(fr.denominator, 1)

        wants = {k: (pv.get(), mv.get()) for k, (pv, mv) in self.seq_out_vars.items()}
        if not any(p or m for p, m in wants.values()):
            self.status_var.set("シーケンスレンダ: 出力素材が1つも選ばれていません")
            return False

        def _need(key):
            return wants[key][0] or wants[key][1]

        seq_notes = []
        mp4_any = any(m for _, m in wants.values())
        ffmpeg = None
        if mp4_any:
            ffmpeg = core.find_ffmpeg(getattr(self, "_ffmpeg_hint", None))
            if not ffmpeg:
                self.status_var.set("MP4 出力には ffmpeg が必要です（見つかりません。"
                                    "設定 JSON の ffmpeg_path か PATH を確認）")
                return False
            self._ffmpeg_hint = ffmpeg
            # libx264 + yuv420p は奇数の幅/高さを拒否する（spec E12）
            if (W & 1) or (H & 1):
                W, H = W & ~1, H & ~1
                seq_notes.append("MP4 用に解像度を %d×%d（偶数）に調整" % (W, H))
        crf = self._resolve_crf()

        matte_needed = _need("mfront") or _need("behind")
        depth_needed = _need("depth")
        depth_hi = depth_needed and seq_depth_mode != "8bit"
        if depth_hi and seq_depth_mode == "exr" and wants["depth"][1]:
            # 生 cm の float は H.264 にできない（値域が 0..1 でない）
            wants["depth"] = (True, False)
            seq_notes.append("Depth(EXR float): MP4 は出しません（連番のみ）")
        normal_needed = _need("normal")
        objid_needed = _need("objid")
        rlfull_needed = _need("rlfull")
        rldir_needed = _need("rldir")
        cloudmatte_needed = _need("cloudmatte")
        skymatte_needed = _need("skymatte")
        # メインジョブ（全編 Beauty）が要るか: 16bit/EXR の Depth は PP パスではなく
        # 別経路（フレーム毎 SceneCapture）なので、それ単独ならメインジョブは省く
        main_needed = (_need("beauty") or (depth_needed and not depth_hi) or normal_needed
                       or matte_needed or objid_needed or rlfull_needed)
        # Direct だけならメインジョブ自体を直射レンダにする（全編2回レンダの回避）
        only_direct = rldir_needed and not main_needed

        vdb = self._vdb_enabled()
        matte_actors = None
        matte_prims, matte_vols = [], []
        if matte_needed or self.seq_matte_hide_var.get():
            matte_actors = core._resolve_target_actors(
                None, self._pick_targets_resolved(self.matte_pick) or None)
            if not matte_actors:
                if matte_needed:
                    self.status_var.set("シーケンスレンダ: Matte 対象が見つかりません"
                                        "（対象欄の Matte targets か選択を確認）")
                    return False
                # 「Matte 対象を隠す」だけが ON で対象が無い: 隠さずに続行する
                seq_notes.append("Matte 対象を隠す: 対象が無いため無視")
                matte_actors = None
            # 板(プリミティブ)/雲(HVボリューム) 分割。雲は専用 CloudMatte ジョブで扱う。
            if matte_actors:
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
                                    "（対象欄の Object ID targets か選択を確認）")
                return False
            _op, _ov = core.split_volumetric_targets(objid_actors)
            if _ov:
                seq_notes.append("ObjectID: 雲(ボリューム)対象は映像では未対応（静止画のみ）")
                objid_actors = _op
                if not objid_actors:
                    wants["objid"] = (False, False)
                    objid_needed = False

        try:
            t_user = int(unreal.LevelSequenceEditorBlueprintLibrary.get_current_time())
        except Exception:
            t_user = None
        # 名前本体: テンプレートを レベル/カット/カメラカット先頭のカメラ/シーケンス で解決
        seq_cam = None
        try:
            seq_cam = self._sequence_camera_at(seq, cs_eff)
        except Exception as e:
            unreal.log_warning("[SceneCapture] 先頭カメラの解決に失敗（名前はカメラ無しで組む）: %s" % e)
        name_body = self._resolve_name(self.seq_name_tpl_var.get(), seq_cam, seq)
        if self.seq_overwrite_var.get():
            take_str = "%03d" % (core.latest_take_number(base_out, name_body) or 1)
            removed = core.delete_take_outputs(base_out, name_body, take_str)
            if removed:
                seq_notes.append("上書き: テイク %s の既存出力 %d 件を削除" % (take_str, removed))
        else:
            take_str = "%03d" % core.next_take_number(base_out)
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

        if depth_needed and self.seq_depth_auto_var.get():
            # Depth 自動レンジ: 先頭/中間/終端フレームをスクラブして実測し、
            # 全編を包む Near/Far を決定（欄へ書き戻し・プレイヘッドは復元）
            try:
                t0 = int(unreal.LevelSequenceEditorBlueprintLibrary
                         .get_current_time())
            except Exception:
                t0 = None
            lo = hi = None
            for f in sorted({cs_eff, (cs_eff + ce_eff - 1) // 2, ce_eff - 1}):
                c = self._sequence_camera_at(seq, f)
                if c is None:
                    continue
                rng = None
                try:
                    rng = core.measure_depth_range(c)
                except Exception as e:
                    unreal.log_warning(
                        "[SceneCapture] Depth自動レンジ失敗 (F%d): %s" % (f, e))
                if rng:
                    lo = rng[0] if lo is None else min(lo, rng[0])
                    hi = rng[1] if hi is None else max(hi, rng[1])
            if t0 is not None:
                try:
                    unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(t0)
                except Exception:
                    pass
            if lo is not None and hi is not None and hi > lo:
                self.seq_near_var.set("%g" % lo)
                self.seq_far_var.set("%g" % hi)
            else:
                seq_notes.append("Depth自動レンジ: 測定失敗（手動値を使用）")
        depth_near = self._float_var(self.seq_near_var, 0.0)
        depth_far = self._float_var(self.seq_far_var, 10000.0)
        if depth_needed and depth_far <= depth_near:
            seq_notes.append("Z-Depth: Far が Near 以下のため既定値 (0〜10000cm) を使用")
            depth_near, depth_far = 0.0, 10000.0
        depth_mat = matte_mat = matte_sil_mat = objid_mat = normal_mat = None
        hide_actors = None

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

        try:
            if depth_needed and not depth_hi:
                depth_mat = core.create_temp_depth_material(
                    depth_near, depth_far,
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
            # 途中まで作った一時マテリアルを残さない
            _cleanup_materials()
            self.status_var.set("一時マテリアル生成失敗: %s" % e)
            return False

        def _final(ok, od):
            try:
                _cleanup_materials()
            except Exception as e:
                unreal.log_warning("[SceneCapture] 一時マテリアル削除失敗: %s" % e)
            # 雲ジョブが残す r.PostProcessing.PropagateAlpha=1 を元値へ（実測リーク）
            self._run_restore_hooks()
            _apply_exposure_lock(False)
            self._set_busy(False)
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
        # Behind プレートの near clip と隠した手前アクター（MatteSil の遮蔽判定に流用）
        behind_seq = {"nc": None, "front": []}
        mfront_cloud = (vdb and bool(matte_vols) and not matte_prims
                        and _need("mfront"))
        # 雲マット独立出力: 対象はレベル内の全VDB雲。Matteの前の雲ジョブと重なる場合は
        # 1本で共用（対象は Matte 対象の雲のまま＝Matteの前の合成の意味を変えない）。
        # Normal / Depth 選択時も雲αを自動レンダして連番の雲領域を黒に落とす。
        cm_vols = []
        cloud_for_black = False
        # Depth の雲抜きは 8bit / 16bit PNG のみ（EXR float は生 cm なので対象外）
        depth_cloud_ok = depth_needed and seq_depth_mode != "exr"
        if cloudmatte_needed or normal_needed or depth_cloud_ok:
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
                    cloud_for_black = normal_needed or depth_cloud_ok
                    if depth_needed and seq_depth_mode == "exr":
                        seq_notes.append("Depth(EXR float): 生cmのため雲抜き対象外")
        cloud_vols_job = matte_vols if mfront_cloud else cm_vols
        if ((cloudmatte_needed or cloud_for_black) and mfront_cloud
                and {a.get_path_name() for a in matte_vols}
                != {a.get_path_name() for a in cm_vols}):
            seq_notes.append("雲マット: Matte対象の雲のみ（Matteの前と共用）")
        cloud_seq = {"run": mfront_cloud or cloudmatte_needed or cloud_for_black,
                     "mfront": mfront_cloud}
        if cloud_seq["run"]:
            pa0 = unreal.SystemLibrary.get_console_variable_int_value(
                "r.PostProcessing.PropagateAlpha")

            def _restore_pa():
                unreal.SystemLibrary.execute_console_command(
                    None, "r.PostProcessing.PropagateAlpha %d" % int(pa0))
            self._restore_hooks.append(_restore_pa)
        if backing_seq["run"]:
            seq_notes.append("雲マット: バッキング差分（板の手前の雲を遮蔽どおり反映）")
        pass_files_main = [] if only_direct else ["Beauty"]
        if depth_needed and not depth_hi:
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
                if depth_hi:
                    # 16bit PNG / EXR float の Depth: フレーム毎にスクラブして
                    # SceneCapture（RGBA32F）で実測（Behind の MatteSil と同じ経路）
                    self.status_var.set("Z-Depth(%s) をフレーム毎に実測中… (%d〜%dF)"
                                        % (seq_depth_mode, cs_eff, ce_eff - 1))
                    self.root.update()
                    nd, dext = core.render_depth_sequence(
                        out, name_body, take_str, cs_eff, ce_eff, W, H,
                        camera_for_frame=lambda f: self._sequence_camera_at(seq, f),
                        near_cm=depth_near, far_cm=depth_far,
                        mode=seq_depth_mode, aa=1)
                    if nd <= 0:
                        seq_notes.append("Z-Depth: フレーム毎の深度取得に失敗")
                        wants["depth"] = (False, False)
                    elif seq_depth_mode == "exr" and dext != "exr":
                        seq_notes.append("Z-Depth: EXR を書けず 16bit PNG にフォールバック")
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
                        camera_for_frame=lambda f: self._sequence_camera_at(seq, f),
                        near_clip_cm=behind_seq["nc"],
                        hidden_front=behind_seq["front"])
                    if ns > 0:
                        core.composite_behind_sequence(out, name_body, take_str)
                    else:
                        seq_notes.append("Matteの奥: シルエット生成に失敗")
                        wants["behind"] = (False, False)
                if cloud_for_black and cloud_seq["run"]:
                    # Normal / Depth 連番の雲領域を黒に（CloudMatte が α のままのうちに乗算）
                    if normal_needed:
                        core.apply_cloud_black_to_pass_frames(
                            out, name_body, take_str, "Normal")
                    if depth_cloud_ok and _need("depth"):
                        core.apply_cloud_black_to_pass_frames(
                            out, name_body, take_str, "Depth")
                if normal_needed and matte_prims:
                    # Normal のマット対象を黒に。MatteSil(全投影シルエット)ではなく
                    # 遮蔽考慮の Matte を使うので、マットより手前のキャラは残る。
                    if core.apply_matte_black_to_pass_frames(
                            out, name_body, take_str, "Normal") <= 0:
                        seq_notes.append("Normal のマット黒塗り: Matte 連番が無く未適用")
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

            def _delete_frames_ext(pass_names, exts):
                """指定パスの連番のうち拡張子 exts のものだけ削除（形式切替の後始末）。"""
                import re
                for pn in pass_names:
                    pat = re.compile(re.escape("%s_%s_%s." % (name_body, pn, take_str))
                                     + r"\d+\.(%s)$" % "|".join(exts))
                    for f in os.listdir(out):
                        if pat.match(f):
                            try:
                                os.remove(os.path.join(out, f))
                            except Exception:
                                pass

            cmds = []
            for key, _label, pass_name in _SEQ_OUTPUTS:
                if wants[key][1]:
                    cmd, _dst = core.encode_mp4_cmd(ffmpeg, out, name_body, pass_name,
                                                    take_str, fps_num, fps_den, crf, cs_eff)
                    cmds.append(cmd)

            def _after_encode(enc_ok):
                if not enc_ok:
                    # ffmpeg 失敗時は何も消さない（MP4 だけの素材の連番が消えて
                    # データ消失になる。spec E15）。作業フォルダごと診断用に残す
                    _final(False, od)
                    return
                drop = ["BehindPlate", "BackingT"]   # 中間素材は削除
                # CloudMatte は _SEQ_OUTPUTS 行になったので下の汎用ループが
                # 「連番のチェックが無いときだけ」削除する
                if matte_mat is not None:
                    drop.append("Matte")
                if _need("behind"):
                    drop.append("MatteSil")
                    drop.append("BehindOcc")
                if rldir_needed:
                    drop.append("DirectPlate")       # 直射ジョブの Beauty（内部素材）
                for key, _label, pass_name in _SEQ_OUTPUTS:
                    if not wants[key][0]:
                        drop.append(pass_name)
                core.delete_pass_frames(out, name_body, take_str, drop)
                if seq_fmt != "png":
                    # 形式が EXR/JPG のとき MRQ は全パスをその形式でも書く。
                    # 形式の対象は Beauty / Raw Lighting だけなので、他パスの EXR/JPG は
                    # 捨て、対象パスは逆に内部 PNG（合成・MP4 用）を捨てる
                    fmt_passes = {"Beauty", "RawLightingFull", "RawLightingDirect"}
                    others = [pn for _k, _l, pn in _SEQ_OUTPUTS if pn not in fmt_passes]
                    others += ["Matte", "MatteSil", "BehindOcc", "BehindPlate",
                               "DirectPlate", "SkyMatte"]
                    _delete_frames_ext(others, (seq_fmt,))
                    _delete_frames_ext([pn for pn in fmt_passes
                                        if any(wants[k][0] for k, _l, p in _SEQ_OUTPUTS
                                               if p == pn)], ("png",))
                # 連番を選んだ素材は素材毎のサブフォルダへ自動移動（MP4は直下のまま）
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
                        if depth_cloud_ok and _need("depth"):
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
                if cloudmatte_needed:
                    # 連番が無いまま MP4 を組むと ffmpeg が失敗して全体が失敗扱いになる
                    wants["cloudmatte"] = (False, False)
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
                        fog_off=seq_fog, on_done=cb, **kw)
                except Exception as e:
                    self.status_var.set("雲マット起動失敗: %s" % e)
                    _abort("雲マット: 起動失敗 (%s)" % label, od)

            if not vis_mode:
                # 従来の分離モードα（Matteの前の合成と共用・意味を変えない）
                def _cloud_done(cok, cod):
                    if not cok:
                        cloud_seq["run"] = False
                        if cloudmatte_needed:
                            wants["cloudmatte"] = (False, False)
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
                    fog_off=seq_fog, on_done=_bw_done)
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
                    image_format=seq_fmt, also_png=(seq_fmt != "png"),
                    fog_off=seq_fog, on_done=_direct_done)
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
                cams = []
                cs_cam = None
                for f in sorted({cs_eff, (cs_eff + ce_eff) // 2,
                                 max(cs_eff, ce_eff - 1)}):
                    try:
                        ca = self._sequence_camera_at(seq, f)
                    except Exception:
                        ca = None
                    if ca is None:
                        continue
                    c = core.get_camera_settings(ca)
                    if cs_cam is None:
                        cs_cam = c
                    cams.append(c)
                    ncs.append(core.matte_near_clip_cm(matte_prims, c))
                if not ncs:
                    raise RuntimeError("シーケンスカメラを特定できません")
                # 各フレームの実測最近距離のうち最小＝どのフレームでも背景を
                # 削らない値。⚠ サンプルは先頭/中間/末尾の3フレームのみ。
                nc = min(ncs)
                behind_seq["nc"] = nc
                # HV(雲)は near-clip を無視して写り込むため手前の雲はアクター単位で隠す
                front_vols = core.volumetrics_nearer_than(nc, cs_cam) if vdb else []
                # near clip は視線に垂直な1枚なので、傾いた板の手前に残る物は
                # アクター単位で隠す（MatteSil の遮蔽判定からも同じ物を外す）
                front = core.actors_in_front_of_matte(matte_prims, cams)
                behind_seq["front"] = front
            except Exception as e:
                # Behind だけ諦めて、レンダ済みのメイン素材の後処理は続行する
                seq_notes.append("Matteの奥: near-clip 計算失敗のためスキップ")
                self.status_var.set("Matteの奥: near-clip 計算失敗: %s" % e)
                wants["behind"] = (False, False)
                _run_direct(True, od)
                return
            # 「空マット」も選ばれていれば奥プレートも空黒で撮る（Matteの奥＝空黒版。
            # 空マットと同じく fog/PPマテリアル/ブルームは強制 OFF）。ユーザー指定 2026-10-07
            def _bp_done(bok, bod):
                # プレートが失敗しても、レンダ済みのメイン素材の後処理は完走する
                # （Direct / 空マット / 雲マットと同じ扱い）
                if not bok:
                    wants["behind"] = (False, False)
                    seq_notes.append("Matteの奥: プレートのレンダ失敗")
                _run_direct(True, od)

            self.status_var.set("Matteの奥プレートをレンダ中… (near-clip %.0fcm%s)"
                                % (nc, " / 空黒" if skymatte_needed else ""))
            self.root.update()
            try:
                capture_mrq.render_sequence(
                    seq, out, W, H, name_body, take_str,
                    do_png=True, do_mp4=False,
                    temporal_samples=ts, warmup=warm,
                    custom_start=cs, custom_end=ce,
                    hidden_actors=list(matte_prims) + front_vols + front,
                    near_clip_cm=nc,
                    beauty_label="BehindPlate",
                    sky_matte=skymatte_needed,
                    fog_off=True if skymatte_needed else seq_fog,
                    on_done=_bp_done)
            except Exception as e:
                seq_notes.append("Matteの奥: プレート起動失敗のためスキップ")
                self.status_var.set("Matteの奥プレート起動失敗: %s" % e)
                wants["behind"] = (False, False)
                _run_direct(True, od)

        if not main_needed and not only_direct:
            # 雲マット/空マット/16bit・EXR Depth のみ: 全編の Beauty メインジョブを
            # 省いて専用ジョブチェーン（→ 後処理の Depth 実測）から開始する
            self.status_var.set("マット素材のみをレンダ中…" if (cloudmatte_needed or skymatte_needed)
                                else "Z-Depth をフレーム毎に実測中…")
            self.root.update()
            _run_skymatte(True, out)
            return True

        if seq_vp and not only_direct:
            seq_notes.append("ビューポートのルック: MRQ 近似")
        # Matteの奥のみ（Matteの前なし）のとき: MatteSil はプレートジョブ側に
        # 移したため、メインの Beauty から板を消すには単純非表示にする
        main_hidden = (hide_actors if hide_actors
                       else (matte_prims if (_need("behind") and matte_mat is None
                                             and matte_prims)
                             else None))

        def _launch_main(exposure_ev=None):
            self.status_var.set("シーケンスレンダ中… (PIE / %d〜%dF @%gfps)"
                                % (cs_eff, ce_eff - 1, float(fps_num) / fps_den))
            self.root.update()
            capture_mrq.render_sequence(
                seq, out, W, H, name_body, take_str,
                do_png=True, do_mp4=False,
                temporal_samples=ts, warmup=warm,
                custom_start=cs, custom_end=ce,
                depth_material=depth_mat, normal_material=normal_mat,
                matte_material=matte_mat, matte_actors=matte_prims,
                objid_material=objid_mat, objid_actors=objid_actors,
                hidden_actors=main_hidden,
                fog_off=seq_fog,
                light_pass=(rlfull_needed or only_direct),
                light_direct=only_direct,
                light_label=("RawLightingDirect" if only_direct
                             else "RawLightingFull"),
                beauty_label=("DirectPlate" if only_direct else "Beauty"),
                # 連番の形式（Beauty / Raw Lighting）。PNG 以外のときは合成・MP4 用の
                # 内部 PNG も同時出力し、後処理の最後で片方を捨てる
                image_format=seq_fmt, also_png=(seq_fmt != "png"),
                # ビューポートのルックは Beauty を出すメインジョブだけ（MRQ 近似）
                viewport_look=(seq_vp and not only_direct),
                exposure_offset_ev=exposure_ev,
                on_done=_after_main)

        exposure_ev = None
        if seq_vp and not only_direct and (_need("beauty") or _need("mfront")):
            # ビューポートのルック: 露出の差はレベル/カメラの値から決める（試し撮りなし）
            try:
                exposure_ev, why = capture_mrq.viewport_look_exposure(seq, cs_eff)
                unreal.log("[SceneCapture] ビューポートのルック 露出: %s" % why)
                if exposure_ev:
                    seq_notes.append("ビューポートのルック: 露出 %+.2fEV 補正（カメラの露出補正の打ち消し）"
                                     % exposure_ev)
            except Exception as e:
                exposure_ev = None
                unreal.log_warning("[SceneCapture] ビューポートのルック: 露出の判定に失敗（補正なし）: %s" % e)
        try:
            _launch_main(exposure_ev or None)
        except Exception as e:
            _cleanup_materials()
            self.status_var.set("シーケンスレンダ起動失敗: %s" % e)
            return False
        return True

    def _viewport_grab(self, cam, W, H, path, hidden_actors, on_done):
        """ビューポートのルック（経路A）: レベルビューポートでカメラを通して W×H を撮る。
        Slate tick でファイル完成をポーリングし、完了/失敗/タイムアウトのいずれでも
        ビューポート状態を戻してから on_done(ok, err) を呼ぶ。hidden_actors はマット系
        出力時のクリーンプレート用（MRQ の Beauty と同じく撮影中だけエディタで隠す）。"""
        import time
        hid = []
        for a in hidden_actors or []:
            try:
                if not a.is_temporarily_hidden_in_editor():
                    a.set_is_temporarily_hidden_in_editor(True)
                    hid.append(a)
            except Exception:
                pass

        def _unhide():
            for a in hid:
                try:
                    a.set_is_temporarily_hidden_in_editor(False)
                except Exception:
                    pass
            hid[:] = []

        state = {"h": None, "g": None, "t0": time.time(), "ended": False}

        def _end(ok, err=None):
            if state["ended"]:
                return
            state["ended"] = True
            if state["h"] is not None:
                unreal.unregister_slate_post_tick_callback(state["h"])
                state["h"] = None
            if state["g"] is not None:
                state["g"]["restore"]()
            _unhide()
            if not ok:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass
            on_done(ok, err)

        try:
            state["g"] = core.begin_viewport_grab(cam, W, H, path)
        except Exception as e:
            _end(False, e)
            return
        # チェーン途中の例外でもビューポート/カメラ PP/非表示を残さない
        self._restore_hooks.append(state["g"]["restore"])
        self._restore_hooks.append(_unhide)

        def _tick(dt):
            try:
                if state["g"]["poll"]():
                    _end(True)
                elif time.time() - state["t0"] > _VIEWPORT_GRAB_TIMEOUT:
                    _end(False, "タイムアウト %.0f 秒" % _VIEWPORT_GRAB_TIMEOUT)
            except Exception as e:
                _end(False, e)

        state["h"] = unreal.register_slate_post_tick_callback(_tick)

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

    def _make_picker(self, parent, label, tip=None):
        """対象アクターのリストを作る。リストの中身＝対象。
        選択を追加: アウトライナ/ビューポートの選択を追加 / 削除: リストで選択した項目を削除。
        キーはフルパス名だが、追加時のラベルも保持し、パスが解決できなくなった場合
        （再インポート等でアクターが作り直された場合）はラベルで再解決する。"""
        p = {"all": [], "labels": {}}
        bar = ttk.Frame(parent)
        lab = ttk.Label(bar, text=label)
        lab.pack(side="left")
        if tip:
            self._tip(lab, tip)
        self._tip(ttk.Button(bar, text="＋ 追加",
                             command=lambda: self._pick_add_selection(p)),
                  "アウトライナ / ビューポートで選択中のアクターをリストへ追加").pack(
            side="right")
        self._tip(ttk.Button(bar, text="－ 削除",
                             command=lambda: self._pick_clear(p)),
                  "リストで選択した項目を外す").pack(side="right", padx=4)
        bar.pack(fill="x", pady=(0, 4))
        lbf = ttk.Frame(parent)
        p["list"] = tk.Listbox(lbf, selectmode="extended", height=3,
                               exportselection=False, activestyle="none",
                               bg=_THEME["field"], fg=_THEME["fg"],
                               selectbackground=_THEME["accent"],
                               selectforeground="#ffffff", borderwidth=0,
                               highlightthickness=1,
                               highlightbackground=_THEME["border"],
                               highlightcolor=_THEME["border"])
        sb = ttk.Scrollbar(lbf, orient="vertical", command=p["list"].yview)
        p["list"].configure(yscrollcommand=sb.set)
        p["list"].pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        lbf.pack(fill="x")
        return p

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
        self._update_name_preview()

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

    def _resolve_name(self, template, camera, seq):
        """ファイル名テンプレートを 現在のレベル名 / カット番号 / カメラ / シーケンス で解決する。
        camera はアクターでもラベル文字列でもよい。失敗したトークンは空になる（例外は出さない）。"""
        return core.resolve_name_template(template, **self._name_tokens(camera, seq))

    @staticmethod
    def _seq_first_camera(seq):
        """カメラカット先頭のカメラ（ログを出さない静かな解決・名前プレビューと同じ）。"""
        try:
            import capture_mrq
            cams = capture_mrq._camera_cut_camera_actors(seq, core._get_editor_world())
            return cams[0] if cams else None
        except Exception:
            return None

    def _expand_out_dir(self, path, camera, seq):
        """出力フォルダのトークン（ファイル名と同じ {level}/{cut}/{camera}/{seq}）を展開する。
        トークンが無ければそのまま（従来どおり）。設定には展開前の文字列を保存する。"""
        if "{" not in (path or ""):
            return path
        return core.resolve_path_template(path, **self._name_tokens(camera, seq))

    def _name_tokens(self, camera, seq):
        label = ""
        if camera is not None:
            try:
                label = camera if isinstance(camera, str) else camera.get_actor_label()
            except Exception:
                label = ""
        lvl = cut = ""
        try:
            world = core._get_editor_world()
            lvl = core.level_name(world)
            cut = core.detect_cut_number(world)
        except Exception as e:
            unreal.log_warning("[SceneCapture] レベル名/カット番号の取得に失敗: %s" % e)
        sname = ""
        if seq is not None:
            try:
                sname = seq.get_name()
            except Exception:
                sname = ""
        return {"level": lvl, "cut": cut, "camera": label, "seq": sname}

    def _update_name_preview(self):
        """ファイル名欄の下に解決後の名前を出す（画像=選択カメラ、映像=カメラカット先頭のカメラ）。
        映像側のカメラはログを出さない静かな解決（_camera_cut_camera_actors）で済ませる。"""
        seq = None
        try:
            seq = self._current_sequence()
        except Exception:
            seq = None
        if hasattr(self, "name_preview_var"):
            try:
                body = self._resolve_name(self.name_tpl_var.get(), self._current_camera(), seq)
                self.name_preview_var.set("→ %s_素材名_NNN" % body)
            except Exception as e:
                self.name_preview_var.set("(名前の解決に失敗: %s)" % e)
        if not hasattr(self, "seq_name_preview_var"):
            return
        cam = None
        if seq is not None:
            try:
                import capture_mrq
                cams = capture_mrq._camera_cut_camera_actors(seq, core._get_editor_world())
                cam = cams[0] if cams else None
            except Exception:
                cam = None
        try:
            body = self._resolve_name(self.seq_name_tpl_var.get(), cam, seq)
            self.seq_name_preview_var.set("→ %s_素材名_NNN.フレーム番号" % body)
        except Exception as e:
            self.seq_name_preview_var.set("(名前の解決に失敗: %s)" % e)

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
        self._update_name_preview()

    # ----------------------------------------------------------- 設定の保持
    def _settings_path(self):
        if getattr(self, "_settings_override", None):
            return self._settings_override
        return os.path.normpath(os.path.join(
            unreal.Paths.project_saved_dir(), "UE5Capture_ui_settings.json"))

    def _save_ui_state(self):
        try:
            state = self._ui_state_dict()
            # 途中で失敗しても空ファイルを残さない（tmp に書いて差し替え）
            path = self._settings_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            unreal.log_warning("[SceneCapture] 設定保存に失敗: %s" % e)

    def _ui_state_dict(self):
        """パネルの全設定（設定ファイルとバックグラウンドのジョブで同じ形式）。"""
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
                "name_tpl": self.name_tpl_var.get(),
                "overwrite": self.overwrite_var.get(),
                "depth": self.depth_var.get(),
                "depth_auto": self.depth_auto_var.get(),
                "seq_depth_auto": self.seq_depth_auto_var.get(),
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
                "exposure_lock": self.exposure_lock_var.get(),
                "viewport_look": self.viewport_look_var.get(),
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
                "seq_exposure_lock": self.seq_exposure_lock_var.get(),
                "seq_no_mblur": self.seq_no_mblur_var.get(),
                "seq_viewport_look": self.seq_viewport_look_var.get(),
                "seq_out": self.seq_out_var.get(),
                "seq_name_tpl": self.seq_name_tpl_var.get(),
                "seq_overwrite": self.seq_overwrite_var.get(),
                "seq_near": self.seq_near_var.get(),
                "seq_far": self.seq_far_var.get(),
                "seq_depth_bit": self.seq_depth_bit_var.get(),
                "seq_beauty_fmt": self.seq_beauty_fmt_var.get(),
                "seq_bg": self.seq_bg_var.get(),
                "topmost": self.topmost_var.get(),
        }
        return state

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
        if not isinstance(st, dict):
            unreal.log_warning("[SceneCapture] 設定ファイルの形式が不正（無視）: %s" % p)
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
        if "name_tpl" in st:
            _setvar(self.name_tpl_var, "name_tpl")
        elif st.get("name_usecustom") and str(st.get("name_custom", "")).strip():
            # 旧設定（任意名 + カメラ名トグル）からの移行。任意名が無ければ新既定のまま
            self.name_tpl_var.set(str(st["name_custom"]).strip()
                                  + ("_{camera}" if st.get("name_usecam", True) else ""))
        _setvar(self.overwrite_var, "overwrite")
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
        _setvar(self.depth_auto_var, "depth_auto")
        _setvar(self.seq_depth_auto_var, "seq_depth_auto")
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
        _setvar(self.exposure_lock_var, "exposure_lock")
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
        _setvar(self.seq_exposure_lock_var, "seq_exposure_lock")
        _setvar(self.seq_no_mblur_var, "seq_no_mblur")
        _setvar(self.seq_out_var, "seq_out")
        if "seq_name_tpl" in st:
            _setvar(self.seq_name_tpl_var, "seq_name_tpl")
        elif st.get("seq_usecustom") and str(st.get("seq_custom", "")).strip():
            self.seq_name_tpl_var.set(str(st["seq_custom"]).strip() + "_{seq}")
        _setvar(self.seq_overwrite_var, "seq_overwrite")
        _setvar(self.seq_near_var, "seq_near")
        _setvar(self.seq_far_var, "seq_far")
        if st.get("seq_depth_bit") in ("8bit PNG", "16bit PNG", "EXR float"):
            self.seq_depth_bit_var.set(st["seq_depth_bit"])
        if st.get("seq_beauty_fmt") in ("PNG 8bit", "JPG 8bit", "EXR 16bit (float)"):
            self.seq_beauty_fmt_var.set(st["seq_beauty_fmt"])
        _setvar(self.viewport_look_var, "viewport_look")
        _setvar(self.seq_viewport_look_var, "seq_viewport_look")
        _setvar(self.seq_bg_var, "seq_bg")
        _setvar(self.topmost_var, "topmost")
        self._apply_topmost()
        # 読込順に依らずビューポートのルックを優先（Fog/露出固定が同時 ON の設定でも外す）
        self._sync_viewport_look()

    def _collect_settings(self):
        s = core.CaptureSettings()
        s.camera_actor = self._current_camera()
        s.use_camera_resolution = (self.res_mode.get() == "camera")
        s.override_width = self._int_var(self.w_var, s.override_width)
        s.override_height = self._int_var(self.h_var, s.override_height)
        s.aa_factor = {"1x": 1, "2x": 2, "4x": 4}.get(self.aa_var.get(), 2)
        s.output_dir = self._expand_out_dir(self.out_var.get().strip(), s.camera_actor,
                                            self._current_sequence())
        s.name_body = self._resolve_name(self.name_tpl_var.get(), s.camera_actor,
                                         self._current_sequence())
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
                return
            try:
                self._bg_poll()
            except Exception as e:
                # 待ち行列の表示の失敗でパネルの tick を止めない（1 回だけ警告）
                core._warn_once("bg_poll", "バックグラウンドの状態取得に失敗: %s" % e)
        unreal._ue5capture_tick_handle = unreal.register_slate_post_tick_callback(_tick)

    def _on_close(self):
        """閉じる=withdraw。ルートは destroy しない（再生成時に Tcl panic で
        エディタごと落ちるため）。再表示は show() が deiconify する。"""
        self._save_ui_state()
        _unregister_global_tick()
        try:
            # tick が止まると更新も操作もできないので、キューリストも一緒に隠す
            bw = getattr(self, "_bg_win", None)
            if bw is not None:
                bw["top"].withdraw()
        except Exception:
            pass
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
