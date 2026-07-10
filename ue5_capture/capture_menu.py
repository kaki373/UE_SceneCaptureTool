# -*- coding: utf-8 -*-
"""
capture_menu.py  --  メニューバー「SceneCapture」メニュー + ツールバーボタンの登録

導入（ue2max と同じ方式・プロジェクト毎に1回）:
  Project Settings > Plugins > Python > Startup Scripts の「+Add」に
  このファイルのパスを追加してエディタを再起動する:

      D:/webui/ClaudeCode/UE_capture/ue5_capture/capture_menu.py

一度きりの手動実行（再起動なし）:
  Output Log の Python コンソールから  py "<このファイルのパス>"
  再実行してもメニューは重複しない（登録済みなら削除して作り直す）。
"""
import os
import sys

import unreal

try:
    _THIS_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    raise RuntimeError("capture_menu はファイルとして実行してください"
                       "（Startup Scripts / py コマンド）")

if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

_MENU_NAME = "SceneCapture"
_PARENT = "LevelEditor.MainMenu"
_SUBMENU_PATH = _PARENT + "." + _MENU_NAME


# ------------------------------------------------------------------- handlers
def on_open_panel():
    """ツールを開く（次の Slate tick に遅延して開く）。

    メニュー/ツールバークリックの Slate コールスタック内で tk.Tk() を生成すると、
    別ツール（max2ue 等）の Tk ルートが既に存在するセッションで Tcl panic →
    エディタごと abort する（2026-07-10 クラッシュダンプ2件で実測。
    MCP 経由など Slate スタック外からの同じ処理は落ちない）。
    コールスタックが巻き戻った後の tick で開けば安全。"""
    state = {}

    def _open_once(dt):
        h = state.pop("h", None)
        if h is not None:
            try:
                unreal.unregister_slate_post_tick_callback(h)
            except Exception:
                pass
        try:
            import importlib
            import capture_core
            import capture_ui
            importlib.reload(capture_core)
            importlib.reload(capture_ui)
            capture_ui.show()
        except Exception as ex:
            import traceback
            traceback.print_exc()
            unreal.log_error("[SceneCapture] パネルを開けませんでした: %s" % ex)

    state["h"] = unreal.register_slate_post_tick_callback(_open_once)


def _settings_json_path():
    return os.path.normpath(os.path.join(
        unreal.Paths.project_saved_dir(), "UE5Capture_ui_settings.json"))


def on_open_output():
    """前回使った出力フォルダ（無ければ Saved/Captures）を OS で開く。"""
    import json
    out = ""
    try:
        with open(_settings_json_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
        out = (st.get("seq_out") or st.get("out") or "").strip()
    except Exception:
        pass
    if not out:
        out = os.path.normpath(os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
    try:
        os.makedirs(out, exist_ok=True)
        os.startfile(out)
    except Exception as ex:
        unreal.log_warning("[SceneCapture] 出力フォルダを開けませんでした: %s" % ex)


def on_settings():
    """UI 設定 JSON をテキストエディタで開く。"""
    p = _settings_json_path()
    if not os.path.isfile(p):
        unreal.log_warning("[SceneCapture] 設定ファイルがまだありません"
                           "（一度ツールを開くと作られます）: %s" % p)
        return
    try:
        os.startfile(p)
    except Exception as ex:
        unreal.log_warning("[SceneCapture] 設定を開けませんでした: %s" % ex)


# ------------------------------------------------------------------ menu build
def _entry(name, label, tooltip, call):
    e = unreal.ToolMenuEntry(name=name, type=unreal.MultiBlockType.MENU_ENTRY)
    e.set_label(label)
    e.set_tool_tip(tooltip)
    # このファイルのフォルダは sys.path 済みなのでモジュール名 import で解決できる
    e.set_string_command(
        unreal.ToolMenuStringCommandType.PYTHON, unreal.Name(""),
        "import capture_menu; capture_menu.%s" % call)
    return e


def register():
    """メニューバー「SceneCapture」+ ツールバーボタンを(再)登録する。冪等。"""
    menus = unreal.ToolMenus.get()
    if menus.is_menu_registered(_SUBMENU_PATH):
        menus.remove_menu(_SUBMENU_PATH)
    main = menus.find_menu(_PARENT)
    if main is None:
        unreal.log_warning("[SceneCapture] メインメニューが見つからず登録をスキップしました。")
        return None
    sub = main.add_sub_menu(
        _MENU_NAME, "", _MENU_NAME, "SceneCapture",
        "Beauty / Z-Depth / Matte / Object ID / シーケンスレンダ (PNG連番・MP4)")

    sub.add_section("panel", "Panel")
    sub.add_menu_entry("panel", _entry(
        "OpenPanel", "Panel...",
        "Scene Capture Tool を開く（コード修正は reload されて反映）",
        "on_open_panel()"))

    sub.add_section("misc", "")
    sub.add_menu_entry("misc", _entry(
        "OpenOutput", "Open Output Folder",
        "前回使った出力フォルダを OS のファイルブラウザで開く",
        "on_open_output()"))
    sub.add_menu_entry("misc", _entry(
        "Settings", "Settings...",
        "UI 設定 JSON (Saved/UE5Capture_ui_settings.json) を開く",
        "on_settings()"))

    # ツールバーの SceneCapture ボタンも登録（セッション内での二重登録は防ぐ）
    try:
        import startup_menu
        if not getattr(unreal, "_ue5capture_toolbar_registered", False):
            if startup_menu.register():
                unreal._ue5capture_toolbar_registered = True
    except Exception as ex:
        unreal.log_warning("[SceneCapture] ツールバーボタン登録に失敗: %s" % ex)

    menus.refresh_all_widgets()
    unreal.log("[SceneCapture] メニュー '%s' を登録しました。" % _SUBMENU_PATH)
    return sub


if __name__ == "__main__":
    register()
