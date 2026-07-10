# -*- coding: utf-8 -*-
"""
startup_menu.py  --  UE エディタのツールバーに Scene Capture 起動ボタンを登録する

Documents/UnrealEngine/Python/init_unreal.py から呼ばれる想定
（エンジンがそのフォルダを sys.path に加え、init_unreal.py を自動実行する）。
ボタンはモジュールを reload してから GUI を開くので、コード修正後も
ボタンを押すだけで最新版が立ち上がる。
"""

import os

import unreal

_TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
_OWNER = "UE5CaptureLauncher"
_SECTION = "UE5Capture"
_ENTRY = "UE5CaptureLaunch"

_LAUNCH_CMD = (
    "import sys, importlib\n"
    "p = r'%s'\n"
    "if p not in sys.path:\n"
    "    sys.path.insert(0, p)\n"
    "import capture_core, capture_ui\n"
    "importlib.reload(capture_core)\n"
    "importlib.reload(capture_ui)\n"
    "capture_ui.show()\n"
) % _TOOL_DIR


def register():
    """レベルエディタのツールバー（右端の User 領域）に SceneCapture ボタンを追加する。"""
    menus = unreal.ToolMenus.get()
    toolbar = menus.find_menu("LevelEditor.LevelEditorToolBar.User")
    if toolbar is None:
        unreal.log_warning("[SceneCapture] ツールバーが見つからずボタン登録をスキップしました。")
        return False
    entry = unreal.ToolMenuEntry(
        name=_ENTRY, type=unreal.MultiBlockType.TOOL_BAR_BUTTON)
    entry.set_label("SceneCapture")
    entry.set_tool_tip("Scene Capture Tool を開く（コード修正は reload されて反映）")
    entry.set_string_command(
        unreal.ToolMenuStringCommandType.PYTHON, "", string=_LAUNCH_CMD)
    toolbar.add_section(_SECTION, "Scene Capture")
    toolbar.add_menu_entry(_SECTION, entry)
    menus.refresh_all_widgets()
    unreal.log("[SceneCapture] ツールバーボタンを登録しました。")
    return True
