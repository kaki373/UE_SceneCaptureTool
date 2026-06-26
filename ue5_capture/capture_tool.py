# -*- coding: utf-8 -*-
"""
capture_tool.py  --  UE5.7 Scene Capture Tool エントリポイント

UE5 エディタの Output Log / Python コンソールから:

    py "C:/tools/ue5_capture/capture_tool.py"

動作:
  1) LAUNCH_GUI=True かつ tkinter が使える → GUI を表示
  2) tkinter が無い / LAUNCH_GUI=False → 下記 CONFIG 辞書の内容で即キャプチャ（CUI）

スクリプトはプロジェクト外の任意パスに置ける。実行時に自身のフォルダを
sys.path へ追加するので capture_core / capture_ui を解決できる。
"""

import os
import sys

import unreal

# このスクリプトのあるフォルダを import パスに追加（プロジェクト外配置に対応）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import capture_core as core


# ============================================================================
#  設定（GUI が使えない環境ではこの CONFIG を編集して実行 = CUI フォールバック）
# ============================================================================
LAUNCH_GUI = True   # False にすると GUI を出さず CONFIG で即実行

CONFIG = {
    # カメラはラベル名で指定（None なら最初に見つかったカメラ）
    "camera_label": None,

    # 解像度: use_camera_resolution=True ならカメラのアスペクト維持
    "use_camera_resolution": False,
    "override_width": 3840,
    "override_height": 2160,
    "base_height": 1080,           # use_camera_resolution 時の基準高さ

    "aa_factor": 2,                # 1 / 2 / 4

    # 出力先（空ならプロジェクトの Saved/Captures/）
    "output_dir": "",

    "do_color": True,
    "do_depth": False,
    "do_matte": False,
    "do_object_id": False,         # 色分け Object ID 1枚 + 色対応 JSON
    "matte_invert": False,         # Matte: 選択=黒/周囲=白
    "matte_fill_alpha": False,     # Matte を Fill(レンダ画像)のアルファに組み込んだ RGBA も出力
    "do_behind_matte": False,      # マット対象の向こう側だけ（窓抜き）のマスク
    "objid_fill_alpha": False,     # Fill + Object ID 範囲をα化した RGBA も出力
    "objid_hide_render": False,    # Object ID 対象を非表示にして Fill をレンダリング

    "depth_bit": "16bit",          # "8bit"(PNG) / "16bit"(PNG) / "exr"(float)
    "depth_near": 0.0,             # cm
    "depth_far": 10000.0,          # cm
    "depth_invert": True,          # True: 手前=白/奥=黒（PNG出力時）

    # 露出: "auto"(自動) / "manual"(exposure_bias の EV) / "scene"(ビューポート設定)
    "exposure_mode": "auto",
    "exposure_bias": -8.0,         # manual 時の EV（負で明るく）

    # Matte 対象: use_selection=True → 選択中のアクター / False → names のラベル指定
    "matte_use_selection": True,
    "matte_actor_names": [],       # 例: ["SM_Tree_01", "SM_Rock_02"]
    # Object ID 対象（Matte とは別）
    "objid_use_selection": True,
    "objid_actor_names": [],
}
# ============================================================================


def _find_camera(label):
    cams = core.list_cameras()
    if not cams:
        return None
    if not label:
        return cams[0]
    for c in cams:
        if c.get_actor_label() == label:
            return c
    unreal.log_warning("[SceneCapture] カメラ '%s' が見つかりません。先頭を使用します。" % label)
    return cams[0]


def _settings_from_config():
    s = core.CaptureSettings()
    s.camera_actor = _find_camera(CONFIG["camera_label"])
    s.use_camera_resolution = CONFIG["use_camera_resolution"]
    s.override_width = CONFIG["override_width"]
    s.override_height = CONFIG["override_height"]
    s.base_height = CONFIG["base_height"]
    s.aa_factor = CONFIG["aa_factor"]
    out = CONFIG["output_dir"].strip()
    if not out:
        out = os.path.normpath(os.path.join(unreal.Paths.project_saved_dir(), "Captures"))
    if out and not os.path.isdir(out):
        try:
            os.makedirs(out)
        except Exception:
            pass
    s.output_dir = out
    s.do_color = CONFIG["do_color"]
    s.do_depth = CONFIG["do_depth"]
    s.do_matte = CONFIG["do_matte"]
    s.do_object_id = CONFIG.get("do_object_id", False)
    s.matte_invert = CONFIG.get("matte_invert", False)
    s.matte_fill_alpha = CONFIG.get("matte_fill_alpha", False)
    s.do_behind_matte = CONFIG.get("do_behind_matte", False)
    s.objid_fill_alpha = CONFIG.get("objid_fill_alpha", False)
    s.objid_hide_render = CONFIG.get("objid_hide_render", False)
    s.depth_bit = CONFIG["depth_bit"]
    s.depth_near = CONFIG["depth_near"]
    s.depth_far = CONFIG["depth_far"]
    s.depth_invert = CONFIG.get("depth_invert", True)
    s.exposure_mode = CONFIG.get("exposure_mode", "auto")
    s.exposure_bias = CONFIG.get("exposure_bias", -8.0)
    s.matte_actors = None
    s.matte_actor_names = None if CONFIG["matte_use_selection"] else list(CONFIG.get("matte_actor_names", []))
    s.objid_actors = None
    s.objid_actor_names = None if CONFIG.get("objid_use_selection", True) else list(CONFIG.get("objid_actor_names", []))
    return s


def run_cui():
    unreal.log("[SceneCapture] CUI モードで実行します（CONFIG を使用）。")
    s = _settings_from_config()
    return core.run_capture(s)


def main():
    if LAUNCH_GUI:
        try:
            import capture_ui
            capture_ui.show()
            unreal.log("[SceneCapture] GUI を起動しました。")
            return
        except ImportError as e:
            unreal.log_warning(
                "[SceneCapture] GUI(tkinter) を使えません (%s)。CONFIG による CUI に切替えます。" % e)
        except Exception as e:
            unreal.log_warning("[SceneCapture] GUI 起動に失敗 (%s)。CUI に切替えます。" % e)
    run_cui()


if __name__ == "__main__":
    main()
