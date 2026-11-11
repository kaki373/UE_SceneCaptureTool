# -*- coding: utf-8 -*-
"""
capture_core.py  --  UE5.7/5.8 Scene Capture core logic

SceneCaptureComponent2D + TextureRenderTarget2D をランタイムで Spawn / 生成し、
Color / Z-Depth / Matte Alpha の各パスをキャプチャしてディスクへ書き出す。
すべて Transient パッケージ上で生成し、キャプチャ後（例外時含む）に確実に破棄する。
.uasset として保存するのは /Game/_UE5Capture_Tmp 下の一時 PP マテリアル群と
マット板用の MatteBoard 材（M_UE5Cap_MatteBoardUnlit。レベルから参照される）だけ。

外部依存（仕様書 ue5_capture_tool_prompt.md の「外部依存」節を参照）:
  - numpy   : 必須（RenderTarget 画素の後処理。AA ダウンスケール / Depth 正規化 / Matte 閾値）
  - Pillow  : 必須（PNG / 16bit PNG 書き出し、PNG 読み込み）
  - OpenEXR + Imath か imageio : 任意（Depth 16bit を EXR で書き出す場合のみ。無ければ 16bit PNG にフォールバック）

UE の RenderTarget は全画素を Python から高速に読み取る API が無いため、
一旦 export_render_target() で一時ファイルへ書き出してから numpy/Pillow で後処理する。
"""

import os
import re
import math
import uuid
import datetime

import unreal

# ----------------------------------------------------------------------------
# 任意ライブラリの検出
# ----------------------------------------------------------------------------
try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:
    _np = None
    _HAS_NUMPY = False

try:
    from PIL import Image as _PILImage
    _HAS_PIL = True
except Exception:
    _PILImage = None
    _HAS_PIL = False


# ----------------------------------------------------------------------------
# ログ
# ----------------------------------------------------------------------------
_TAG = "[SceneCapture] "


def _log(msg):
    unreal.log(_TAG + str(msg))


def _warn(msg):
    unreal.log_warning(_TAG + str(msg))


def _err(msg):
    unreal.log_error(_TAG + str(msg))


_WARNED_ONCE = set()


def _warn_once(key, msg):
    """同じ key の警告はセッション中1回だけ出す（フレーム毎ループ等での連打防止）。"""
    if key in _WARNED_ONCE:
        return
    _WARNED_ONCE.add(key)
    _warn(msg)


# ----------------------------------------------------------------------------
# エンジンバージョン差の吸収（UE5.7 / 5.8）
#   ツールが使う API は 5.7 と 5.8 で同名・同シグネチャ（2026-10-29 リフレクション
#   全数比較）。差分は 5.8 で増えた API の有無だけなので、ここに集約して hasattr で分岐する。
# ----------------------------------------------------------------------------
def ensure_preset_config_mode(job):
    """MRQ ジョブを従来の Preset 構成（job.get_configuration() の設定群）で描かせる。
    5.8 でジョブに Preset/Graph/Basic の構成モードが入り、Preset 以外では
    get_configuration() に足した設定が使われない。allocate_new_job の既定は Preset だが、
    エディタ既定（Basic）に寄せた変更が入っても黙って設定が無視されないよう明示する。
    5.7 には構成モードが無いので何もしない。"""
    mode_enum = getattr(unreal, "MoviePipelineConfigMode", None)
    if mode_enum is None or not hasattr(job, "set_config_mode"):
        return
    if job.get_config_mode() != mode_enum.PRESET:
        job.set_config_mode(mode_enum.PRESET)


def default_near_clip_cm():
    """プロジェクト設定の near clip（GEngine の NearClipPlane, cm）。
    r.SetNearClipPlane はコマンドで自動復元されないため、撮影後はこの値へ戻す
    （10 固定で戻すと DefaultEngine.ini で変えているプロジェクトで値が変わったままになる）。
    読めなければエンジン既定（BaseEngine.ini）の 10.0。
    UEngine は Python に公開されていない（5.7 実測: unreal.Engine が無い）ので、
    プロジェクトの DefaultEngine.ini の [/Script/Engine.Engine] NearClipPlane を読む。
    ⚠ 簡易読み: プラットフォーム別 ini / Saved 側の上書きは見ない。厳密にするなら
    C++ 側で GNearClippingPlane を返す関数を公開する。結果はモジュール変数にキャッシュ。"""
    global _NEAR_CLIP_CACHE
    if _NEAR_CLIP_CACHE is None:
        _NEAR_CLIP_CACHE = _read_near_clip_ini()
    return _NEAR_CLIP_CACHE


_NEAR_CLIP_CACHE = None


def _read_near_clip_ini():
    try:
        ini = os.path.join(unreal.Paths.project_config_dir(), "DefaultEngine.ini")
        if os.path.isfile(ini):
            sec = None
            v = None
            with open(ini, "r", encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("["):
                        sec = s
                    elif sec == "[/Script/Engine.Engine]" and \
                            s.lower().startswith("nearclipplane="):
                        v = float(s.split("=", 1)[1])
            if v is not None and v > 0.0:
                return v
    except Exception as e:
        _warn_once("near_clip", "near clip 設定を読めないため 10cm で復元します: %s" % e)
    return 10.0


# ----------------------------------------------------------------------------
# 設定オブジェクト
# ----------------------------------------------------------------------------
class CaptureSettings(object):
    """キャプチャ1回分の全パラメータ。"""

    def __init__(self):
        self.camera_actor = None          # unreal.CameraActor / CineCameraActor
        self.use_camera_resolution = True  # True: カメラのアスペクト維持 / False: override
        self.override_width = 1920
        self.override_height = 1080
        self.base_height = 1080            # use_camera_resolution 時の基準高さ
        self.aa_factor = 2                 # 1 / 2 / 4 （Spatial Supersample 倍率）
        self.output_dir = ""               # 出力フォルダ
        self.take_suffix = None            # ファイル名の通し番号(例 "001")。None なら日時を使う
        self.name_prefix = ""              # 任意名（空なら付けない）
        self.name_include_camera = True    # カメラ名をファイル名に含めるか
        self.name_body = ""                # 解決済みの名前本体（テンプレート方式。空なら上 2 つで組む）

        self.do_color = True
        self.do_depth = False
        self.do_matte = False
        self.do_object_id = False          # 各アクターを色分けした Object ID 1枚（+色対応JSON）
        self.matte_invert = False          # True: 選択=黒/周囲=白（既定は 選択=白/周囲=黒）
        self.matte_fill_alpha = False      # Matte を Fill(レンダ画像)のアルファに組み込んだ RGBA も出力
        self.color_hide_matte = False      # Color(Beauty) から Matte 対象を非表示にして撮る（クリーンプレート）
        self.depth_hide_matte = False      # Z-Depth から Matte 対象を除外（マット位置は奥の深度になる）
        self.objid_fill_alpha = False      # Fill + Object ID カバレッジをアルファにした RGBA も出力
        self.objid_hide_render = False     # Object ID 対象を非表示にして Fill をレンダリング
        self.do_behind_matte = False       # マット対象の向こう側だけ（窓抜き）。Beauty合成は MRQ 側
        self.overscan = 0.0                # オーバースキャン率 f（FOVを(1+f)倍に広げる。解像度は呼び側で×(1+f)）
        self.fog_off = False               # True で Fog を OFF にして書き出す

        self.depth_bit = "16bit"           # "8bit"(PNG) / "16bit"(PNG) / "exr"(float)
        self.depth_near = 0.0              # cm
        self.depth_far = 10000.0           # cm
        self.depth_invert = True           # True: 手前=白/奥=黒（PNG出力時のみ。EXRは生値）

        # 露出（UE5.7: SceneCapture 単発は eye-adaptation が収束せず暗くなる）
        #   exposure_mode: "auto"  = 自動キャリブレーション（既定・暗潰れ回避）
        #                  "manual"= exposure_bias の EV を固定
        #                  "scene" = カメラ/シーン(ビューポート)の PostProcess をそのまま使う
        self.exposure_mode = "auto"
        self.exposure_bias = -8.0          # manual 時の EV（負で明るく）
        self.exposure_target = 45.0        # auto 時の目標 median 輝度 (0-255)。下げると暗く（ビューポート寄り）
        self.exposure_probe_lo = -4.0      # auto キャリブレーションのプローブ EV（明）
        self.exposure_probe_hi = -12.0     # auto キャリブレーションのプローブ EV（暗）

        # Matte 対象（独立）
        self.matte_actors = None           # 明示の actor オブジェクトリスト（最優先）
        self.matte_actor_names = None       # ラベル名リスト（次点）。両方Noneなら選択を使用
        # Object ID 対象（Matte とは別管理）
        self.objid_actors = None
        self.objid_actor_names = None

    def validate(self):
        """問題があれば警告文字列のリストを返す（空なら OK）。"""
        problems = []
        if self.camera_actor is None:
            problems.append("カメラが選択されていません。")
        if not self.output_dir:
            problems.append("出力先フォルダが指定されていません。")
        elif not os.path.isdir(self.output_dir):
            problems.append("出力先フォルダが存在しません: %s" % self.output_dir)
        if self.aa_factor not in (1, 2, 4):
            problems.append("AA 倍率は 1 / 2 / 4 のいずれかにしてください。")
        if not self.use_camera_resolution:
            if self.override_width <= 0 or self.override_height <= 0:
                problems.append("オーバーライド解像度が不正です。")
        if self.do_depth and self.depth_far <= self.depth_near:
            problems.append("Depth の Far は Near より大きくしてください。")
        if not (self.do_color or self.do_depth or self.do_matte or self.do_object_id):
            problems.append("出力パスが1つも選択されていません。")
        return problems


# ----------------------------------------------------------------------------
# エディタ / アクター取得ヘルパ（UE5.7/5.8 共通のサブシステム API）
# ----------------------------------------------------------------------------
def _get_editor_world():
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return ues.get_editor_world()


def _actor_subsystem():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def list_cameras():
    """シーン内の CameraActor / CineCameraActor を返す。
    get_all_level_actors() はトランジェント／アウトライナ非表示のアクターを除外するため、
    Sequencer がプレビュー用にスポーンしたスポーナブルカメラが漏れる。ワールドを直接
    走査する GameplayStatics の結果をマージして拾う（CineCameraActor は CameraActor の
    派生なので CameraActor だけで両方取れる）。"""
    cams = []
    seen = set()
    for a in _actor_subsystem().get_all_level_actors():
        if isinstance(a, (unreal.CameraActor, unreal.CineCameraActor)):
            cams.append(a)
            seen.add(a.get_path_name())
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            _get_editor_world(), unreal.CameraActor):
        if a.get_path_name() not in seen:
            cams.append(a)
            seen.add(a.get_path_name())
    return cams


def get_selected_actors():
    return list(_actor_subsystem().get_selected_level_actors())


def _camera_component(camera_actor):
    # ACameraActor / ACineCameraActor とも camera_component プロパティを持つ
    return camera_actor.camera_component


def set_camera_overscan_filmback(camera_actor, fx, fy):
    """カメラの filmback を 横×(1+fx)/縦×(1+fy) に拡大して overscan（縦横独立可）。
    焦点距離は変えないので FOV が広がり、元フレームは中央に保たれる。元の (sw,sh) を返す。"""
    comp = camera_actor.camera_component
    fb = comp.get_editor_property("filmback")
    sw = float(fb.get_editor_property("sensor_width"))
    sh = float(fb.get_editor_property("sensor_height"))
    fb.set_editor_property("sensor_width", sw * (1.0 + float(fx)))
    fb.set_editor_property("sensor_height", sh * (1.0 + float(fy)))
    comp.set_editor_property("filmback", fb)
    return (sw, sh)


def restore_camera_filmback(camera_actor, sw, sh):
    comp = camera_actor.camera_component
    fb = comp.get_editor_property("filmback")
    fb.set_editor_property("sensor_width", float(sw))
    fb.set_editor_property("sensor_height", float(sh))
    comp.set_editor_property("filmback", fb)


# ----------------------------------------------------------------------------
# 「ビューポートのルック」: ビューポートで見えている見た目で Beauty を撮る
# ----------------------------------------------------------------------------
def _same_actor(a, b):
    try:
        return a is not None and b is not None and a.get_path_name() == b.get_path_name()
    except Exception:
        return False


def viewport_pilots(camera_actor):
    """レベルビューポートがこのカメラをパイロット中なら True（Exact Camera View は問わない）。"""
    try:
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        return _same_actor(les.get_pilot_level_actor(), camera_actor)
    except Exception:
        return False


def viewport_shows_camera_pp(camera_actor):
    """レベルビューポートがこのカメラをパイロット中（Exact Camera View）なら True。
    そのときユーザーはカメラ自身のポスプロ込みの見た目を見ている。"""
    try:
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        return (_same_actor(les.get_pilot_level_actor(), camera_actor)
                and bool(les.get_exact_camera_view()))
    except Exception:
        return False


def zero_camera_pp_weights(cameras):
    """パイロット中でないカメラの post_process_blend_weight を一時 0 にする（ビューポートの
    見た目にはカメラ自身の露出補正・ビネット等が乗っていないため）。
    変更した (component, 元の値) のリストを返す（restore_camera_pp_weights で戻す）。"""
    saved = []
    for cam in cameras or []:
        try:
            if viewport_shows_camera_pp(cam):
                unreal.log("[SceneCapture] ビューポートのルック: %s はパイロット中のためカメラPPを維持"
                           % cam.get_actor_label())
                continue
            comp = cam.camera_component
            w = float(comp.get_editor_property("post_process_blend_weight"))
            if w == 0.0:
                continue
            comp.set_editor_property("post_process_blend_weight", 0.0)
            saved.append((comp, w))
        except Exception as e:
            unreal.log_warning("[SceneCapture] カメラPPウェイトを変更できません: %s" % e)
    return saved


def restore_camera_pp_weights(saved):
    for comp, w in saved or []:
        try:
            comp.set_editor_property("post_process_blend_weight", float(w))
        except Exception as e:
            unreal.log_warning("[SceneCapture] カメラPPウェイト復元に失敗: %s" % e)


def _take_high_res_screenshot(width, height, filename, delay):
    """AutomationLibrary.take_high_res_screenshot（5.7/5.8 同一シグネチャ:
    res_x, res_y, filename, camera, mask_enabled, capture_hdr, comparison_tolerance,
    comparison_notes, delay, force_game_view）。camera=None で呼ぶのは、渡すと完了時に
    エンジンがアクターロックを外し、ユーザーのパイロットまで解除するため。
    戻り値は完了判定用のタスク（無い場合は None = HighResShot コマンドで代用）。"""
    lib = getattr(unreal, "AutomationLibrary", None)
    fn = getattr(lib, "take_high_res_screenshot", None) if lib else None
    if fn is not None:
        try:
            return fn(int(width), int(height), filename, None, False, False,
                      unreal.ComparisonTolerance.LOW, "", float(delay), False)
        except TypeError:
            return fn(int(width), int(height), filename, delay=float(delay),
                      force_game_view=False)
    unreal.SystemLibrary.execute_console_command(
        None, 'HighResShot %dx%d filename="%s"' % (int(width), int(height), filename))
    return None


def begin_viewport_grab(camera_actor, width, height, filename, delay=0.5):
    """レベルビューポートでカメラをパイロットし、ゲームビューで width×height の
    HighResShot を撮り始める（非同期）。返り値の dict:
      poll()    -> True=ファイル完成 / False=待機中（例外=失敗）
      restore() -> パイロット・ゲームビュー・Exact Camera View・ビューポート視点・
                   カメラPPウェイト・アスペクト拘束を元に戻す（冪等）
    ビューポートが無い（コマンドレット）・別カメラにシネマティックロック中などで
    パイロットできなければ restore 済みで RuntimeError。"""
    import capture_mrq
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    prev_pilot = les.get_pilot_level_actor()
    prev_game = bool(les.editor_get_game_view())
    prev_exact = bool(les.get_exact_camera_view())
    keep_pp = viewport_shows_camera_pp(camera_actor)
    vp_info = None
    try:
        r = ues.get_level_viewport_camera_info()
        if r and len(r) == 2:
            vp_info = r
        elif r and len(r) == 3 and r[0]:
            vp_info = (r[1], r[2])
    except Exception:
        vp_info = None
    st = {"done": False, "pp": [], "asp": None, "piloted": False}

    def restore():
        if st["done"]:
            return
        st["done"] = True
        restore_camera_pp_weights(st["pp"])
        if st["asp"] is not None:
            capture_mrq._restore_cameras_aspect([st["asp"]])
        try:
            if st["piloted"]:
                if st.get("repilot"):
                    les.pilot_level_actor(prev_pilot)
                else:
                    les.eject_pilot_level_actor()
                    # シネマティックロック中は eject が自分のロックを外さないため直接解除
                    les.pilot_level_actor(None)
            les.set_exact_camera_view(prev_exact)
            les.editor_set_game_view(prev_game)
            if vp_info is not None and prev_pilot is None:
                ues.set_level_viewport_camera_info(vp_info[0], vp_info[1])
        except Exception as e:
            unreal.log_warning("[SceneCapture] ビューポート状態の復元に失敗: %s" % e)

    try:
        if not keep_pp:
            st["pp"] = zero_camera_pp_weights([camera_actor])
        # 出力アスペクト≠filmback の黒帯を MRQ 側と同じ規則で防ぐ
        st["asp"] = capture_mrq._fill_aspect_comp(camera_actor, width, height)
        if not _same_actor(prev_pilot, camera_actor):
            les.pilot_level_actor(camera_actor)
            st["piloted"] = True
            if not _same_actor(les.get_pilot_level_actor(), camera_actor):
                raise RuntimeError("ビューポートでカメラをパイロットできません"
                                   "（ビューポート無し / 別カメラにロック中）")
            # パイロットが効いた＝直前のロックは通常パイロット（シネマティックではない）
            st["repilot"] = prev_pilot is not None
        les.set_exact_camera_view(True)
        les.editor_set_game_view(True)
        if st["piloted"]:
            # 視点が飛んだ直後は自動露出・TSR/Lumen の履歴が落ち着いていないので長めに待つ
            delay = max(float(delay), 2.0)
        if os.path.isfile(filename):
            os.remove(filename)
        st["task"] = _take_high_res_screenshot(width, height, filename, delay)
    except Exception:
        restore()
        raise
    st["size"] = -1

    def poll():
        task = st.get("task")
        if task is not None and not task.is_task_done():
            return False
        if not os.path.isfile(filename):
            return False
        # 書き込み途中を拾わないよう、サイズが 2 tick 続けて同じになるまで待つ
        sz = os.path.getsize(filename)
        if sz <= 0 or sz != st["size"]:
            st["size"] = sz
            return False
        return True

    st["poll"] = poll
    st["restore"] = restore
    return st


def finish_viewport_grab_file(src_png, dst_path):
    """HighResShot の PNG を Beauty の最終名・形式へ（α は捨てて RGB。JPG は変換）。"""
    if _HAS_PIL:
        from PIL import Image
        with Image.open(src_png) as im0:
            im = im0.convert("RGB")
        if dst_path.lower().endswith(".jpg"):
            im.save(dst_path, quality=95)
        else:
            im.save(dst_path)
        os.remove(src_png)
    else:
        os.replace(src_png, dst_path)
    return dst_path


def blend_exposure_bias(default_bias, volumes):
    """ポストプロセスの露出補正（auto_exposure_bias）の合成値。volumes は
    (priority, blend_weight, overrides_bias, bias) の列。エンジンと同じく priority の
    低い順に、上書きしている物だけ lerp(現在値, bias, weight) で重ねる。"""
    b = float(default_bias)
    for _p, w, ov, bias in sorted(volumes, key=lambda v: v[0]):
        if ov:
            w = max(0.0, min(1.0, float(w)))
            b = b + (float(bias) - b) * w
    return b


def viewport_look_exposure_offset(base_bias, cam_overrides_bias, cam_bias, cam_weight,
                                  cam_pp_in_render, cam_pp_in_viewport):
    """ビューポートのルック（MRQ 近似）で r.ExposureOffset に足す EV。
    ビューポートはパイロット中でなければカメラのポスプロを使わない。近似側でカメラの
    ウェイトを 0 にできなかったカメラ（スポーナブル等）は、レンダにだけカメラの
    露出補正 lerp(base, cam, w) が乗るので、その差 -w×(cam−base) を打ち消す。
    両方に乗る/どちらにも乗らない/カメラが露出補正を上書きしていない → 0。"""
    if not cam_overrides_bias or bool(cam_pp_in_render) == bool(cam_pp_in_viewport):
        return 0.0
    w = max(0.0, min(1.0, float(cam_weight)))
    d = w * (float(cam_bias) - float(base_bias))
    return -d if cam_pp_in_render else d


def get_camera_settings(camera_actor):
    """カメラの Transform / FOV / アスペクト / PostProcess を取得。
    aspect_ratio が未設定(≒0)のカメラは filmback (sensor w/h) から算出する。"""
    cam_comp = _camera_component(camera_actor)
    asp = float(cam_comp.get_editor_property("aspect_ratio"))
    if asp <= 0.01:
        try:
            fb = cam_comp.get_editor_property("filmback")
            sh = float(fb.get_editor_property("sensor_height"))
            if sh > 0:
                asp = float(fb.get_editor_property("sensor_width")) / sh
        except Exception:
            pass
    return {
        "transform": camera_actor.get_actor_transform(),
        "fov": float(cam_comp.get_editor_property("field_of_view")),
        "aspect_ratio": asp,
        "post_process": cam_comp.get_editor_property("post_process_settings"),
    }


# ----------------------------------------------------------------------------
# RenderTarget / SceneCapture 生成
# ----------------------------------------------------------------------------
def _make_render_target(world, width, height, fmt, linear_gamma):
    """Transient な TextureRenderTarget2D を生成。"""
    rt = unreal.RenderingLibrary.create_render_target2d(
        world, int(width), int(height), fmt,
        unreal.LinearColor(0.0, 0.0, 0.0, 0.0), linear_gamma)
    if rt is None:
        raise RuntimeError("RenderTarget の生成に失敗しました (%dx%d)" % (width, height))
    return rt


# エディタ専用の表示要素（ゲーム実行時には出ないもの）。これらを OFF にして
# 「ゲーム中と同じ絵」にする。アクターアイコン=Sprites/BillboardSprites など。
_EDITOR_ONLY_SHOW_FLAGS = [
    "BillboardSprites", "Sprites", "Grid", "EditorPrimitives",
    "Gizmos", "LightRadius", "Cameras", "Selection", "SelectionOutline",
    "Snap", "Bounds", "ModeWidgets", "HelperPrimitives", "MeshEdges",
]


def _apply_game_show_flags(comp, fog_off=False):
    """キャプチャコンポーネントのエディタ専用表示を OFF にする（ゲーム表示相当）。
    fog_off=True なら Fog / 大気 / ボリューメトリックフォグ に加え、ポスプロの
    グロー(Bloom) も OFF にする（UI の「Fogなし」1個に束ねている）。"""
    try:
        names = list(_EDITOR_ONLY_SHOW_FLAGS)
        if fog_off:
            # 大気の ShowFlag は UE5 では "Atmosphere"（"AtmosphericFog" は UE4 名で無視される）
            names += ["Fog", "Atmosphere", "AtmosphericFog", "VolumetricFog", "Bloom"]
        settings = []
        for name in names:
            try:
                fs = unreal.EngineShowFlagsSetting()
                fs.set_editor_property("show_flag_name", name)
                fs.set_editor_property("enabled", False)
                settings.append(fs)
            except Exception as e:
                _warn_once("showflag:" + name, "ShowFlag %s を設定できません: %s" % (name, e))
        if settings:
            comp.set_editor_property("show_flag_settings", settings)
    except Exception as e:
        _warn("ShowFlags(ゲーム表示) 設定に失敗: %s" % e)


def _spawn_capture(world, transform, fov, rt, source,
                   show_only_actors=None, hidden_actors=None, post_process=None,
                   clip_base=None, clip_normal=None, fog_off=False):
    """SceneCapture2D アクターを Spawn して1フレームキャプチャし、アクターを返す。
    clip_base/clip_normal を渡すと、その平面の normal-負側（手前）を描画時にクリップする。
    fog_off=True で Fog を OFF にして撮る。"""
    actor = _spawn_transient_capture_actor(transform)
    try:
        _setup_and_capture(actor, fov, rt, source, show_only_actors, hidden_actors,
                           post_process, clip_base, clip_normal, fog_off)
    except Exception:
        # 呼び出し側の spawned へ入る前に失敗するとレベルに残るのでここで破棄する
        _destroy_actors([actor])
        raise
    return actor


def _spawn_transient_capture_actor(transform):
    """SceneCapture2D を transient で Spawn する（レベルを dirty にせず Undo にも積まない）。"""
    loc = transform.translation
    rot = transform.rotation.rotator()
    sub = _actor_subsystem()
    try:
        actor = sub.spawn_actor_from_class(unreal.SceneCapture2D, loc, rot, transient=True)
    except TypeError:
        actor = sub.spawn_actor_from_class(unreal.SceneCapture2D, loc, rot)
    if actor is None:
        raise RuntimeError("SceneCapture2D の Spawn に失敗しました。")
    return actor


def _setup_and_capture(actor, fov, rt, source, show_only_actors, hidden_actors,
                       post_process, clip_base, clip_normal, fog_off):
    comp = actor.capture_component2d
    comp.set_editor_property("fov_angle", float(fov))
    comp.set_editor_property("texture_target", rt)
    comp.set_editor_property("capture_source", source)
    comp.set_editor_property("capture_every_frame", False)
    comp.set_editor_property("capture_on_movement", False)
    _apply_game_show_flags(comp, fog_off=fog_off)   # エディタ専用表示（＋任意でFog）を消す

    if clip_base is not None and clip_normal is not None:
        # クリッププレーン: normal の指す側（＝奥）を残し、反対側（＝手前）を描画時に除去する。
        # レンダー時クリップなのでフォリッジ等アクター非表示で消せないものも消える。
        comp.set_editor_property("enable_clip_plane", True)
        comp.set_editor_property("clip_plane_base", clip_base)
        comp.set_editor_property("clip_plane_normal", clip_normal)

    if post_process is not None:
        comp.set_editor_property("post_process_settings", post_process)
        comp.set_editor_property("post_process_blend_weight", 1.0)

    if show_only_actors is not None:
        comp.set_editor_property(
            "primitive_render_mode",
            unreal.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST)
        # UE5.7: show_only_actors は set_editor_property 不可。専用メソッドで登録する
        comp.clear_show_only_components()
        for sa in show_only_actors:
            comp.show_only_actor_components(sa)

    if hidden_actors:
        # 既定 PrimitiveRenderMode（全描画）のまま、指定アクターだけ非表示にする
        comp.clear_hidden_components()
        for ha in hidden_actors:
            comp.hide_actor_components(ha)

    comp.capture_scene()


def _destroy_actors(actors):
    sub = _actor_subsystem()
    for a in actors:
        try:
            if a is not None:
                sub.destroy_actor(a)
        except Exception as e:
            _warn("アクター破棄に失敗: %s" % e)


# ----------------------------------------------------------------------------
# 一時ファイル経由のピクセル読み出し
# ----------------------------------------------------------------------------
def _temp_dir():
    d = os.path.join(unreal.Paths.project_saved_dir(), "UE5CaptureTmp")
    d = os.path.normpath(d)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _export_rt(world, rt, ext):
    """RenderTarget を一時ファイルへ書き出し、絶対パスを返す。"""
    tdir = _temp_dir()
    # export_render_target は失敗しても例外を出さないので、名前を毎回一意にし
    # 既存ファイルを消してから書く（古い画像を今回のキャプチャとして読まない）
    fname = "rt_tmp_%s%s" % (uuid.uuid4().hex, ext)
    path = os.path.join(tdir, fname)
    if os.path.isfile(path):
        os.remove(path)
    unreal.RenderingLibrary.export_render_target(world, rt, tdir, fname)
    if not os.path.isfile(path):
        raise RuntimeError("RenderTarget のエクスポートに失敗しました: %s" % path)
    return path


def _read_ldr(path, delete=False):
    """PNG など LDR 画像を numpy uint8 (H,W,C) で読む。delete=True なら読んだ後に消す。"""
    with _PILImage.open(path) as img:
        arr = _np.array(img)
    if delete:
        try:
            os.remove(path)
        except Exception as e:
            _warn("一時ファイルを削除できません %s: %s" % (path, e))
    return arr


def _release_rt(rt):
    """RenderTarget の GPU リソースを即時解放する（GC はフレーム毎ループ中に走らない）。"""
    try:
        unreal.RenderingLibrary.release_render_target2d(rt)
    except Exception as e:
        _warn_once("release_rt", "RenderTarget の解放に失敗: %s" % e)


# ----------------------------------------------------------------------------
# RenderTarget 直接読み出し（UE5.7: .hdr 経由不要。read_render_target_raw を使う）
#   - R32F へ SceneDepth を書くと UE5.7 では壊れる（全画素一定値）。
#     深度は RGBA16F に撮り、ここで R チャンネル（cm）を取り出す。
# ----------------------------------------------------------------------------
def _read_rt_raw_r(world, rt, width, height):
    """RenderTarget の R チャンネルを生値のまま numpy float32 (H,W) で返す。"""
    samples = unreal.RenderingLibrary.read_render_target_raw(world, rt, False)
    n = width * height
    if len(samples) < n:
        raise RuntimeError("RenderTarget の読み出しが不足 (%d / %d 画素)" % (len(samples), n))
    a = _np.fromiter((c.r for c in samples), dtype=_np.float32, count=n)
    return a.reshape(height, width)


# 深度の「空/未ヒット」判定しきい値。RGBA16F(half) の未ヒット値 ~65504 を検出する。
# ⚠️ これは同時に「600m より遠い実ジオメトリも空とみなす」ことを意味する。
# RGBA16F は 65504cm(655m) で飽和するので 16F ではこれ以上は分離できない。
_DEPTH_FAR_CM = 60000.0
# RGBA32F で撮ったときの「空/ファープレーン」しきい値（measure_depth_range と同値）。
# 32F なら 655m を超える実ジオメトリと空を区別できる。
_DEPTH_FAR_32F = 1.0e7


def _visible_mask(target_depth, full_depth, far=_DEPTH_FAR_CM):
    """show-only 深度と全シーン深度の一致で「対象が最前面に見える画素」の bool マスクを返す。
    手前に非対象物がある画素は target と full の差が許容値を超えて除外される（オクルージョン考慮）。
    far は「空/未ヒット」しきい値（32F で撮った深度なら _DEPTH_FAR_32F を渡す）。"""
    tol = _np.maximum(full_depth * 0.02, 10.0)
    return (target_depth < far) & (_np.abs(target_depth - full_depth) <= tol)


def _render_depth_r(world, cam, w, h, spawned, show_only_actors=None,
                    hidden_actors=None, fmt=None, clip_base=None, clip_normal=None):
    """SCS_SCENE_DEPTH を1枚撮り、R チャンネル（cm 距離）を float32 (H,W) で返す。
    UE5.7: SceneDepth を R32F へ撮ると全画素一定値になる不具合があるため既定は
    RGBA16F（⚠️ FP16 上限 65504cm≈655m で飽和する）。fmt=RTF_RGBA32F を渡すと
    655m 超も測れる（4ch 32F は R32F バグの対象外か要実測）。
    clip_base/clip_normal で描画時クリップ面（_spawn_capture と同じ）。"""
    rt = _make_render_target(world, w, h,
                             fmt or unreal.TextureRenderTargetFormat.RTF_RGBA16F,
                             True)
    try:
        actor = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                               unreal.SceneCaptureSource.SCS_SCENE_DEPTH,
                               show_only_actors=show_only_actors, hidden_actors=hidden_actors,
                               clip_base=clip_base, clip_normal=clip_normal)
        spawned.append(actor)
        return _read_rt_raw_r(world, rt, w, h)
    finally:
        _release_rt(rt)


def _depth_rt_broken(d):
    """32F 深度が R32F 系バグ（全画素一定値）に該当するか。
    全画素が空/未ヒット（板が画面外のフレーム等）でも ptp=0 になるので、それは
    バグ扱いしない（誤判定すると以降ずっと 16F に固定され 655m 超が抜ける）。"""
    if d is None or d.size == 0:
        return False
    if float(d.min()) >= _DEPTH_FAR_32F:
        return False
    return float(_np.ptp(d)) < 1e-3


def _depth_fmt(state):
    return (unreal.TextureRenderTargetFormat.RTF_RGBA32F if state.get("use32", True)
            else unreal.TextureRenderTargetFormat.RTF_RGBA16F)


def _render_depth_r_auto(world, cam, w, h, spawned, state, label="Depth", **kw):
    """_render_depth_r を RGBA32F で撮り、R32F 系バグ（全画素一定値）なら RGBA16F で
    撮り直す。state={"use32": True} を呼び出し側で共有すると、一度 16F に落ちた後は
    以降の呼び出しも 16F で撮る（フレーム毎に 32F を無駄撃ちしない）。
    戻り値 (depth, far): far はその深度に使う空/未ヒットしきい値
    （32F=_DEPTH_FAR_32F / 16F=_DEPTH_FAR_CM）。kw は _render_depth_r へそのまま渡す。"""
    if state.get("use32", True):
        d = _render_depth_r(world, cam, w, h, spawned,
                            fmt=unreal.TextureRenderTargetFormat.RTF_RGBA32F, **kw)
        if not _depth_rt_broken(d):
            return d, _DEPTH_FAR_32F
        _warn("%s: RGBA32F が定数値（深度RTバグ該当）→ RGBA16F へ退避"
              "（655m 超と空は飽和）" % label)
        state["use32"] = False
    return _render_depth_r(world, cam, w, h, spawned, **kw), _DEPTH_FAR_CM


def measure_depth_range(camera_actor, w=384, h=216,
                        lo_pct=0.5, hi_pct=99.5, pad=0.05):
    """Depth 自動レンジ: SceneCapture 深度を低解像度で1枚撮り、可視ジオメトリの
    最近/最遠距離 (cm) をロバスト百分位で推定する。空/ファープレーン(≥1e7)は除外。
    pad は両端の余白率（クリップ防止）。値は2有効数字へ丸める（near切下げ/far切上げ）。
    戻り値 (near_cm, far_cm)。有効画素が無ければ None。
    ※ バウンズ走査でなく実描画ベース: 遮蔽・マスク材・巨大スカイ球・LevelInstance
    列挙の罠を全て回避できる（現在のシーン状態＝現在フレームで測る）。"""
    if not _HAS_NUMPY:
        return None
    world = _get_editor_world()
    cam = get_camera_settings(camera_actor)
    spawned = []
    try:
        # RGBA32F で FP16 の 655m 飽和を回避（R32F 単チャンネルのみのバグ）。
        # 万一 32F が全画素一定（バグ該当）なら 16F に落として続行
        d, far_th = _render_depth_r_auto(world, cam, int(w), int(h), spawned,
                                         {"use32": True}, label="Depth自動レンジ")
    finally:
        _destroy_actors(spawned)
    if d is None:
        return None
    v = d[(d > 1.0) & (d < far_th)]
    if v.size < 64:
        return None
    near = float(_np.percentile(v, lo_pct)) * (1.0 - pad)
    far = float(_np.percentile(v, hi_pct)) * (1.0 + pad)
    if far <= near:
        return None

    def _round_sig(x, up):
        if x <= 0:
            return 0.0
        import math
        exp = math.floor(math.log10(x)) - 1
        q = 10.0 ** exp
        f = math.ceil if up else math.floor
        return f(x / q) * q

    near = max(_round_sig(near, up=False), 0.0)
    far = _round_sig(far, up=True)
    _log("Depth自動レンジ: near=%.0fcm far=%.0fcm (%.1fm〜%.1fm, 有効%.0f%%)"
         % (near, far, near / 100.0, far / 100.0,
            100.0 * v.size / d.size))
    return near, far


def _render_final_color(world, settings, cam, w, h, aa, spawned,
                        hidden_actors=None, clip_base=None, clip_normal=None,
                        fog_off=False):
    """SCS_FINAL_COLOR_LDR を w*aa × h*aa で撮り、AA ダウンスケール済みの
    float 配列 (H,W,C) を返す。露出は settings.exposure_mode に従う。"""
    pp = _resolve_color_pp(world, settings, cam)
    rt = _make_render_target(world, w * aa, h * aa,
                             unreal.TextureRenderTargetFormat.RTF_RGBA8, False)
    try:
        actor = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                               unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR,
                               hidden_actors=hidden_actors, post_process=pp,
                               clip_base=clip_base, clip_normal=clip_normal,
                               fog_off=fog_off)
        spawned.append(actor)
        arr = _read_ldr(_export_rt(world, rt, ".png"), delete=True)
    finally:
        _release_rt(rt)
    return _downscale(arr, aa)


# ----------------------------------------------------------------------------
# 露出（手動 EV 固定 ＋ 自動キャリブレーション）
# ----------------------------------------------------------------------------
def _manual_exposure_pp(bias):
    """eye-adaptation を切って手動 EV を固定した PostProcessSettings を返す。"""
    pp = unreal.PostProcessSettings()
    pp.set_editor_property("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL)
    pp.set_editor_property("auto_exposure_bias", float(bias))
    pp.set_editor_property("override_auto_exposure_method", True)
    pp.set_editor_property("override_auto_exposure_bias", True)
    return pp


def _probe_luma(world, cam, bias, pw=160, ph=90):
    """小サイズで1枚撮り、median 輝度(0-255 sRGB)を返す（露出キャリブレーション用）。"""
    rt = _make_render_target(world, pw, ph,
                             unreal.TextureRenderTargetFormat.RTF_RGBA8, False)
    actor = None
    try:
        actor = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                               unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR,
                               post_process=_manual_exposure_pp(bias))
        RL = unreal.RenderingLibrary
        vals = []
        for yy in range(4, ph, 8):
            for xx in range(4, pw, 8):
                px = RL.read_render_target_pixel(world, rt, xx, yy)
                vals.append((px.r + px.g + px.b) / 3.0)
    finally:
        _destroy_actors([actor])
        _release_rt(rt)
    vals.sort()
    return vals[len(vals) // 2] if vals else 0.0


def _resolve_exposure_bias(world, settings, cam):
    """反復プローブで目標 median 輝度に合う EV を求めて返す（auto 用）。
    トーンマッパーは非線形なので、測定→補正→再測定を数回まわして収束させる。"""
    target = max(1.0, float(settings.exposure_target))
    PER_EV = 1.43          # 経験則: より負へ1EVで median が約×1.43 明るくなる
    ev = -7.0
    m = _probe_luma(world, cam, ev)
    for _ in range(5):
        m = max(_probe_luma(world, cam, ev), 0.5)
        if abs(m - target) <= max(3.0, target * 0.05):
            break
        ev = ev - math.log(target / m) / math.log(PER_EV)   # 暗ければより負＝明るく
        ev = max(-20.0, min(2.0, ev))
    _log("露出キャリブレーション: EV %.2f (median≈%.0f / target %.0f)" % (ev, m, target))
    return ev


# ----------------------------------------------------------------------------
# numpy 後処理
# ----------------------------------------------------------------------------
def _downscale(arr, n):
    """N×N ボックスフィルタでダウンスケール（Spatial Supersample）。float を返す。"""
    if n <= 1:
        return arr.astype(_np.float32)
    h, w = arr.shape[0], arr.shape[1]
    h2, w2 = h // n, w // n
    arr = arr[:h2 * n, :w2 * n]
    if arr.ndim == 3:
        c = arr.shape[2]
        return arr.reshape(h2, n, w2, n, c).astype(_np.float32).mean(axis=(1, 3))
    return arr.reshape(h2, n, w2, n).astype(_np.float32).mean(axis=(1, 3))


def _write_png_u8(path, arr):
    a = _np.clip(_np.rint(arr), 0, 255).astype(_np.uint8)
    if a.ndim == 2:
        _PILImage.fromarray(a, "L").save(path)
    elif a.shape[2] == 4:
        _PILImage.fromarray(a, "RGBA").save(path)
    elif a.shape[2] == 3:
        _PILImage.fromarray(a, "RGB").save(path)
    else:
        _PILImage.fromarray(a[:, :, 0], "L").save(path)


def _write_png_u16_gray(path, arr01):
    """0..1 正規化済み配列を 16bit グレースケール PNG で書く。"""
    a = _np.clip(_np.rint(arr01 * 65535.0), 0, 65535).astype(_np.uint16)
    # uint16 の 2D 配列は mode 指定なしで I;16 になる。mode="I;16" の明示は
    # Pillow 13 で廃止（ValueError: Invalid mode for data type）。
    _PILImage.fromarray(a).save(path)


def _write_exr_gray(path, arr):
    """単一チャンネル float を 32bit float EXR で書く。失敗時 False。
    imageio(FreeImage) は flags 無しだと half で書く（65504cm≈655m 超が inf になり
    50000cm も 49984 に量子化される実測）ので EXR_FLOAT を明示する。"""
    try:
        import imageio.v2 as _iio
        from imageio.plugins.freeimage import IO_FLAGS
        _iio.imwrite(path, arr.astype(_np.float32), flags=IO_FLAGS.EXR_FLOAT)
        return True
    except Exception as e:
        _warn_once("exr_imageio", "imageio で EXR を書けません（OpenEXR を試します）: %s" % e)
    try:
        import OpenEXR
        import Imath
        h, w = arr.shape[0], arr.shape[1]
        hdr = OpenEXR.Header(w, h)
        fch = Imath.Channel(Imath.PixelType(Imath.PixelType.FLOAT))
        hdr["channels"] = {"R": fch, "G": fch, "B": fch}
        out = OpenEXR.OutputFile(path, hdr)
        data = _np.ascontiguousarray(arr, dtype=_np.float32).tobytes()
        out.writePixels({"R": data, "G": data, "B": data})
        out.close()
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# ファイル名
# ----------------------------------------------------------------------------
def _timestamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def next_take_number(output_dir):
    """出力フォルダ内の既存の連番(_NNN_ / _NNN. / フォルダ名末尾 _NNN)を調べ、
    次の take 番号を返す。設定違いを上書きせず複数出力するための通し番号。
    テイク毎サブフォルダ（シーケンスレンダ）もフォルダ名で走査する。"""
    if not output_dir or not os.path.isdir(output_dir):
        return 1
    mx = 0
    pat = re.compile(r"_(\d{3})(?=[._])")
    dirpat = re.compile(r"_(\d{3})$")
    for f in os.listdir(output_dir):
        if os.path.isdir(os.path.join(output_dir, f)):
            m = dirpat.search(f)
            if m:
                mx = max(mx, int(m.group(1)))
        elif f.lower().endswith((".png", ".exr", ".jpg", ".jpeg", ".json", ".mp4")):
            for m in pat.finditer(f):
                mx = max(mx, int(m.group(1)))
    return mx + 1


def _safe_name(s):
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(s)).strip("_")


NAME_TOKENS = ("level", "cut", "camera", "seq")
DEFAULT_NAME_TEMPLATE = "{level}_{cut}_{camera}"


def level_name(world=None):
    """永続レベル（開いているマップ）の名前。Untitled なら "Untitled"。"""
    world = world or _get_editor_world()
    try:
        return world.get_name()
    except Exception:
        return ""


def _digit_groups(name):
    return re.findall(r"\d+", str(name))


def _level_package(level):
    # "/Game/.../LV_047_Environment.LV_047_Environment:PersistentLevel" -> package 部分
    return str(level.get_path_name()).split(".")[0]


def detect_cut_number(world=None):
    """サブレベル群からカット番号を推定する。
    サブレベルの親フォルダ名（例 01_047_Forest）とサブレベル名（LV_047_Environment）に
    共通する数字列を各サブレベルから取り、多数決で選ぶ。表示状態が取れるときは
    表示中のサブレベルだけを数える（MASTER が複数カットを束ねる構成への対応）。
    サブレベルが無ければ永続レベル名の数字列（LV_047_MASTER → 047）。見つからなければ ""。"""
    world = world or _get_editor_world()
    try:
        levels = list(unreal.EditorLevelUtils.get_levels(world))
    except Exception:
        levels = []
    try:
        persistent_pkg = str(world.get_path_name()).split(".")[0]
    except Exception:
        persistent_pkg = ""
    votes = {}
    visible_votes = {}
    for lv in levels:
        try:
            pkg = _level_package(lv)
        except Exception:
            continue
        if not pkg or pkg == persistent_pkg:
            continue
        folder = pkg.rsplit("/", 1)[0].rsplit("/", 1)[-1]
        name = pkg.rsplit("/", 1)[-1]
        common = [g for g in _digit_groups(folder) if g in set(_digit_groups(name))]
        cands = common or _digit_groups(folder)
        if not cands:
            continue
        # 同じ数字が複数あるとき（01_047 と LV_047）は長い方＝カット番号を優先
        cand = max(cands, key=len)
        votes[cand] = votes.get(cand, 0) + 1
        vis = None
        try:
            sl = unreal.GameplayStatics.get_streaming_level(world, pkg)
            if sl is not None:
                vis = bool(sl.is_level_visible())
        except Exception:
            vis = None
        if vis:
            visible_votes[cand] = visible_votes.get(cand, 0) + 1
    pool = visible_votes or votes
    if pool:
        return max(sorted(pool), key=lambda k: pool[k])
    for g in _digit_groups(level_name(world)):
        if len(g) >= 2:
            return g
    return ""


def resolve_name_template(template, **tokens):
    """"{level}_{cut}_{camera}" 形式のテンプレートを解決し、ファイル名に安全な名前本体を返す。
    未知のトークンや空の値は消え、余った区切りの "_" は畳む。全部空なら "capture"。"""
    class _D(dict):
        def __missing__(self, k):
            return ""
    vals = _D((k, _safe_name(v) if v is not None else "") for k, v in tokens.items())
    try:
        s = str(template or DEFAULT_NAME_TEMPLATE).format_map(vals)
    except Exception:
        # "{level.x}" "{0}" "{a" など書式として壊れたテンプレートは文字どおりに使う
        s = str(template)
    s = _safe_name(s)
    s = re.sub(r"_{2,}", "_", s).strip("_")
    return s or "capture"


def resolve_path_template(path, **tokens):
    """出力フォルダのテンプレート。"{" を含む区切り（フォルダ名）だけを resolve_name_template で
    解決する（同じトークン・同じ無害化・壊れた書式は文字どおり・空なら "capture"）。
    トークンの無いパスは一切変えない。"""
    if "{" not in (path or ""):
        return path
    parts = re.split(r"([\\/])", str(path))
    return "".join(resolve_name_template(p, **tokens) if "{" in p else p for p in parts)


def latest_take_number(output_dir, name_body):
    """output_dir 内で name_body に属する既存テイクの最大番号（無ければ 0）。
    ファイル name_body_素材_NNN.* と、テイク毎サブフォルダ name_body_NNN を見る。"""
    if not output_dir or not os.path.isdir(output_dir) or not name_body:
        return 0
    mx = 0
    fpat = _take_file_pattern(name_body, None)
    dpat = re.compile(r"^" + re.escape(name_body) + r"_(\d{3})$")
    for f in os.listdir(output_dir):
        full = os.path.join(output_dir, f)
        m = (dpat if os.path.isdir(full) else fpat).match(f)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx


def _take_file_pattern(name_body, take_str):
    """name_body_<素材名>_NNN(.ext | .frame.ext | _tag…) に一致する正規表現。素材名に "_" は無いので、
    名前本体が別の名前の接頭辞（A と A_B）でも相手のファイルには一致しない。take_str=None なら任意の NNN。"""
    take = re.escape(take_str) if take_str else r"\d{3}"
    return re.compile(r"^" + re.escape(name_body) + r"_[A-Za-z0-9]+_(" + take + r")(?=[._]|$)")


def delete_take_outputs(output_dir, name_body, take_str):
    """「上書き」用: name_body のテイク take_str の既存出力を消す。
    対象はテイク毎サブフォルダ name_body_NNN（丸ごと）、直下の name_body_*_NNN.* ファイル、
    直下の素材サブフォルダ *_NNN / *_NNN_* の中の name_body_* ファイル（空になれば削除）。
    他の名前のファイルには触れない。消した数を返す。"""
    import shutil
    n = 0
    if not output_dir or not os.path.isdir(output_dir) or not name_body:
        return 0
    sub = os.path.join(output_dir, "%s_%s" % (name_body, take_str))
    if os.path.isdir(sub):
        shutil.rmtree(sub, ignore_errors=True)
        n += 1
    fpat = _take_file_pattern(name_body, take_str)
    dpat = re.compile(r"_" + re.escape(take_str) + r"(_|$)")
    for f in os.listdir(output_dir):
        full = os.path.join(output_dir, f)
        if os.path.isfile(full):
            if fpat.match(f):
                try:
                    os.remove(full)
                    n += 1
                except OSError:
                    pass
        elif dpat.search(f) and f != os.path.basename(sub):
            removed_here = 0
            for g in os.listdir(full):
                if fpat.match(g):
                    try:
                        os.remove(os.path.join(full, g))
                        removed_here += 1
                    except OSError:
                        pass
            n += removed_here
            # 自分が空にしたフォルダだけ畳む（他の名前の空フォルダには触れない）
            if removed_here:
                try:
                    if not os.listdir(full):
                        os.rmdir(full)
                except OSError:
                    pass
    return n


def out_basename(settings, pass_type, take):
    """ファイル名（拡張子なし）を 名前本体_素材名_NNN で組む。
    名前本体は settings.name_body（テンプレート解決済み）。空なら旧式の
    任意名(name_prefix)_カメラ名(name_include_camera) で組む（capture_tool.py 互換）。"""
    parts = []
    body = (getattr(settings, "name_body", "") or "").strip()
    if body:
        parts.append(_safe_name(body))
    else:
        pre = (settings.name_prefix or "").strip()
        if pre:
            parts.append(_safe_name(pre))
        if settings.name_include_camera and settings.camera_actor is not None:
            parts.append(_safe_name(settings.camera_actor.get_actor_label()))
    parts.append(pass_type)
    parts.append(str(take))
    return "_".join(parts)


def _out_path(settings, ts, pass_type, ext):
    return os.path.join(settings.output_dir, out_basename(settings, pass_type, ts) + ext)


# ----------------------------------------------------------------------------
# CustomDepthStencil（Matte 用：仕様準拠で一時設定→復元）
# ----------------------------------------------------------------------------
def _set_custom_depth(actors, enable, stencil=1):
    """選択アクター配下の PrimitiveComponent に CustomDepth/Stencil を設定。
    返り値: 復元用の (component, prev_enable, prev_stencil) リスト。"""
    saved = []
    for a in actors:
        if a is None:
            continue
        for comp in a.get_components_by_class(unreal.PrimitiveComponent):
            try:
                prev_e = comp.get_editor_property("render_custom_depth")
                prev_s = comp.get_editor_property("custom_depth_stencil_value")
                saved.append((comp, prev_e, prev_s))
                comp.set_editor_property("render_custom_depth", bool(enable))
                if enable:
                    comp.set_editor_property("custom_depth_stencil_value", int(stencil))
            except Exception as e:
                _warn("CustomDepth 設定に失敗: %s" % e)
    return saved


def _restore_custom_depth(saved):
    for comp, prev_e, prev_s in saved:
        try:
            comp.set_editor_property("render_custom_depth", prev_e)
            comp.set_editor_property("custom_depth_stencil_value", prev_s)
        except Exception as e:
            _warn("CustomDepth 復元に失敗: %s" % e)


# ----------------------------------------------------------------------------
# 各パス
# ----------------------------------------------------------------------------
def _resolve_resolution(settings, cam):
    if settings.use_camera_resolution:
        aspect = cam["aspect_ratio"] if cam["aspect_ratio"] > 0.01 else (16.0 / 9.0)
        h = int(settings.base_height)
        w = int(round(h * aspect))
        return w, h
    return int(settings.override_width), int(settings.override_height)


def _resolve_color_pp(world, settings, cam):
    """露出モードに応じて Color(Fill) 用の PostProcessSettings を返す。"""
    mode = settings.exposure_mode
    if mode == "scene":
        _log("Color 露出: scene（カメラ/ビューポート設定）")
        return cam["post_process"]
    if mode == "manual":
        bias = float(settings.exposure_bias if settings.exposure_bias is not None else -8.0)
        _log("Color 露出: manual EV %.2f" % bias)
        return _manual_exposure_pp(bias)
    return _manual_exposure_pp(_resolve_exposure_bias(world, settings, cam))  # auto


def _render_fill_rgb(world, settings, cam, w, h, aa, spawned):
    """Color(Fill) を撮って RGB(=H,W,3, 0..255) を返す（Matte 合成用に共有）。"""
    arr = _render_final_color(world, settings, cam, w, h, aa, spawned)
    return arr[:, :, :3] if (arr.ndim == 3 and arr.shape[2] >= 3) else arr


def _force_opaque(arr):
    """FinalColor のアルファは 0 になりがちで、そのままだと透明 PNG（ビューアで白）になる。
    不透明画像として出すパスではアルファを 255 に固定する。"""
    if arr.ndim == 3 and arr.shape[2] == 4:
        arr[:, :, 3] = 255.0
    return arr


def _capture_color(world, settings, cam, w, h, ts, spawned):
    # color_hide_matte: Matte 対象を非表示にして撮る（クリーンプレート）。背景が代わりに写る。
    hidden = None
    if settings.color_hide_matte:
        hidden = _resolve_target_actors(settings.matte_actors, settings.matte_actor_names)
        if hidden:
            _log("Color: Matte 対象 %d 個を非表示にして撮影（クリーンプレート）" % len(hidden))
        else:
            _warn("color_hide_matte 指定ですが Matte 対象が見つかりません。通常の Color を撮ります。")
    arr = _render_final_color(world, settings, cam, w, h, settings.aa_factor, spawned,
                              hidden_actors=hidden, fog_off=settings.fog_off)
    out = _out_path(settings, ts, "Color", ".png")
    _write_png_u8(out, _force_opaque(arr))
    _log("Color 出力: %s" % out)
    return out


def _capture_depth(world, settings, cam, w, h, ts, spawned):
    aa = settings.aa_factor
    # depth_hide_matte: マット対象を深度から除外（マットの位置は奥の深度になる）
    hidden = None
    if settings.depth_hide_matte:
        hidden = _resolve_target_actors(settings.matte_actors, settings.matte_actor_names)
        if hidden:
            _log("Depth: Matte 対象 %d 個を深度から除外" % len(hidden))
    # RGBA32F で撮る: 16F だと空/未ヒットと 655m 超が half 最大 65504cm に飽和し、
    # far>655m のとき空が「far より手前」扱いでグレーに残る（far=700m で空 6.4%
    # グレーの実測 2026-08-27）。32F が定数値（R32F 系バグ該当）なら 16F へ退避
    depth, _far = _render_depth_r_auto(world, cam, w * aa, h * aa, spawned,
                                       {"use32": True}, hidden_actors=hidden)
    depth = _downscale(depth, aa)  # cm 単位の距離（空/未ヒットは 1e8 オーダー）

    near, far = settings.depth_near, settings.depth_far

    def _normalized():
        """Near/Far で 0..1 に正規化。depth_invert なら手前=1(白)/奥=0(黒)。"""
        n = _np.clip((depth - near) / max(far - near, 1e-6), 0.0, 1.0)
        if settings.depth_invert:
            n = 1.0 - n
        return n

    bit = (settings.depth_bit or "16bit").lower()

    if bit.startswith("8"):
        out = _out_path(settings, ts, "Depth", ".png")
        _write_png_u8(out, _normalized() * 255.0)
        _log("Depth(8bit PNG%s) 出力: %s" % (" 反転" if settings.depth_invert else "", out))
        return out

    if bit.startswith("16"):
        out = _out_path(settings, ts, "Depth", ".png")
        _write_png_u16_gray(out, _normalized())
        _log("Depth(16bit PNG%s) 出力: %s" % (" 反転" if settings.depth_invert else "", out))
        return out

    # EXR（float リニア距離 cm をそのまま。データ用途のため反転/正規化はしない）
    out = _out_path(settings, ts, "Depth", ".exr")
    if _write_exr_gray(out, depth):
        _log("Depth(EXR float, 生cm) 出力: %s" % out)
        return out
    # EXR ライブラリが無い → 16bit PNG（Near/Far 正規化）にフォールバック
    out = _out_path(settings, ts, "Depth", ".png")
    _write_png_u16_gray(out, _normalized())
    _warn("EXR 書き出しライブラリが無いため 16bit PNG で出力しました: %s" % out)
    return out


def _resolve_target_actors(actors, names):
    """対象アクターを解決する。優先順: actors(オブジェクト) > names > エディタ選択。
    names は フルパス名(get_path_name) / 内部名(get_name) / ラベル(get_actor_label) の
    いずれでも一致する。UI はパス名で保持するので、ラベルをリネームしても、また別サブレベルに
    同じ内部名のアクターがあっても、対象を一意かつ確実に解決できる。"""
    if actors:
        return list(actors)
    if names:
        want = [n.strip() for n in names if n and str(n).strip()]
        if want:
            wantset = set(want)
            found = []
            matched = set()
            for a in _actor_subsystem().get_all_level_actors():
                try:
                    keys = {a.get_path_name(), a.get_name(), a.get_actor_label()}
                except Exception:
                    continue
                hit = keys & wantset
                if hit:
                    found.append(a)
                    matched |= hit
            missing = wantset - matched
            if missing:
                _warn("指定のアクターが見つかりません: %s" % ", ".join(sorted(missing)))
            return found
    return get_selected_actors()


def _capture_matte(world, settings, cam, w, h, ts, spawned):
    actors = _resolve_target_actors(settings.matte_actors, settings.matte_actor_names)
    if not actors:
        _warn("Matte 対象アクターがありません（選択 or 名前リストを指定）。Matte をスキップします。")
        return None

    aa = settings.aa_factor
    # 仕様準拠: 対象に CustomDepthStencil を一時付与（後で復元）
    saved = _set_custom_depth(actors, True, stencil=1)
    try:
        # 全シーン深度と対象のみ(show-only)深度の一致で、対象が「実際に最前面に
        # 見えている」画素だけマット化する（オクルージョン考慮）。
        # 600m 超の対象も拾えるよう RGBA32F（定数値なら 16F・600m しきい値へ退避）
        st = {"use32": True}
        full, _f = _render_depth_r_auto(world, cam, w * aa, h * aa, spawned, st,
                                        label="Matte")
        grp, far = _render_depth_r_auto(world, cam, w * aa, h * aa, spawned, st,
                                        label="Matte", show_only_actors=actors)
        sel = _visible_mask(grp, full, far=far).astype(_np.float32)
        if settings.matte_invert:
            sel = 1.0 - sel          # 選択=黒 / 周囲=白
        mask = _downscale(sel * 255.0, aa)  # 縁が AA される
        outs = []
        out = _out_path(settings, ts, "Matte", ".png")
        _write_png_u8(out, mask)
        _log("Matte(白黒) 出力: %s" % out)
        outs.append(out)
        # Fill（レンダリング画像）に matte をアルファとして組み込んだ RGBA も出力
        if settings.matte_fill_alpha:
            rgb = _render_fill_rgb(world, settings, cam, w, h, aa, spawned)
            rgba = _np.dstack([rgb, mask[:, :, None]]) if mask.ndim == 2 else _np.dstack([rgb, mask])
            out_fill = _out_path(settings, ts, "MatteBeauty", ".png")
            _write_png_u8(out_fill, rgba)
            _log("Matte(Fill+α) 出力: %s" % out_fill)
            outs.append(out_fill)
        return outs
    finally:
        _restore_custom_depth(saved)


def _objid_u8(r, g, b):
    """ObjectID の塗り色（0..1 の rgb → PNG に書かれる値と同じ rint 丸めの 0..255）。
    マニフェストのキーもこの値から作る（int 切り捨てだと画素と 1 ずれる）。"""
    return _np.clip(_np.rint(_np.array([r, g, b], dtype=_np.float64) * 255.0),
                    0, 255).astype(_np.float32)


def _objid_key(col):
    return "#%02X%02X%02X" % (int(col[0]), int(col[1]), int(col[2]))


def _capture_object_id(world, settings, cam, w, h, ts, spawned):
    """選択/指定アクターを色分けした Object ID 1枚 + 色→名前の対応 JSON を出力。
    オクルージョンは全シーン深度と各アクター深度の一致で正しく解決する。"""
    import colorsys, json
    actors = _resolve_target_actors(settings.objid_actors, settings.objid_actor_names)
    if not actors:
        _warn("Object ID 対象アクターがありません（選択 or 名前リストを指定）。スキップします。")
        return None

    # 全シーン深度（オクルージョン解決用）。600m 超の対象が「見えない」扱いで
    # マニフェストから落ちないよう RGBA32F（定数値なら 16F へ退避）
    st = {"use32": True}
    full, _f = _render_depth_r_auto(world, cam, w, h, spawned, st, label="Object ID")

    id_rgb = _np.zeros((h, w, 3), dtype=_np.float32)
    coverage = _np.zeros((h, w), dtype=bool)   # 対象全体の可視カバレッジ（アルファ用）
    manifest = {}
    idx = 0
    for act in actors:
        try:
            label = act.get_actor_label()
        except Exception:
            continue
        ad, far = _render_depth_r_auto(world, cam, w, h, spawned, st,
                                       label="Object ID", show_only_actors=[act])
        vis = _visible_mask(ad, full, far=far)  # そのアクターが最前面に見える画素
        if not bool(vis.any()):
            continue
        coverage |= vis
        # 黄金角でホールド相を回し、確実に色が離れるようにする（彩度/明度も少し振る）
        hue = (idx * 0.6180339887498949) % 1.0
        sat = 0.75 + 0.20 * ((idx // 6) % 2)
        val = 1.0 - 0.20 * ((idx // 3) % 2)
        idx += 1
        r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
        col = _objid_u8(r, g, b)
        id_rgb[vis] = col
        manifest[_objid_key(col)] = label

    outs = []
    out = _out_path(settings, ts, "ObjectID", ".png")
    _write_png_u8(out, id_rgb)
    try:
        with open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _warn("Object ID マニフェスト書き出しに失敗: %s" % e)
    _log("Object ID 出力: %s (%d obj)" % (out, len(manifest)))
    outs.append(out)

    # Fill + Object ID カバレッジ(アルファ) の RGBA
    if settings.objid_fill_alpha:
        rgb = _render_fill_rgb(world, settings, cam, w, h, 1, spawned)  # objid は等倍運用
        alpha = (coverage.astype(_np.float32) * 255.0)[:, :, None]
        rgba = _np.dstack([rgb, alpha])
        out_fa = _out_path(settings, ts, "ObjectIDBeauty", ".png")
        _write_png_u8(out_fa, rgba)
        _log("Object ID(Fill+α) 出力: %s" % out_fa)
        outs.append(out_fa)
    # Object ID 対象を非表示にして Fill をレンダリング（クリーンプレート）
    if settings.objid_hide_render:
        arr = _render_final_color(world, settings, cam, w, h, settings.aa_factor,
                                  spawned, hidden_actors=actors)
        out_hd = _out_path(settings, ts, "ObjectIDClean", ".png")
        _write_png_u8(out_hd, _force_opaque(arr))
        _log("Object ID 非表示レンダー 出力: %s" % out_hd)
        outs.append(out_hd)
    return outs


# HeterogeneousVolume（VDB/SVT の雲等）。CustomDepth/SceneDepth を書かないため
# 既存のマット/ObjectID 機構（ステンシル・深度一致）には一切写らない。
# マット系はアルファレンダ（capture_mrq の cloud_matte ジョブ）で扱う。
_HV_COMP_CLASS = getattr(unreal, "HeterogeneousVolumeComponent", None)


def _is_hv_comp(comp):
    return _HV_COMP_CLASS is not None and isinstance(comp, _HV_COMP_CLASS)


def is_volumetric_actor(actor):
    """HeterogeneousVolume(VDB) コンポーネントを持つアクターか。"""
    if _HV_COMP_CLASS is None or actor is None:
        return False
    try:
        return bool(actor.get_components_by_class(_HV_COMP_CLASS))
    except Exception:
        return False


def split_volumetric_targets(actors):
    """対象アクターを (通常プリミティブ, ボリューム(HV)) に分ける。"""
    prims, vols = [], []
    for a in actors or []:
        (vols if is_volumetric_actor(a) else prims).append(a)
    return prims, vols


def _matte_clip_plane(actors, cam):
    """マット対象群の代表平面（base, normal）を求める。normal は『奥』を向くよう調整。
    平面マットは actor の up ベクトルが面法線。複数なら可視点に最も近い1つを採用。"""
    cam_loc = cam["transform"].translation
    # カメラに最も近いマットを代表に（最前面の窓を定義する面）
    a = min(actors, key=lambda x: (x.get_actor_location() - cam_loc).length())
    base = a.get_actor_location()
    normal = a.get_actor_up_vector()
    if normal.length() < 1e-3:
        normal = a.get_actor_forward_vector()
    normal = normal.normal()
    # normal を『カメラから見て奥』方向へ向ける（手前側をクリップし奥を残すため）
    to_far = (base - cam_loc).normal()
    if normal.dot(to_far) < 0.0:
        normal = normal * -1.0
    return base, normal


def set_matte_shadow_occlusion(actors, enabled):
    """マット対象配下の PrimitiveComponent の影・オクルージョン寄与を一括設定。
    enabled=False で『他オブジェクトへの DropShadow と Occlusion(AO) を OFF』にする。
    永続設定（プロパティ変更）。対象数を返す。"""
    n = 0
    for a in actors or []:
        if a is None:
            continue
        for comp in a.get_components_by_class(unreal.PrimitiveComponent):
            if _is_hv_comp(comp):
                continue   # ボリューム(雲)は板ではない: ライティング分離しない
            for prop in ("cast_shadow", "affect_distance_field_lighting",
                         "affect_dynamic_indirect_lighting"):
                try:
                    comp.set_editor_property(prop, bool(enabled))
                except Exception as e:
                    _warn("Matte ライティング分離: %s.%s を設定できません: %s"
                          % (comp.get_name(), prop, e))
            n += 1
    _log("Matte 影/オクルージョン = %s（%d component）" % ("ON" if enabled else "OFF", n))
    return n


_TMP_MATTEBOARD_NAME = "M_UE5Cap_MatteBoardUnlit"
_MATTE_ORIGMAT_TAG = "ue5cap_origmat"


def get_or_create_matteboard_material():
    """マット板用のアンリット純黒マテリアル（ライティング・影を一切受けない）。
    黒＝放射ゼロなので Lumen スクリーントレース/反射経由で周囲のオブジェクトに
    色が被らない（旧 0.18 グレーはビューポートで茶色く見え色被りも起きた）。
    バッキング差分の黒側も 0 になり数学が単純化する。"""
    full = _TMP_MAT_PKG + "/" + _TMP_MATTEBOARD_NAME
    mat = None
    MEL = unreal.MaterialEditingLibrary
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        mat = unreal.EditorAssetLibrary.load_asset(full)
        # 既存で Emissive が繋がっていればそのまま返す。毎回作り直すと再コンパイルで
        # 固まり、uasset の上書き保存で SVN 作業コピーが常に「変更あり」になる
        if (mat is not None and _emissive_connected(mat)
                and mat.get_editor_property("shading_model")
                == unreal.MaterialShadingModel.MSM_UNLIT):
            return mat
    if mat is None:
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset(_TMP_MATTEBOARD_NAME, _TMP_MAT_PKG,
                              unreal.Material, unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("MatteBoard マテリアルの生成に失敗しました。")
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    try:
        MEL.delete_all_material_expressions(mat)
    except Exception as ex:
        _warn("MatteBoard マテリアルの式削除に失敗: %s" % ex)
    e = _mx_const(mat, 0.0, -300, 0)    # 純黒（放射ゼロ）
    _mx_prop(e, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as ex:
        _warn("MatteBoard マテリアルの保存に失敗（未保存のまま続行）: %s" % ex)
    return mat


_TMP_BACKINGWHITE_NAME = "M_UE5Cap_BackingWhite"
_TMP_BACKINGCLEAR_NAME = "M_UE5Cap_BackingClear"


def material_blend_mode(m):
    """MaterialInterface の実効ブレンドモード（インスタンスのオーバーライド考慮）。
    解決できなければ None。"""
    try:
        if isinstance(m, unreal.MaterialInstance):
            bpo = m.get_editor_property("base_property_overrides")
            if bpo.get_editor_property("override_blend_mode"):
                return bpo.get_editor_property("blend_mode")
            base = m.get_base_material()
            return (base.get_editor_property("blend_mode")
                    if base is not None else None)
        return m.get_editor_property("blend_mode")
    except Exception:
        return None


def material_writes_depth(m):
    """このマテリアルが不透明深度を書くか（Opaque/Masked）。
    半透明系（Translucent/Additive/Modulate 等）は深度を書かない＝
    バッキング材(不透明)に差し替えると Beauty に存在しない遮蔽面が出現し、
    その奥の HV(VDB雲) が深度テストで丸ごと消える（UDS の雲内フォグ板が
    雲高度に不可視の巨大水平面を張っていた実測 2026-08-26）。判定不能は
    True（従来どおり差し替え）。"""
    bm = material_blend_mode(m)
    if bm is None:
        return True
    return bm in (unreal.BlendMode.BLEND_OPAQUE, unreal.BlendMode.BLEND_MASKED)


def get_or_create_backing_clear_material():
    """バッキングレンダ用の完全不可視マテリアル（Translucent・Unlit・opacity 0）。
    半透明スロットの差し替え先。深度を書かず何も描かない＝素の半透明が
    見た目を汚すのも防ぎつつ、幻の遮蔽面を作らない。"""
    full = _TMP_MAT_PKG + "/" + _TMP_BACKINGCLEAR_NAME
    mat = None
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        mat = unreal.EditorAssetLibrary.load_asset(full)
    if mat is None:
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset(_TMP_BACKINGCLEAR_NAME, _TMP_MAT_PKG,
                              unreal.Material, unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("BackingClear マテリアルの生成に失敗しました。")
    MEL = unreal.MaterialEditingLibrary
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    mat.set_editor_property("two_sided", True)
    try:
        MEL.delete_all_material_expressions(mat)
    except Exception as ex:
        _warn("BackingClear マテリアルの式削除に失敗: %s" % ex)
    z = _mx_const(mat, 0.0, -300, 0)
    _mx_prop(z, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    z2 = _mx_const(mat, 0.0, -300, 150)
    _mx_prop(z2, unreal.MaterialProperty.MP_OPACITY)
    MEL.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as ex:
        _warn("BackingClear マテリアルの保存に失敗（未保存のまま続行）: %s" % ex)
    return mat


def get_or_create_backing_white_material():
    """バッキングレンダ用のアンリット白(発光100)マテリアル。
    板をこの材で1回レンダした線形色 W = 手前の内容の発光C + 透過率T×100 で、
    C は通常のシーン輝度（〜1程度）なので C/100 ≤ 1% となり無視できる
    → T ≈ W/素板レベル。黒板との2レンダ差分は不要（レンダ1本で済む）。
    発光100はブルーム/スクリーントレースを撒くため、バッキングジョブは
    それらをジョブ内 cvar で切る（capture_mrq 側）。"""
    full = _TMP_MAT_PKG + "/" + _TMP_BACKINGWHITE_NAME
    mat = None
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        mat = unreal.EditorAssetLibrary.load_asset(full)
    # 白側は板と違い常設表示されない（バッキングレンダ中のみ差し替え）ため発光1.0のまま
    if mat is None:
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset(_TMP_BACKINGWHITE_NAME, _TMP_MAT_PKG,
                              unreal.Material, unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("BackingWhite マテリアルの生成に失敗しました。")
    MEL = unreal.MaterialEditingLibrary
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("two_sided", True)
    try:
        MEL.delete_all_material_expressions(mat)
    except Exception as ex:
        _warn("BackingWhite マテリアルの式削除に失敗: %s" % ex)
    e = _mx_const(mat, 100.0, -300, 0)
    _mx_prop(e, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as ex:
        _warn("BackingWhite マテリアルの保存に失敗（未保存のまま続行）: %s" % ex)
    return mat


def level_has_volumetrics():
    """レベル内に HV(VDB雲) アクターがあるか（バッキング差分の自動トリガー用）。"""
    return bool(level_volumetrics())


def level_volumetrics():
    """レベル内の HV(VDB雲) アクターを列挙する（雲マット独立出力の既定対象）。"""
    if _HV_COMP_CLASS is None:
        return []
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    out = []
    for a in sub.get_all_level_actors():
        try:
            if a.get_components_by_class(_HV_COMP_CLASS):
                out.append(a)
        except Exception:
            pass
    return out


def set_matte_unlit(actors):
    """マット対象の全メッシュスロットをアンリット材へ差替える（受光・受影なし）。
    元マテリアルのパスはアクタータグ ue5cap_origmat|comp|slot|path に退避
    （restore_matte_materials で復元）。差替えたスロット数を返す。"""
    mat = get_or_create_matteboard_material()
    mat_path = mat.get_path_name()
    n = 0
    for a in actors or []:
        if a is None:
            continue
        tags = [unreal.Name(str(t)) for t in a.tags]
        for comp in a.get_components_by_class(unreal.MeshComponent):
            if _is_hv_comp(comp):
                # ボリューム(雲)は板ではない。Volume ドメイン材が必要で差替え不可、
                # かつ現在材は BP が作る Transient MID で退避パスも復元不能になる。
                continue
            cname = comp.get_name()
            for i in range(comp.get_num_materials()):
                cur = comp.get_material(i)
                cur_path = cur.get_path_name() if cur else ""
                if cur_path == mat_path:
                    continue               # 既に差替え済み
                if cur_path.startswith("/Engine/Transient"):
                    # BP の construction script が作る MID 等。パスを退避しても
                    # 復元時に解決できずスロットが既定材（WorldGrid）になるので触らない
                    _warn("マット板アンリット化: %s の %s slot %d は一時材 (%s) のためスキップ"
                          % (a.get_actor_label(), cname, i, cur_path))
                    continue
                tags.append(unreal.Name("%s|%s|%d|%s"
                                        % (_MATTE_ORIGMAT_TAG, cname, i, cur_path)))
                comp.set_material(i, mat)
                n += 1
        a.set_editor_property("tags", tags)
    _log("マット板をアンリット化: %d slot" % n)
    return n


def restore_matte_materials(actors):
    """set_matte_unlit の退避タグから元マテリアルを復元し、タグを除去する。"""
    n = 0
    for a in actors or []:
        if a is None:
            continue
        keep = []
        comps = {c.get_name(): c
                 for c in a.get_components_by_class(unreal.MeshComponent)}
        for t in a.tags:
            s = str(t)
            if not s.startswith(_MATTE_ORIGMAT_TAG + "|"):
                keep.append(unreal.Name(s))
                continue
            try:
                _p, cname, idx, path = s.split("|", 3)
                comp = comps.get(cname)
                if comp is not None:
                    m = unreal.load_object(None, path) if path else None
                    if path and m is None:
                        # 元材が解決できない（リネーム/削除/一時材）。None を入れると
                        # WorldGrid になり手掛かりも消えるので、タグを残して触らない
                        keep.append(unreal.Name(s))
                        _warn("マテリアル復元: 元材を読み込めないためタグを残します (%s)" % s)
                        continue
                    comp.set_material(int(idx), m)
                    n += 1
            except Exception as e:
                keep.append(unreal.Name(s))
                _warn("マテリアル復元に失敗・タグを残します (%s): %s" % (s, e))
        a.set_editor_property("tags", keep)
    _log("マット板のマテリアル復元: %d slot" % n)
    return n


def composite_behind_in_matte(world, cam, matte_actors, beauty_path, behind_path,
                              out_path, width, height):
    """マットのシルエット範囲だけ behind(near-clip Beauty)、それ以外は通常 Beauty を合成。
    シルエットはマットの show-only 深度（マットが可視である必要あり）。出力パスを返す。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    if not (os.path.isfile(beauty_path) and os.path.isfile(behind_path)):
        _warn("composite_behind: 入力画像が不足 beauty=%s behind=%s" % (beauty_path, behind_path))
        return None
    tmp = []
    try:
        # 600m 超の板面がシルエットから抜けないよう RGBA32F（定数値なら 16F へ退避）
        md, far = _render_depth_r_auto(world, cam, width, height, tmp, {"use32": True},
                                       label="behind composite",
                                       show_only_actors=matte_actors)
    finally:
        _destroy_actors(tmp)
    mask = (md < far).astype(_np.float32)
    cov = 100.0 * float(mask.mean())
    _log("behind composite: マットシルエット被覆 %.1f%%" % cov)
    beauty = _np.asarray(_PILImage.open(beauty_path).convert("RGB")).astype(_np.float32)
    behind = _np.asarray(_PILImage.open(behind_path).convert("RGB")).astype(_np.float32)
    H, W = beauty.shape[0], beauty.shape[1]
    if mask.shape != (H, W):
        mask = _np.asarray(_PILImage.fromarray((mask * 255).astype(_np.uint8)).resize((W, H)),
                           dtype=_np.float32) / 255.0
    m3 = mask[:, :, None]
    comp = behind * m3 + beauty * (1.0 - m3)
    _write_png_u8(out_path, _np.clip(comp, 0, 255))
    _log("behind composite 出力: %s" % out_path)
    return out_path


def find_ffmpeg(explicit=None):
    """ffmpeg 実行ファイルを探す。優先: 明示パス → imageio_ffmpeg 同梱（実行中の
    Python に入っているもの）→ 個人 Python の imageio_ffmpeg（glob）→ PATH。"""
    import glob as _glob
    import shutil as _shutil
    if explicit and os.path.isfile(explicit):
        return explicit
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception as e:
        _warn_once("imageio_ffmpeg", "imageio_ffmpeg から ffmpeg を取得できません: %s" % e)
    # 最後の手段: ユーザー個人の Python レイアウト依存（配布先ではまず当たらない）
    pat = os.path.expanduser(
        r"~\AppData\Local\Programs\Python\*\Lib\site-packages"
        r"\imageio_ffmpeg\binaries\ffmpeg-*.exe")
    hits = sorted(_glob.glob(pat))
    if hits:
        return hits[-1]
    return _shutil.which("ffmpeg")


def encode_mp4_cmd(ffmpeg, output_dir, name_body, pass_name, take_str,
                   fps_num, fps_den, crf, start_number):
    """PNG 連番 → H.264 MP4 の ffmpeg コマンド（リスト）と出力パスを返す。
    フレームレートはシーケンスの Display Rate をそのまま使う（fps ずれ防止）。"""
    fps = "%d/%d" % (int(fps_num), max(int(fps_den), 1))
    src = os.path.join(output_dir, "%s_%s_%s.%%04d.png" % (name_body, pass_name, take_str))
    dst = os.path.join(output_dir, "%s_%s_%s.mp4" % (name_body, pass_name, take_str))
    # pad: libx264 の yuv420p は幅・高さが偶数必須（例 round(1080*2.39)=2581 で失敗する）
    return [ffmpeg, "-nostdin", "-y", "-framerate", fps,
            "-start_number", str(int(start_number)),
            "-i", src, "-c:v", "libx264", "-crf", str(int(crf)),
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-pix_fmt", "yuv420p", "-r", fps, dst], dst


# 連番の拡張子（Depth 連番は EXR もあり得る）。合成・雲抜き系は PNG 専用のまま
_FRAME_EXT_RE = r"\.(png|exr|jpg|jpeg)$"


def trim_sequence_frames(output_dir, name_body, take_str, pass_names, start, end_excl):
    """[start, end_excl) 範囲外のフレーム（PNG/EXR/JPG）を削除する。
    MRQ はテンポラルサンプル使用時に終端へ1フレーム余分に書くことがあるため、
    合成・エンコード前に必ず範囲を揃える。削除数を返す。"""
    n = 0
    for pn in pass_names:
        pat = re.compile(re.escape("%s_%s_%s." % (name_body, pn, take_str))
                         + r"(\d+)" + _FRAME_EXT_RE)
        for f in os.listdir(output_dir):
            m = pat.match(f)
            if m and not (int(start) <= int(m.group(1)) < int(end_excl)):
                try:
                    os.remove(os.path.join(output_dir, f))
                    n += 1
                except Exception as e:
                    _warn("範囲外フレームの削除に失敗 %s: %s" % (f, e))
    if n:
        _log("範囲外フレームを削除: %d" % n)
    return n


def move_pass_frames_to_subdirs(output_dir, name_body, take_str, pass_names):
    """PNG連番の各パスを <素材名>_<テイク> サブフォルダへ移動する
    （連番の大量ファイルで出力フォルダが埋まるのを防ぐ。MP4 は直下のまま）。
    合成・エンコード等の後処理がすべて終わった最後に呼ぶこと。移動数を返す。"""
    n = 0
    for pn in pass_names:
        pat = re.compile(re.escape("%s_%s_%s." % (name_body, pn, take_str))
                         + r"\d+" + _FRAME_EXT_RE)
        files = [f for f in os.listdir(output_dir) if pat.match(f)]
        if not files:
            continue
        sub = os.path.join(output_dir, "%s_%s" % (pn, take_str))
        os.makedirs(sub, exist_ok=True)
        for f in files:
            try:
                os.replace(os.path.join(output_dir, f), os.path.join(sub, f))
                n += 1
            except Exception as e:
                _warn("連番の移動に失敗 %s: %s" % (f, e))
    if n:
        _log("PNG連番をサブフォルダへ移動: %d ファイル" % n)
    return n


def delete_pass_frames(output_dir, name_body, take_str, pass_names):
    """指定パスのフレーム PNG を削除（中間素材や PNG 不要指定の後始末）。"""
    n = 0
    for pn in pass_names:
        pat = re.compile(re.escape("%s_%s_%s." % (name_body, pn, take_str))
                         + r"\d+" + _FRAME_EXT_RE)
        for f in os.listdir(output_dir):
            if pat.match(f):
                try:
                    os.remove(os.path.join(output_dir, f))
                    n += 1
                except Exception as e:
                    _warn("連番の削除に失敗 %s: %s" % (f, e))
    return n


def composite_mattefront_sequence(output_dir, name_body, take_str, use_cloud=False,
                                  use_backing=False):
    """Beauty（クリーン）に Matte をアルファとして焼いた MatteBeauty 連番を書く
    （Matteの前＝マット部分が穴。選択=黒がアルファ0）。RGB はアルファ乗算済みなので
    MP4 化しても穴が黒になる。use_cloud=True なら CloudMatte 連番（PNG の α=
    雲の可視不透明度）も mask×(1-α) で統合する（板マスクが無い雲のみの場合は
    白地から作る）。use_backing=True なら BackingT 連番（16bit グレー = 板より
    手前の透過率）を mask' = 255−(255−mask)×T で統合する（板の手前の雲・半透明が
    遮蔽関係どおり穴を塞ぐ）。出力フレーム数を返す。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return 0
    pat = re.compile(re.escape("%s_Beauty_%s." % (name_body, take_str)) + r"(\d+)\.png$")
    n = 0
    for f in sorted(os.listdir(output_dir)):
        m = pat.match(f)
        if not m:
            continue
        fr = m.group(1)
        matte_p = os.path.join(output_dir, "%s_Matte_%s.%s.png" % (name_body, take_str, fr))
        cloud_p = os.path.join(output_dir, "%s_CloudMatte_%s.%s.png" % (name_body, take_str, fr))
        beauty = _np.asarray(_PILImage.open(os.path.join(output_dir, f)).convert("RGB"),
                             dtype=_np.float32)
        matte = None
        if os.path.isfile(matte_p):
            matte = _np.asarray(_PILImage.open(matte_p).convert("L"), dtype=_np.float32)
        if use_cloud and os.path.isfile(cloud_p):
            cim = _PILImage.open(cloud_p)
            if cim.mode == "RGBA":
                ca = _np.asarray(cim)[:, :, 3].astype(_np.float32) / 255.0
                if matte is None:
                    matte = _np.full(ca.shape, 255.0, dtype=_np.float32)
                matte = matte * (1.0 - ca)
        if use_backing and matte is not None:
            bt_p = os.path.join(output_dir,
                                "%s_BackingT_%s.%s.png" % (name_body, take_str, fr))
            if os.path.isfile(bt_p):
                tim = _PILImage.open(bt_p)
                t = _np.asarray(tim).astype(_np.float32) / (
                    65535.0 if tim.mode in ("I;16", "I") else 255.0)
                if t.shape == matte.shape:
                    matte = 255.0 - (255.0 - matte) * t
        if matte is None:
            _warn("MatteBeauty 合成: フレーム %s の Matte/CloudMatte が無い" % fr)
            continue
        rgb = beauty * (matte / 255.0)[:, :, None]
        rgba = _np.dstack([rgb, matte])
        out = os.path.join(output_dir, "%s_MatteBeauty_%s.%s.png" % (name_body, take_str, fr))
        _write_png_u8(out, rgba)
        n += 1
    _log("MatteBeauty 合成: %d フレーム出力" % n)
    return n


def objid_stencil_color(stencil):
    """ステンシル値の ObjectID 色 (r,g,b 0-255)。ObjectID マテリアルの
    コサインパレットと同じ式を sRGB エンコードした値（PNG/MP4 は表示用
    エンコードで書かれるため、マニフェストもエンコード後の色で合わせる）。"""
    h = (stencil * 0.6180339887498949) % 1.0
    def _c(x):
        lin = 0.5 + 0.5 * math.cos(2.0 * math.pi * x)
        srgb = lin * 12.92 if lin <= 0.0031308 else 1.055 * (lin ** (1.0 / 2.4)) - 0.055
        return int(round(255.0 * max(0.0, min(1.0, srgb))))
    return (_c(h), _c(h - 1.0 / 3.0), _c(h - 2.0 / 3.0))


def composite_behind_sequence(output_dir, name_body, take_str,
                              plate_label="BehindPlate", out_label="Behind"):
    """シーケンスレンダの behind 合成。各フレームで MatteSil（マット対象の全投影
    シルエット・選択=黒・遮蔽非依存）をマスクに、黒い部分は BehindPlate、白い部分は
    Beauty を合成した Behind 連番を書く。可視性マスク（Matte）は使わない：マットより
    手前のオブジェクト（キャラ等）の画素が白になり Beauty 側で写り込むため。
    出力したフレーム数を返す。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return 0
    pat = re.compile(re.escape("%s_Beauty_%s." % (name_body, take_str)) + r"(\d+)\.png$")
    n = 0
    n_occ = 0
    for f in sorted(os.listdir(output_dir)):
        m = pat.match(f)
        if not m:
            continue
        fr = m.group(1)
        plate_p = os.path.join(output_dir, "%s_%s_%s.%s.png" % (name_body, plate_label, take_str, fr))
        matte_p = os.path.join(output_dir, "%s_MatteSil_%s.%s.png" % (name_body, take_str, fr))
        if not (os.path.isfile(plate_p) and os.path.isfile(matte_p)):
            _warn("behind 合成: フレーム %s の素材が不足（plate/mattesil）" % fr)
            continue
        beauty = _np.asarray(_PILImage.open(os.path.join(output_dir, f)).convert("RGB"),
                             dtype=_np.float32)
        plate = _np.asarray(_PILImage.open(plate_p).convert("RGB"), dtype=_np.float32)
        matte = _np.asarray(_PILImage.open(matte_p).convert("L"), dtype=_np.float32) / 255.0
        w_plate = (1.0 - matte)[:, :, None]     # Matte は 選択=黒（黒い所ほどプレート）
        comp = plate * w_plate + beauty * (1.0 - w_plate)
        # BehindOcc（render_matte_sil_sequence の occlusion）: マット面より手前の
        # ジオメトリが写っている画素は「板の奥」ではないので黒にする。
        # near clip はマットの最近点1枚なので、傾いた板ではここでしか消せない。
        occ_p = os.path.join(output_dir, "%s_BehindOcc_%s.%s.png" % (name_body, take_str, fr))
        if os.path.isfile(occ_p):
            occ = _np.asarray(_PILImage.open(occ_p).convert("L"), dtype=_np.float32) / 255.0
            comp = comp * (1.0 - occ * (1.0 - matte))[:, :, None]
            n_occ += 1
        out = os.path.join(output_dir, "%s_%s_%s.%s.png" % (name_body, out_label, take_str, fr))
        _write_png_u8(out, _np.clip(comp, 0, 255))
        n += 1
    _log("behind 合成(%s): %d フレーム出力（手前遮蔽の黒塗り %d フレーム）"
         % (out_label, n, n_occ))
    return n


def matte_near_clip_cm(actors, cam):
    """マット対象が実際に画面に写っている画素のうち、視線方向いちばん手前の距離(cm)。
    低解像度の show-only 深度を1枚撮って実測する。
    バウンズ8隅の射影だと視錐台の外まで数えるので、寝た板では桁が変わる（実測
    2026-08-30 matPlane6: 隅の最大 1001m に対し画面内の実測最近は 361m。1001m で
    クリップすると板の裏に見えるべき背景を 640m 分削り、プレートに穴が開く）。
    r.SetNearClipPlane は視線に垂直な平面しか作れないため、傾いた板ではどのスカラー
    を選んでも誤差は残る。ここでは『正しい背景を消さない』側＝最近距離を採る
    （手前の遮蔽物が少し残るほうが、板の裏に穴が開くより軽い）。
    ⚠️ 厳密には斜めクリップ面が要る。SceneCapture2D は clip_plane_base/normal で
    対応済みだが MRQ には手段が無い。cam は fov を含む get_camera_settings() の戻り値。"""
    if not _HAS_NUMPY:
        raise RuntimeError("numpy がありません（near-clip 実測に必要）")
    world = _get_editor_world()
    tmp = []
    try:
        # 32F が定数値（UE5.7 の深度RTバグ）なら 16F へ退避（655m 超は測れない）
        d, far = _render_depth_r_auto(world, cam, 480, 270, tmp, {"use32": True},
                                      label="near-clip 実測", show_only_actors=actors)
    finally:
        _destroy_actors(tmp)
    v = d[(d > 1.0) & (d < far)] if d is not None else None
    if v is None or v.size < 16:
        raise RuntimeError("マット対象が画面内に見当たりません（near-clip 測定不能）")
    return max(1.0, float(v.min()))


def render_matte_sil_sequence(output_dir, name_body, take_str, start, end_excl,
                              actors, width, height, camera_for_frame, aa=2,
                              occlusion=True, near_clip_cm=None, hidden_front=None):
    """映像 Behind 用: 各フレームをシーケンサーでスクラブ評価し、マット対象の
    全投影シルエット（選択=黒/周囲=白）を show-only 深度から MatteSil 連番として
    書く（静止画側の composite_behind_in_matte と同一手法）。
    メインジョブの MatteSil PP パスは使わない: ObjectID 対象のステンシルが
    「板より手前」の画素でシルエットを上書きして穴を開け、合成が Beauty 側
    （手前オブジェクト入り）を選んでしまう（実測 2026-07-24）。
    occlusion=True なら同じフレームで「マットを隠した全シーン深度」も撮り、
    マット面より手前にある画素（白=手前）を BehindOcc 連番として書く。
    プレートは r.SetNearClipPlane（視線に垂直な1枚）でしか手前を消せないので、
    傾いた板では『最近点より遠く・板より手前』の物がプレートに残る（実測
    2026-09-29 AV024 take035: 板 447〜921m・near clip 447m で画面の 3%、左半分に
    集中）。composite_behind_sequence がこのマスクで該当画素を黒にする。
    深度を書かない HV(VDB雲)/半透明は判定できない（従来どおりアクター単位で隠す）。
    hidden_front はプレートで隠したアクター（actors_in_front_of_matte）。プレートに
    写っていないので遮蔽物からも外す（外さないと隠した物の形に黒が残る）。
    camera_for_frame(frame) はスクラブ込みでそのフレームのカメラアクターを返す。
    end_excl は排他的終端（trim_sequence_frames と同じ [start, end_excl)）。
    書いたフレーム数を返す。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return 0
    world = _get_editor_world()
    try:
        t0 = unreal.LevelSequenceEditorBlueprintLibrary.get_current_time()
    except Exception as e:
        _warn("MatteSil 生成: プレイヘッド位置を取得できません（復元しません）: %s" % e)
        t0 = None
    n = 0
    # 一度 32F の定数値バグを検出したら以降のフレームも 16F で撮る
    # （板が画面外＝全画素が空のフレームはバグ扱いしない: _depth_rt_broken）
    st = {"use32": True}
    restore_nc = default_near_clip_cm()
    try:
        for i_fr, fr in enumerate(range(int(start), int(end_excl))):
            cam_actor = camera_for_frame(fr)
            if cam_actor is None:
                _warn("MatteSil 生成: フレーム %d のカメラを特定できずスキップ" % fr)
                continue
            cam = get_camera_settings(cam_actor)
            tmp = []
            try:
                # RGBA32F で撮る。既定の RGBA16F は 65504cm(655m) で飽和するため、
                # _DEPTH_FAR_CM(600m) より遠いマット面が「未ヒット＝空」と同じ値に
                # なり、シルエットから白く抜け落ちる。抜けた側は合成で Beauty が
                # 選ばれるので、映像に「マットの手前」が出る（実測 2026-08-30
                # AV003A take015: 板が視線方向 545〜1156m に跨り、600m の等距離線
                # x≒1008 を境に Behind が左右で前後に割れた）。
                # 32F が全画素一定（UE5.7 の深度RTバグ該当）なら 16F。この場合
                # 655m 超のマット面は救えない
                d, far = _render_depth_r_auto(world, cam, width * aa, height * aa, tmp, st,
                                              label="MatteSil", show_only_actors=actors)
                fmt = _depth_fmt(st)
                sd = None
                if occlusion:
                    # プレートと同じ near clip（r.SetNearClipPlane）を立てて撮る。
                    # これより手前はプレートでも消えて奥が露出しているので、遮蔽物
                    # として数えてはいけない（数えると露出した奥まで黒くなる。実測
                    # 2026-09-29 AV024 take036）。SceneCapture2D の clip_plane は
                    # プロジェクトの Global Clip Plane が無効だと効かない（同日実測）
                    # ため、MRQ と同じ cvar を使う。SceneCapture も GNearClippingPlane
                    # に従う。
                    if near_clip_cm:
                        unreal.SystemLibrary.execute_console_command(
                            world, "r.SetNearClipPlane %f" % float(near_clip_cm))
                    try:
                        sd = _render_depth_r(world, cam, width * aa, height * aa, tmp,
                                             hidden_actors=list(actors) + list(hidden_front or []),
                                             fmt=fmt)
                    finally:
                        if near_clip_cm:
                            unreal.SystemLibrary.execute_console_command(
                                world, "r.SetNearClipPlane %f" % restore_nc)
            finally:
                _destroy_actors(tmp)
            if i_fr % 8 == 7:
                unreal.SystemLibrary.collect_garbage()
            sil = _downscale((d < far).astype(_np.float32) * 255.0, aa)
            out = os.path.join(output_dir,
                               "%s_MatteSil_%s.%04d.png" % (name_body, take_str, fr))
            _write_png_u8(out, 255.0 - sil)     # 選択=黒 / 周囲=白
            if sd is not None:
                # マット面より手前（1% + 20cm より手前）にジオメトリがある画素。
                # 空/未ヒットは「奥」扱い。near clip より手前はクリップ面が効かなかった
                # 場合の保険として除外（プレート側では消えている）。
                occ = (d < far) & (sd < far) & (sd < d * 0.99 - 20.0)
                if near_clip_cm:
                    occ &= sd > float(near_clip_cm)
                occ_img = _downscale(occ.astype(_np.float32) * 255.0, aa)
                _write_png_u8(os.path.join(
                    output_dir, "%s_BehindOcc_%s.%04d.png" % (name_body, take_str, fr)),
                    occ_img)                     # 手前=白
            n += 1
    finally:
        _restore_playhead(t0, "MatteSil 生成")
    _log("MatteSil 生成（スクラブ+show-only深度）: %d フレーム" % n)
    return n


def _restore_playhead(t0, label):
    if t0 is None:
        return
    try:
        unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(int(t0))
    except Exception as e:
        _warn("%s: プレイヘッドを復元できません（フレーム %s）: %s" % (label, t0, e))


def render_depth_sequence(output_dir, name_body, take_str, start, end_excl,
                          width, height, camera_for_frame, near_cm, far_cm, mode,
                          aa=1):
    """映像タブの厳密リニア Depth: 各フレームをシーケンサーでスクラブ評価し、
    SceneCapture の全シーン SceneDepth を width*aa × height*aa で撮って書く
    （MRQ の PP パスは表示用 sRGB エンコードの 8bit なので、16bit/EXR はこちら）。
    mode="16bit": 手前=白の正規化 v = clip((far−d)/(far−near), 0, 1)。空/未ヒット
                  （しきい値以上）は 0 固定。%s_Depth_%s.%04d.png（16bit グレー）。
    mode="exr"  : 生 cm の float32（空は生の巨大値のまま＝静止画 EXR と同じ）。
                  %s_Depth_%s.%04d.exr。1 フレーム目で EXR を書けなければ全編
                  16bit PNG へ切り替える（警告付き）。
    RGBA32F で撮り、定数値バグなら 16F へ退避（_render_depth_r_auto）。
    camera_for_frame(frame) はスクラブ込みでそのフレームのカメラアクターを返す。
    end_excl は排他的終端。戻り値 (書いたフレーム数, 拡張子 "png"|"exr")。
    参考: 同じフレーム毎 RGBA32F キャプチャは 1920x1080 aa=1 で約 1.1 秒/フレーム。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return 0, "png"
    mode = "exr" if str(mode).lower().startswith("exr") else "16bit"
    near, far = float(near_cm), float(far_cm)
    world = _get_editor_world()
    try:
        t0 = unreal.LevelSequenceEditorBlueprintLibrary.get_current_time()
    except Exception as e:
        _warn("Depth 連番: プレイヘッド位置を取得できません（復元しません）: %s" % e)
        t0 = None
    st = {"use32": True}
    aa = max(int(aa), 1)
    frames = list(range(int(start), int(end_excl)))
    n = 0
    try:
        for i_fr, fr in enumerate(frames):
            cam_actor = camera_for_frame(fr)
            if cam_actor is None:
                _warn("Depth 連番: フレーム %d のカメラを特定できずスキップ" % fr)
                continue
            cam = get_camera_settings(cam_actor)
            tmp = []
            try:
                d, far_th = _render_depth_r_auto(world, cam, int(width) * aa,
                                                 int(height) * aa, tmp, st,
                                                 label="Depth 連番")
            finally:
                _destroy_actors(tmp)
            if i_fr % 8 == 7:
                unreal.SystemLibrary.collect_garbage()
            d = _downscale(d, aa)
            if mode == "exr":
                out = os.path.join(output_dir,
                                   "%s_Depth_%s.%04d.exr" % (name_body, take_str, fr))
                if _write_exr_gray(out, d):
                    n += 1
                elif n == 0:
                    _warn("Depth 連番: EXR を書けないため全編 16bit PNG で出力します")
                    mode = "16bit"
                else:
                    _warn("Depth 連番: フレーム %d の EXR 書き出しに失敗" % fr)
                    continue
            if mode == "16bit":
                v = _np.clip((far - d) / max(far - near, 1e-6), 0.0, 1.0)
                v[d >= far_th] = 0.0
                out = os.path.join(output_dir,
                                   "%s_Depth_%s.%04d.png" % (name_body, take_str, fr))
                _write_png_u16_gray(out, v)
                n += 1
            if (i_fr + 1) % 10 == 0:
                _log("Depth 連番: %d / %d フレーム" % (i_fr + 1, len(frames)))
    finally:
        _restore_playhead(t0, "Depth 連番")
    ext = "exr" if mode == "exr" else "png"
    _log("Depth 連番（スクラブ+SceneCapture %s）: %d フレーム"
         % ("EXR 生cm" if ext == "exr" else "16bit PNG 手前=白", n))
    return n, ext


def volumetrics_nearer_than(dist_cm, cam):
    """カメラ視線方向の深度が dist_cm より手前にある HV(VDB雲) アクターを返す。
    HV は r.SetNearClipPlane の描画クリップを無視して写り込む（実測 2026-07-24）
    ため、Matteの奥のプレートでは手前の雲をアクター単位で隠す必要がある。"""
    if _HV_COMP_CLASS is None:
        return []
    cam_loc = cam["transform"].translation
    fwd = cam["transform"].rotation.rotator().get_forward_vector()
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    out = []
    for a in sub.get_all_level_actors():
        try:
            if not a.get_components_by_class(_HV_COMP_CLASS):
                continue
        except Exception:
            continue
        # 中心でなくバウンズの最近点で判定する: クリップ面を跨ぐ大きな雲
        # （中心は奥・手前に張り出し）がゴーストとして残るのを防ぐ
        try:
            origin, ext = a.get_actor_bounds(False)
        except Exception:
            origin, ext = a.get_actor_location(), unreal.Vector(0, 0, 0)
        d = ((origin.x - cam_loc.x) * fwd.x + (origin.y - cam_loc.y) * fwd.y
             + (origin.z - cam_loc.z) * fwd.z)
        d_near = d - (abs(ext.x * fwd.x) + abs(ext.y * fwd.y) + abs(ext.z * fwd.z))
        if d_near < float(dist_cm):
            out.append(a)
    return out


def _box_in_front_of_planes(origin, ext, planes, cam_locs, straddle_ratio=0.0):
    """AABB(origin±ext) が、全ての平面 (base, normal) について、全てのカメラ位置と
    同じ側にあるか。タプル (x, y, z) の純計算（unreal 非依存）。
    straddle_ratio>0 なら板を跨ぐ箱も、奥側へはみ出す奥行きが箱の全奥行きの
    その割合以下（＝大半がカメラ側）なら手前扱いにする。板を跨ぐ細い柱を
    BehindOcc の黒塗りに回すと黒いシルエットが残るため（AV024 mat_Plane 2026-10-07）。"""
    corners = [(origin[0] + sx * ext[0], origin[1] + sy * ext[1], origin[2] + sz * ext[2])
               for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]

    def _side(p, base, n):
        return (p[0] - base[0]) * n[0] + (p[1] - base[1]) * n[1] + (p[2] - base[2]) * n[2]

    for base, n in planes:
        for c in cam_locs:
            s = _side(c, base, n)
            if s == 0.0:
                return False
            sign = 1.0 if s > 0.0 else -1.0
            d = [_side(p, base, n) * sign for p in corners]   # 正=カメラ側
            dmin, dmax = min(d), max(d)
            if dmin > 0.0:
                continue
            if dmax <= 0.0 or straddle_ratio <= 0.0:
                return False
            if -dmin > straddle_ratio * (dmax - dmin):
                return False
    return True


def _selftest_planes():
    pl, cl = [((0, 0, 0), (0, 0, 1))], [(0, 0, 9)]
    assert _box_in_front_of_planes((0, 0, 5), (1, 1, 1), pl, cl)
    assert not _box_in_front_of_planes((0, 0, .5), (1, 1, 1), pl, cl)
    assert not _box_in_front_of_planes((0, 0, -5), (1, 1, 1), pl, cl)
    # 跨ぎ: 奥へ 0.5 / 全奥行き 2 = 25% → ratio 0.5 なら手前扱い、ratio 0.2 なら跨ぎのまま
    assert _box_in_front_of_planes((0, 0, .5), (1, 1, 1), pl, cl, 0.5)
    assert not _box_in_front_of_planes((0, 0, .5), (1, 1, 1), pl, cl, 0.2)
    assert not _box_in_front_of_planes((0, 0, -.5), (1, 1, 1), pl, cl, 0.5)


try:
    _selftest_planes()
except AssertionError:
    # import を壊さない（パネル全体が開けなくなるため）。判定が壊れていることだけ知らせる
    _warn("_box_in_front_of_planes のセルフテストに失敗（Matteの奥の手前判定が不正の可能性）")


def actors_in_front_of_matte(matte_actors, cams, straddle_ratio=0.5):
    """バウンズ全体がマット板よりカメラ側にあるメッシュアクターを返す。
    板を跨ぐアクターも、奥側へのはみ出しが全奥行きの straddle_ratio 以下（大半が
    手前）なら含める（黒塗りで残る黒い柱対策。0 で従来どおり完全手前のみ）。
    Matteの奥のプレートではこれらをアクター単位で隠す: r.SetNearClipPlane は視線に
    垂直な1枚なので、傾いた板では『near clip より遠く・板より手前』の物がプレートに
    残る（実測 2026-09-29 AV024: 板 447〜921m で手前の柱・アーチが残留。BehindOcc で
    黒く塗ると黒いシルエットとして残るだけで、その奥は写らない）。
    板は _matte_clip_plane と同じく『位置 + up ベクトル』の平面とみなす。cams は
    get_camera_settings() のリスト（動くカメラは複数フレーム分。全てで手前のものだけ）。
    ⚠ 簡易判定の上限: 板を跨ぐアクターは隠さない（near clip と BehindOcc の黒塗りが
    受け持つ）。板が複数なら全ての板平面より手前のものだけ。ISM はコンポーネント
    単位でなくアクターのバウンズで判定。厳密にやるなら画素単位の斜めクリップが要る。"""
    planes = []
    for m in matte_actors or []:
        n = m.get_actor_up_vector()
        if n.length() < 1e-3:
            n = m.get_actor_forward_vector()
        n = n.normal()
        b = m.get_actor_location()
        planes.append(((b.x, b.y, b.z), (n.x, n.y, n.z)))
    cam_locs = []
    for c in cams or []:
        t = c["transform"].translation
        cam_locs.append((t.x, t.y, t.z))
    if not planes or not cam_locs:
        return []
    skip = set(matte_actors)
    out = []
    # get_all_level_actors() は LevelInstance の中身を返さない（AV024 の柱は全てそこ）
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            _get_editor_world(), unreal.Actor):
        if a in skip:
            continue
        try:
            # 見えているメッシュを持つ通常アクターだけ。ライト持ち（UDS 等）は隠すと
            # 照明が変わる。カメラはレンダ視点、HV は volumetrics_nearer_than が受け持つ
            if (isinstance(a, unreal.CameraActor) or is_volumetric_actor(a)
                    or not any(c.is_visible() for c in
                               a.get_components_by_class(unreal.MeshComponent))
                    or a.get_components_by_class(unreal.LightComponentBase)
                    or a.is_hidden_ed() or a.get_editor_property("hidden")):
                continue
            origin, ext = a.get_actor_bounds(False)
        except Exception:
            continue
        if max(ext.x, ext.y, ext.z) <= 0.0:
            continue
        if _box_in_front_of_planes((origin.x, origin.y, origin.z),
                                   (ext.x, ext.y, ext.z), planes, cam_locs,
                                   straddle_ratio):
            out.append(a)
    return out


def capture_behind_matte(world, settings, cam, w, h, ts, spawned):
    """マット面より『手前』を描画時クリップで除去し、奥だけを描画する。
    形状はマット自身の per-pixel シルエットでマスク（マット形状に切り抜く）。
    手前ジオメトリはレンダー時クリップで実際に消えるので、奥が露出する（フォリッジ含む）。
    出力: behindmatte.png(マット形状に切抜き RGBA) と behindmatte_full.png(全画面クリップ)。"""
    actors = _resolve_target_actors(settings.matte_actors, settings.matte_actor_names)
    if not actors:
        _warn("behind-matte: マット対象がありません。スキップします。")
        return None
    aa = settings.aa_factor
    outs = []

    # 1) マットのシルエット（形状・per-pixel）を show-only 深度で取得
    #    （600m 超の板面も拾えるよう RGBA32F。定数値なら 16F へ退避）
    matte_d, far = _render_depth_r_auto(world, cam, w * aa, h * aa, spawned,
                                        {"use32": True}, label="behind-matte",
                                        show_only_actors=actors)
    matte_vis = (matte_d < far).astype(_np.float32)

    # 2) マット面からクリッププレーンを導出
    base, normal = _matte_clip_plane(actors, cam)
    _log("behind-matte clip: base=(%.0f,%.0f,%.0f) normal=(%.2f,%.2f,%.2f)"
         % (base.x, base.y, base.z, normal.x, normal.y, normal.z))

    # 3) クリップ有効・マット自身は隠して Beauty(Fill) を撮る → 手前が消え奥が出る
    rgb = _render_final_color(world, settings, cam, w, h, aa, spawned,
                              hidden_actors=actors, clip_base=base, clip_normal=normal)
    rgb = rgb[:, :, :3] if (rgb.ndim == 3 and rgb.shape[2] >= 3) else rgb

    # 4a) 全画面クリップ版（マット面より手前は全部消える）
    out_full = _out_path(settings, ts, "BehindFull", ".png")
    _write_png_u8(out_full, rgb)
    _log("behind-matte(全画面クリップ) 出力: %s" % out_full)
    outs.append(out_full)

    # 4b) マット形状に切り抜いた RGBA（α=マットシルエット）
    alpha = _downscale(matte_vis * 255.0, aa)
    rgba = _np.dstack([rgb, alpha[:, :, None]]) if alpha.ndim == 2 else _np.dstack([rgb, alpha])
    out = _out_path(settings, ts, "Behind", ".png")
    _write_png_u8(out, rgba)
    _log("behind-matte(マット形状切抜き) 出力: %s" % out)
    outs.append(out)
    return outs


def capture_behind_matte_mask(world, settings, cam, w, h, ts, spawned):
    """マット対象の『全投影シルエット』マスク(白)を出力（オクルージョン非考慮）。
    向こう側だけレンダ(窓抜き)の α として使う。マスクPNGのパスを返す。"""
    actors = _resolve_target_actors(settings.matte_actors, settings.matte_actor_names)
    if not actors:
        _warn("behind-matte: マット対象がありません。スキップします。")
        return None
    aa = settings.aa_factor
    grp, far = _render_depth_r_auto(world, cam, w * aa, h * aa, spawned,
                                    {"use32": True}, label="behind-matte",
                                    show_only_actors=actors)
    mask = (grp < far).astype(_np.float32) * 255.0  # 全シルエット（手前遮蔽は無視）
    mask = _downscale(mask, aa)
    out = _out_path(settings, ts, "BehindMask", ".png")
    _write_png_u8(out, mask)
    _log("behind-matte シルエット出力: %s" % out)
    return out


def compose_rgba(rgb_path, mask_path, out_path):
    """rgb_path(レンダ画像) の RGB と mask_path(L) を合成して RGBA を out_path に書く。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    if not (rgb_path and os.path.isfile(rgb_path) and mask_path and os.path.isfile(mask_path)):
        _warn("compose_rgba: 入力不足 rgb=%s mask=%s" % (rgb_path, mask_path))
        return None
    rgb = _np.asarray(_PILImage.open(rgb_path).convert("RGB"), dtype=_np.float32)
    H, W = rgb.shape[0], rgb.shape[1]
    m = _np.asarray(_PILImage.open(mask_path).convert("L"), dtype=_np.float32)
    if m.shape[0] != H or m.shape[1] != W:
        m = _np.asarray(_PILImage.fromarray(m.astype(_np.uint8)).resize((W, H)),
                        dtype=_np.float32)
    _write_png_u8(out_path, _np.dstack([rgb, m]))
    _log("behind-matte 合成出力: %s" % out_path)
    return out_path


def merge_cloud_alpha_into_matte(matte_path, cloud_png_path, out_path, size_wh=None):
    """Matte マスク（選択=黒/周囲=白）へ CloudMatte のα（雲の可視不透明度）を合成する。
    mask' = mask × (1 - cloud_α)。matte_path が無ければ白地から作る
    （雲だけがマット対象のケース）。出力パスを返す（入力不足なら None）。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    if not (cloud_png_path and os.path.isfile(cloud_png_path)):
        _warn("merge_cloud_alpha: CloudMatte 画像がありません: %s" % cloud_png_path)
        return None
    im = _PILImage.open(cloud_png_path)
    if im.mode != "RGBA":
        _warn("merge_cloud_alpha: CloudMatte にαがありません（mode=%s）" % im.mode)
        return None
    ca = _np.asarray(im)[:, :, 3].astype(_np.float32) / 255.0
    H, W = ca.shape
    if matte_path and os.path.isfile(matte_path):
        mask = _np.asarray(_PILImage.open(matte_path).convert("L"), dtype=_np.float32)
        if mask.shape != (H, W):
            mask = _np.asarray(_PILImage.fromarray(mask.astype(_np.uint8)).resize((W, H)),
                               dtype=_np.float32)
    else:
        mask = _np.full((H, W), 255.0, dtype=_np.float32)
    merged = mask * (1.0 - ca)
    _write_png_u8(out_path, merged)
    _log("Matte マスクへ雲αを合成: %s" % out_path)
    return out_path


def cloudmatte_alpha_to_mask(path, invert=True):
    """CloudMatte PNG（RGB≒黒 + α=可視雲不透明度）を白黒マスク RGB PNG に変換する
    （in place・MP4 エンコード可能になる）。invert=True（既定）で 雲=黒/なし=白。"""
    if not _HAS_PIL:
        raise RuntimeError("Pillow がありません（雲マットの白黒変換に必要）")
    im = _PILImage.open(path)
    if im.mode != "RGBA":
        raise RuntimeError("CloudMatte にαがありません（mode=%s）" % im.mode)
    a = im.getchannel("A")
    if invert:
        a = a.point(lambda v: 255 - v)
    _PILImage.merge("RGB", (a, a, a)).save(path)


def cloudmatte_frames_to_mask(output_dir, name_body, take_str, invert=True):
    """CloudMatte 連番（%s_CloudMatte_%s.NNNN.png）を全フレーム白黒マスク化する。
    変換したフレーム数を返す。"""
    prefix = "%s_CloudMatte_%s." % (name_body, take_str)
    n = 0
    for f in sorted(os.listdir(output_dir)):
        if not (f.startswith(prefix) and f.endswith(".png")):
            continue
        try:
            cloudmatte_alpha_to_mask(os.path.join(output_dir, f), invert=invert)
            n += 1
        except Exception as e:
            _warn("雲マット白黒変換に失敗 %s: %s" % (f, e))
    _log("雲マット白黒変換: %d フレーム" % n)
    return n


def apply_cloud_black(path, cloud_path):
    """画像の雲領域を黒に落とす: img' = img × (1 − 雲α)。Normal(RGB) と
    Depth(8bit RGB / 16bit I;16 グレー) に対応（EXR 生cm は 0=カメラ位置になり
    意味が壊れるため呼ばないこと）。cloud_path は α のままの CloudMatte
    （白黒変換前）であること。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        raise RuntimeError("numpy/Pillow がありません（雲抜きに必要）")
    cm = _PILImage.open(cloud_path)
    if cm.mode != "RGBA":
        raise RuntimeError("CloudMatte にαがありません（mode=%s）" % cm.mode)
    im = _PILImage.open(path)
    if cm.size != im.size:
        cm = cm.resize(im.size)
    ca = _np.asarray(cm)[:, :, 3].astype(_np.float32) / 255.0
    if im.mode in ("I;16", "I"):
        arr = _np.asarray(im).astype(_np.float32) / 65535.0
        _write_png_u16_gray(path, arr * (1.0 - ca))
    else:
        # sRGB 値に直接 (1−α) を掛けるとリニア換算 (1−α)^2.2 で二重に濃くなる
        # （雲マットのαは正しいのに Normal だけ濃く見える実測 2026-08-26 AV024
        # take020）。リニア化して乗算するが、純リニア(m=1)は薄く感じるとの
        # 目視評価により、強さは _CLOUD_MUL_POW で連続調整する（(1−α)^m）
        arr = _np.asarray(im.convert("RGB")).astype(_np.float32) / 255.0
        keep = _np.power(_np.clip(1.0 - ca, 0.0, 1.0), _CLOUD_MUL_POW)
        lin = _np.power(arr, 2.2) * keep[:, :, None]
        out = (_np.power(_np.clip(lin, 0.0, 1.0), 1.0 / 2.2) * 255.0 + 0.5
               ).astype(_np.uint8)
        _PILImage.fromarray(out, "RGB").save(path)


def apply_cloud_black_to_pass_frames(output_dir, name_body, take_str, pass_name):
    """<pass_name> 連番の各フレームへ同フレームの CloudMatte α を乗算して
    雲領域を黒にする。処理したフレーム数を返す（CloudMatte 側が α のままの
    状態で呼ぶこと）。"""
    prefix = "%s_%s_%s." % (name_body, pass_name, take_str)
    n = 0
    for f in sorted(os.listdir(output_dir)):
        if not (f.startswith(prefix) and f.endswith(".png")):
            continue
        frame = f[len(prefix):-4]
        cf = os.path.join(output_dir,
                          "%s_CloudMatte_%s.%s.png" % (name_body, take_str, frame))
        if not os.path.isfile(cf):
            continue
        try:
            apply_cloud_black(os.path.join(output_dir, f), cf)
            n += 1
        except Exception as e:
            _warn("%s の雲抜きに失敗 %s: %s" % (pass_name, f, e))
    _log("%s の雲抜き: %d フレーム" % (pass_name, n))
    return n


def apply_matte_black(path, matte_path):
    """画像のマット対象領域を黒に落とす: img' = img × (Matte/255)。
    matte_path は Matte 連番（選択=黒 / それ以外=白 の可視性マスク）。
    遮蔽考慮済みなので、マットより手前のキャラ等は白のまま＝黒く塗られない。
    全投影シルエットの MatteSil を使うと手前のキャラまで潰れるので使わないこと。
    Normal(RGB) と Depth(8bit RGB / 16bit I;16 グレー) に対応。
    マスクはほぼ二値なので、雲αで必要だったリニア空間での乗算補正は要らない
    （効くのは縁の中間画素だけで、しかも黒へ寄せる方向）。
    ⚠️ Depth の EXR 生cm は 0=カメラ位置になり意味が壊れるため呼ばないこと。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        raise RuntimeError("numpy/Pillow がありません（マット黒塗りに必要）")
    mm = _PILImage.open(matte_path).convert("L")
    im = _PILImage.open(path)
    if mm.size != im.size:
        mm = mm.resize(im.size)
    keep = _np.asarray(mm).astype(_np.float32) / 255.0
    if im.mode in ("I;16", "I"):
        arr = _np.asarray(im).astype(_np.float32) / 65535.0
        _write_png_u16_gray(path, arr * keep)
    else:
        arr = _np.asarray(im.convert("RGB")).astype(_np.float32)
        out = (_np.clip(arr * keep[:, :, None], 0.0, 255.0) + 0.5).astype(_np.uint8)
        _PILImage.fromarray(out, "RGB").save(path)


def apply_matte_black_to_pass_frames(output_dir, name_body, take_str, pass_name):
    """<pass_name> 連番の各フレームへ同フレームの Matte を乗算し、マット対象が
    写っている画素を黒にする。処理したフレーム数を返す（Matte 連番が無ければ 0）。"""
    prefix = "%s_%s_%s." % (name_body, pass_name, take_str)
    n = 0
    for f in sorted(os.listdir(output_dir)):
        if not (f.startswith(prefix) and f.endswith(".png")):
            continue
        frame = f[len(prefix):-4]
        mf = os.path.join(output_dir,
                          "%s_Matte_%s.%s.png" % (name_body, take_str, frame))
        if not os.path.isfile(mf):
            continue
        try:
            apply_matte_black(os.path.join(output_dir, f), mf)
            n += 1
        except Exception as e:
            _warn("%s のマット黒塗りに失敗 %s: %s" % (pass_name, f, e))
    _log("%s のマット黒塗り: %d フレーム" % (pass_name, n))
    return n


def _read_linear_gray(path, ffmpeg):
    """レンダ出力を線形 float 配列で読む。EXR は同梱 ffmpeg で G チャンネルを
    そのまま PFM(float32) に抜いて読む（extractplanes＝色変換なし・クリップなしの
    ビット一致を実測。PIZ 圧縮対応・OpenEXR 依存を増やさない）。
    PNG(I;16/L) は直接読む。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    import subprocess as _subprocess
    p = path
    tmp = None
    if path.lower().endswith(".exr"):
        if not ffmpeg:
            _warn("バッキング: ffmpeg が見つからず EXR を読めません")
            return None
        tmp = path + "._g.pfm"
        # creationflags: コンソールウインドウを出さない（フレーム毎の起動で
        # ウインドウが連続開閉してエディタの UI 操作を奪う・ユーザー報告）
        try:
            r = _subprocess.run([ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", path,
                                 "-vf", "format=gbrpf32le,extractplanes=g", tmp],
                                capture_output=True, creationflags=0x08000000,
                                timeout=600)
        except _subprocess.TimeoutExpired:
            _warn("バッキング: EXR 変換がタイムアウト %s" % path)
            return None
        if r.returncode != 0 or not os.path.isfile(tmp):
            _warn("バッキング: EXR 変換失敗 %s: %s" % (path, r.stderr.decode(errors="replace")[-200:]))
            return None
        p = tmp
    try:
        im = _PILImage.open(p)
        a = _np.asarray(im).astype(_np.float32)
        if im.mode in ("I;16", "I"):
            a = a / 65535.0
        elif im.mode != "F":
            a = a / 255.0
        if a.ndim == 3:
            a = a.mean(axis=-1)
        return a
    except Exception as e:
        # 例外を呼び出し側の連番ループへ漏らさない（1フレームの破損で
        # 後続フレーム処理と EXR 掃除が丸ごと飛ぶのを防ぐ）
        _warn("バッキング: 画像読み込み失敗 %s: %s" % (p, e))
        return None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def _backing_t_from_array(w, scale=None):
    """白バッキング1レンダの輝度配列から透過率 T (0..1) と正規化スケールを返す。
    scale = 素の板(遮蔽なし)の輝度レベル。フレーム間で固定したい場合は指定する。"""
    if scale is None:
        pos = w[w > max(float(w.max()), 1e-6) * 0.05]
        if pos.size == 0:
            return None, None
        scale = float(_np.percentile(pos, 99.5))
    if scale <= 1e-6:
        return None, None
    return _np.clip(w / scale, 0.0, 1.0), scale


def _read_linear_alpha(path, ffmpeg):
    """EXR の α チャンネルを線形 float 配列で読む（_read_linear_gray の α 版）。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    import subprocess as _subprocess
    if not path.lower().endswith(".exr"):
        try:
            im = _PILImage.open(path)
            if im.mode == "RGBA":
                return _np.asarray(im)[:, :, 3].astype(_np.float32) / 255.0
        except Exception:
            pass
        return None
    if not ffmpeg:
        _warn("可視雲: ffmpeg が見つからず EXR αを読めません")
        return None
    tmp = path + "._a.pfm"
    try:
        r = _subprocess.run([ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", path,
                             "-vf", "format=gbrapf32le,extractplanes=a", tmp],
                            capture_output=True, creationflags=0x08000000,
                            timeout=600)
    except _subprocess.TimeoutExpired:
        _warn("可視雲: EXR α抽出がタイムアウト %s" % path)
        return None
    if r.returncode != 0 or not os.path.isfile(tmp):
        _warn("可視雲: EXR α抽出失敗 %s: %s"
              % (path, r.stderr.decode(errors="replace")[-200:]))
        return None
    try:
        return _np.asarray(_PILImage.open(tmp)).astype(_np.float32)
    except Exception as e:
        _warn("可視雲: α読み込み失敗 %s: %s" % (tmp, e))
        return None
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


# 可視雲のジオメトリ画素に掛ける飽和ゲイン（1−e^(−G·x) 正規化）。上げるほど
# 雲の中身が早く不透明になる（輪郭・貫通の筋は輝度≈0のため影響が小さい）
_VIS_CLOUD_GAIN = 7.0


# --- H5 知覚モデル（静止画の可視雲。2026-08-26 frame84 でユーザー目視確定） ---
# per = V/(V + K·T·Bg): V=黒バッキング素照明のベール輝度 / T=白バッキング透過率 /
# Bg=雲なしBeautyのリニア背景輝度。日照ブースト 1−(1−per)(1−S·V^P) →
# カーブ 1−e^(−G·x) → 全体の濃度は透明度ガンマ (1−(1−vis)^γ) で調整する。
_VIS_CLOUD_K = 0.7          # コントラスト項の背景寄与（小さいほど全体が濃い）
_VIS_CLOUD_CURVE_G = 2.0    # 飽和カーブ
_VIS_SUN_BOOST_S = 0.8      # 日照（高ベール輝度）画素の不透明化強さ
_VIS_SUN_BOOST_P = 1.0      # 日照ブーストの集中度（2で最明部限定）
_VIS_DENSE_GAMMA = 1.5      # 全体の濃さ（1=素・1.5=一段濃い・2.2=二段濃い）
_CLOUD_MUL_POW = 2.2        # Normal/Depth 雲抜きの乗算強さ（リニア空間で (1−α)^m。
                            # 1.0=リニア正確=薄め / 2.2=旧sRGB直乗算相当=濃い）


def vis_cloud_gain_tag():
    """雲マット/雲抜き出力のファイル名タグ（例 'H5d1.5'）。濃度設定の識別用。"""
    return "H5d%g" % _VIS_DENSE_GAMMA


def compose_visible_cloud(w_exr, geomask_exr, out_rgba_png, ffmpeg=None, scale=None):
    """可視雲マット合成（黒バッキング方式）: 全ジオメトリを黒アンリット材で
    レンダした EXR（RGB=雲の散乱輝度 L・α=全投影雲α）と GeoMask（ジオメトリ有=白）
    から「見えている雲量」を α に持つ RGBA PNG を書く。
    L は遮蔽考慮（黒い物体が手前なら 0）の知覚的カバレッジ。空画素は α が正確な
    ので、空の雲画素で scale=median(α/L) を自己較正してジオメトリ画素へ L×scale を
    適用する（白バッキングの 1−T は物理透過率で Beauty の見た目より薄い実測
    2026-08-26 → 知覚合わせの輝度方式へ変更）。
    戻り値 (out_path, scale)。失敗時 (None, None)。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None, None
    L = _read_linear_gray(w_exr, ffmpeg)
    a = _read_linear_alpha(w_exr, ffmpeg)
    g = _read_linear_gray(geomask_exr, ffmpeg) if geomask_exr else None
    if L is None or a is None:
        _warn("可視雲: 入力を読めません (L=%s α=%s)"
              % (L is not None, a is not None))
        return None, None
    # UE のシーンリニア EXR は α を「1−カバレッジ」で格納する（空=1・不透明=0。
    # AV024 実測 2026-08-26。PNG 出力は MRQ が標準向きへ反転して書く）
    a = 1.0 - _np.clip(a, 0.0, 1.0)
    if g is None:
        _warn("可視雲: GeoMask なし＝空画素αのみで合成（遮蔽は反映されない）")
        vis = a
        scale = scale or 1.0
    else:
        if scale is None:
            sel = (g < 0.5) & (a > 0.15) & (L > 1e-6)
            if sel.sum() >= 100:
                scale = float(_np.median(a[sel] / L[sel]))
            else:
                # 空に雲が無い構図: 輝度の高位パーセンタイルで代用
                lv = L[L > 1e-6]
                scale = (1.0 / max(float(_np.percentile(lv, 99)), 1e-6)
                         if lv.size else 1.0)
                _warn("可視雲: 空の雲画素が少なく scale=%.4f を輝度から推定" % scale)
        # ジオメトリ画素: 雲の消衰で内部ほど輝度が落ち「輪郭は合うが中身が薄い」
        # マットになる（フレーム84実測）。飽和カーブ 1−e^(−G·x) で中身を不透明側へ
        # 押し上げる（柱の筋など輝度≈0はカーブ後もほぼ0のままで貫通表現は保たれる）
        G = _VIS_CLOUD_GAIN
        x = _np.clip(L * scale, 0.0, None)
        vis_geo = (1.0 - _np.exp(-G * x)) / (1.0 - _np.exp(-G))
        vis = _np.where(g > 0.5, _np.clip(vis_geo, 0.0, 1.0), a)
    vis8 = (_np.clip(vis, 0.0, 1.0) * 255.0 + 0.5).astype(_np.uint8)
    z = _np.zeros_like(vis8)
    _PILImage.merge("RGBA", tuple(_PILImage.fromarray(c) for c in (z, z, z, vis8))
                    ).save(out_rgba_png)
    _log("可視雲マット合成: %s (scale=%.4f)" % (out_rgba_png, scale))
    return out_rgba_png, scale


def compose_visible_cloud_sequence(output_dir, name_body, take_str, ffmpeg=None):
    """映像用: CloudMatte(EXR W+α) + GeoMask(EXR) 連番から可視雲 RGBA PNG 連番を作る。
    正規化スケールは最初の成功フレームで固定（チラつき防止）。使用済み EXR は削除。
    書いたフレーム数を返す。"""
    pat = re.compile(re.escape("%s_CloudMatte_%s." % (name_body, take_str))
                     + r"(\d+)\.exr$")
    frames = {}
    for f in os.listdir(output_dir):
        m = pat.match(f)
        if m:
            frames[m.group(1)] = f
    n = 0
    scale = None
    for fr in sorted(frames):
        wp = os.path.join(output_dir, frames[fr])
        gp = os.path.join(output_dir,
                          "%s_GeoMask_%s.%s.exr" % (name_body, take_str, fr))
        op = os.path.join(output_dir,
                          "%s_CloudMatte_%s.%s.png" % (name_body, take_str, fr))
        r, sc = compose_visible_cloud(wp, gp if os.path.isfile(gp) else None,
                                      op, ffmpeg=ffmpeg, scale=scale)
        if r:
            n += 1
            if scale is None:
                scale = sc
    # EXR は後段（PNG合成・MP4）で不要なので全て削除
    for f in list(os.listdir(output_dir)):
        if (pat.match(f) or f.startswith("%s_GeoMask_%s." % (name_body, take_str))
                and f.endswith(".exr")):
            try:
                os.remove(os.path.join(output_dir, f))
            except Exception as e:
                _warn("可視雲: 中間 EXR を削除できません %s: %s" % (f, e))
    _log("可視雲マット: %d フレーム合成" % n)
    return n


def compose_visible_cloud_h5(w_exr, geomask_exr, veil_exr, bg_png,
                             out_rgba_png, ffmpeg=None, veil_none_exr=None,
                             norm=None):
    """H5 知覚モデルの可視雲マット合成（静止画）。
    w_exr=白バッキング（RGB=T×素板・α=全投影雲α）/ geomask_exr=ジオメトリ有無 /
    veil_exr=黒バッキング素照明（RGB=雲ベール輝度）/ bg_png=雲なしBeauty。
    veil_none_exr を渡すと4レンダのペア方式: V = VeilAll − VeilNone（大気・雲ON
    ベール同士の差分）。UDS 雲レイヤー/フォグ板の雲も V に乗り、空のグラデと
    大気ヘイズは正確に相殺される（HV だけの旧3レンダ方式は中距離の層雲を
    取りこぼす実測 2026-08-26）。空領域はαではなく T=1 の同式で合成する。
    出力: α=可視雲量の RGBA PNG。パラメータは _VIS_CLOUD_K ほか（モジュール定数）。
    norm に dict を渡すと正規化値（plate/v99）を共有する: キーが有れば使い、
    無ければ計算して書き込む（映像でフレーム毎に正規化が揺れてチラつくのを防ぐ）。
    戻り値: 出力パス（失敗時 None）。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    W = _read_linear_gray(w_exr, ffmpeg)
    A = _read_linear_alpha(w_exr, ffmpeg)
    Gm = _read_linear_gray(geomask_exr, ffmpeg) if geomask_exr else None
    V = _read_linear_gray(veil_exr, ffmpeg) if veil_exr else None
    Vn0 = (_read_linear_gray(veil_none_exr, ffmpeg)
           if veil_none_exr else None)
    if W is None or A is None:
        _warn("可視雲H5: 白バッキング入力を読めません")
        return None
    A = 1.0 - _np.clip(A, 0.0, 1.0)   # UE の EXR α は 1−カバレッジ格納
    if Gm is None or V is None:
        _warn("可視雲H5: GeoMask/Veil が無いためαのみで合成（遮蔽なし）")
        vis = A
    else:
        geo = Gm > 0.5
        if norm is not None and "plate" in norm:
            plate = float(norm["plate"])
        else:
            plate = float(_np.percentile(W[geo], 99)) if geo.sum() else 0.0
            if norm is not None and plate > 1e-6:
                norm["plate"] = plate
        if plate > 1e-6:
            T = _np.clip(W / plate, 0.0, 1.0)
        elif Vn0 is not None and not bool(geo.any()):
            # 空だけのフレーム（ジオメトリ無し）: ペア方式は空を T=1 で計算するので
            # 素板レベルは不要（ここで落とすと連番に欠番が出て MP4 が短くなる）
            T = _np.ones_like(W)
        else:
            _warn("可視雲H5: 素板レベルを推定できません")
            return None
        if Vn0 is not None:
            V = _np.clip(V - Vn0, 0.0, None)   # ペア差分＝純粋な雲ベール輝度
            T = _np.where(geo, T, 1.0)         # 空は「明るい背景」として同式で扱う
        if norm is not None and "v99" in norm:
            v99 = float(norm["v99"])
        else:
            vv = V[V > 1e-6]
            v99 = float(_np.percentile(vv, 99)) if vv.size else 1.0
            if norm is not None and v99 > 1e-6:
                norm["v99"] = v99
        V = _np.clip(V / max(v99, 1e-6), 0.0, 1.0)
        try:
            bg_im = _PILImage.open(bg_png).convert("L")
            if bg_im.size != (W.shape[1], W.shape[0]):
                bg_im = bg_im.resize((W.shape[1], W.shape[0]))
            Bg = _np.power(_np.asarray(bg_im).astype(_np.float32) / 255.0, 2.2)
        except Exception as e:
            _warn("可視雲H5: 雲なしBeautyを読めません（背景=0.5固定で続行）: %s" % e)
            Bg = _np.full_like(W, 0.5)
        per = V / (V + _VIS_CLOUD_K * T * Bg + 1e-6)
        boosted = 1.0 - (1.0 - _np.clip(per, 0.0, 1.0)) \
            * (1.0 - _VIS_SUN_BOOST_S * _np.power(V, _VIS_SUN_BOOST_P))
        G = _VIS_CLOUD_CURVE_G
        visg = (1.0 - _np.exp(-G * _np.clip(boosted, 0.0, 1.0))) \
            / (1.0 - _np.exp(-G))
        if Vn0 is not None:
            # ペア方式: 空領域も同式（大気ONの W はαが使えないため）
            vis = _np.clip(visg, 0.0, 1.0)
        else:
            vis = _np.where(geo, _np.clip(visg, 0.0, 1.0), A)
    vis = 1.0 - _np.power(1.0 - _np.clip(vis, 0.0, 1.0), _VIS_DENSE_GAMMA)
    vis8 = (_np.clip(vis, 0.0, 1.0) * 255.0 + 0.5).astype(_np.uint8)
    z = _np.zeros_like(vis8)
    _PILImage.merge("RGBA", tuple(_PILImage.fromarray(c) for c in (z, z, z, vis8))
                    ).save(out_rgba_png)
    _log("可視雲H5合成: %s (K=%g G=%g S=%g P=%g γ=%g)"
         % (out_rgba_png, _VIS_CLOUD_K, _VIS_CLOUD_CURVE_G,
            _VIS_SUN_BOOST_S, _VIS_SUN_BOOST_P, _VIS_DENSE_GAMMA))
    return out_rgba_png


def compose_visible_cloud_h5_sequence(output_dir, name_body, take_str,
                                      ffmpeg=None):
    """映像用: H5 ペア方式の4レンダ連番（CloudMatte=白W+GeoMask / CloudVeil /
    CloudVeil0 / CloudBG）から可視雲 RGBA PNG 連番を作る。正規化（素板レベル・
    ベールp99）は最初の成功フレームで固定（フレーム間のチラつき防止）。
    使用済みの EXR と CloudBG PNG は削除する。書いたフレーム数を返す。"""
    pat = re.compile(re.escape("%s_CloudMatte_%s." % (name_body, take_str))
                     + r"(\d+)\.exr$")
    frames = {}
    for f in os.listdir(output_dir):
        m = pat.match(f)
        if m:
            frames[m.group(1)] = f
    n = 0
    norm = {}
    failed = set()
    for fr in sorted(frames):
        wp = os.path.join(output_dir, frames[fr])
        gp = os.path.join(output_dir,
                          "%s_GeoMask_%s.%s.exr" % (name_body, take_str, fr))
        vp = os.path.join(output_dir,
                          "%s_CloudVeil_%s.%s.exr" % (name_body, take_str, fr))
        v0 = os.path.join(output_dir,
                          "%s_CloudVeil0_%s.%s.exr" % (name_body, take_str, fr))
        bg = os.path.join(output_dir,
                          "%s_CloudBG_%s.%s.png" % (name_body, take_str, fr))
        op = os.path.join(output_dir,
                          "%s_CloudMatte_%s.%s.png" % (name_body, take_str, fr))
        r = compose_visible_cloud_h5(
            wp, gp if os.path.isfile(gp) else None,
            vp if os.path.isfile(vp) else None,
            bg, op, ffmpeg=ffmpeg,
            veil_none_exr=v0 if os.path.isfile(v0) else None,
            norm=norm)
        if r:
            n += 1
        else:
            failed.add(fr)
    # 中間素材は後段（PNG合成・MP4）で不要なので削除。合成に失敗したフレームの
    # 入力は診断用に残す
    fr_pat = re.compile(r"\.(\d+)\.(exr|png)$")
    for f in list(os.listdir(output_dir)):
        drop = False
        for tag in ("CloudMatte", "GeoMask", "CloudVeil", "CloudVeil0"):
            if (f.startswith("%s_%s_%s." % (name_body, tag, take_str))
                    and f.endswith(".exr")):
                drop = True
        if (f.startswith("%s_CloudBG_%s." % (name_body, take_str))
                and f.endswith(".png")):
            drop = True
        m = fr_pat.search(f)
        if drop and m and m.group(1) in failed:
            drop = False
        if drop:
            try:
                os.remove(os.path.join(output_dir, f))
            except Exception as e:
                _warn("可視雲H5: 中間素材を削除できません %s: %s" % (f, e))
    if failed:
        _warn("可視雲H5: %d フレームの合成に失敗（入力 EXR を残しました: %s）"
              % (len(failed), ", ".join(sorted(failed)[:10])))
    _log("可視雲H5マット(ペア方式): %d フレーム合成" % n)
    return n


def backing_t_png(white_path, out_path, ffmpeg=None, scale=None):
    """白(発光100)バッキング1レンダから「板より手前の透過率T」16bit グレー PNG を書く。
    W = 手前の発光C + T×100×露出 で C/100 は無視できるため T ≈ W/素板レベル。
    板の後ろの内容は不透明な板に遮られて写らないため、板の後ろの雲は T に
    影響しない（=他オブジェクトと同じ前後関係の挙動）。
    戻り値 (out_path, scale)。失敗時 (None, None)。"""
    w = _read_linear_gray(white_path, ffmpeg)
    if w is None:
        _warn("バッキング: 入力を読めません（%s）" % white_path)
        return None, None
    t, scale = _backing_t_from_array(w, scale)
    if t is None:
        _warn("バッキング: 素の板領域が見つからず正規化できません")
        return None, None
    _write_png_u16_gray(out_path, t)
    _log("バッキングT: %s (scale=%.4f)" % (out_path, scale))
    return out_path, scale


def backing_t_sequence(output_dir, name_body, take_str, start, end_excl, ffmpeg):
    """映像用: BackingW の EXR 連番から %s_BackingT_%s.NNNN.png 連番を作る。
    対象は [start, end_excl)（排他的終端・trim_sequence_frames と同じ）。
    正規化スケールは最初のフレームで決めて全フレームに固定（フレーム間のチラつき防止）。
    使用後の EXR は範囲外も含め全て削除する。書いたフレーム数を返す。"""
    pat_w = re.compile(re.escape("%s_BackingW_%s." % (name_body, take_str)) + r"(\d+)\.exr$")
    frames = {}
    for f in os.listdir(output_dir):
        m = pat_w.match(f)
        if m:
            frames[m.group(1)] = f
    n = 0
    scale = None
    for fr in sorted(frames):
        if not (int(start) <= int(fr) < int(end_excl)):
            continue
        wp = os.path.join(output_dir, frames[fr])
        op = os.path.join(output_dir, "%s_BackingT_%s.%s.png" % (name_body, take_str, fr))
        r, sc = backing_t_png(wp, op, ffmpeg=ffmpeg, scale=scale)
        if r:
            n += 1
            if scale is None:
                scale = sc      # 失敗フレームで固定スケールを潰さない（チラつき防止）
    prefix = "%s_BackingW_%s." % (name_body, take_str)
    for f in os.listdir(output_dir):
        if f.startswith(prefix) and f.endswith(".exr"):
            try:
                os.remove(os.path.join(output_dir, f))
            except Exception as e:
                _warn("バッキング: 中間 EXR を削除できません %s: %s" % (f, e))
    _log("バッキングT: %d フレーム出力" % n)
    return n


def merge_backing_t_into_matte(matte_path, t_png, out_path):
    """Matte マスク（選択=黒/周囲=白）へバッキング差分の透過率Tを統合する。
    mask' = 255 − (255−mask) × T ＝「板の穴は、手前の内容(雲・半透明)の透過率の分
    だけしか開かない」。板領域外は mask=255 なので T の値に関わらず不変
    （領域判定が不要になるのがこの式の要点）。出力パスを返す。"""
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    if not (matte_path and os.path.isfile(matte_path)):
        _warn("merge_backing_t: 板の Matte マスクがありません: %s" % matte_path)
        return None
    if not (t_png and os.path.isfile(t_png)):
        _warn("merge_backing_t: T 画像がありません: %s" % t_png)
        return None
    mask = _np.asarray(_PILImage.open(matte_path).convert("L"), dtype=_np.float32)
    tim = _PILImage.open(t_png)
    t = _np.asarray(tim).astype(_np.float32) / (65535.0 if tim.mode in ("I;16", "I") else 255.0)
    if t.shape != mask.shape:
        _warn("merge_backing_t: 解像度不一致 %s vs %s" % (t.shape, mask.shape))
        return None
    merged = 255.0 - (255.0 - mask) * t
    _write_png_u8(out_path, merged)
    _log("Matte マスクへバッキングTを合成: %s" % out_path)
    return out_path


def merge_cloud_objid(objid_png, manifest_path, cloud_entries, threshold=0.5):
    """ObjectID 静止画へ雲のIDを合成する。cloud_entries = [(label, cloud_png_path)]。
    各雲は CloudMatte のα >= threshold の画素を専用色で塗る（後勝ち）。
    色相は既存マニフェストのエントリ数から黄金角で継続。JSON も更新する。"""
    import colorsys
    import json as _json
    if not (_HAS_NUMPY and _HAS_PIL):
        return None
    if not (objid_png and os.path.isfile(objid_png)):
        _warn("merge_cloud_objid: ObjectID 画像がありません: %s" % objid_png)
        return None
    img = _np.asarray(_PILImage.open(objid_png).convert("RGB")).astype(_np.float32)
    man = {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            man = _json.load(f)
    except Exception:
        pass
    idx = len(man)
    n = 0
    for label, cpath in cloud_entries:
        if not (cpath and os.path.isfile(cpath)):
            _warn("merge_cloud_objid: %s のCloudMatteがありません" % label)
            continue
        im = _PILImage.open(cpath)
        if im.mode != "RGBA":
            continue
        ca = _np.asarray(im)[:, :, 3].astype(_np.float32) / 255.0
        if ca.shape != img.shape[:2]:
            ca = _np.asarray(_PILImage.fromarray((ca * 255).astype(_np.uint8))
                             .resize((img.shape[1], img.shape[0])),
                             dtype=_np.float32) / 255.0
        vis = ca >= float(threshold)
        if not bool(vis.any()):
            continue
        # 既存マニフェスト（メッシュ側パレット）と偶然同色になったらずらす
        for _ in range(64):
            hue = (idx * 0.6180339887498949) % 1.0
            sat = 0.75 + 0.20 * ((idx // 6) % 2)
            val = 1.0 - 0.20 * ((idx // 3) % 2)
            idx += 1
            r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
            col = _objid_u8(r, g, b)
            key = _objid_key(col)
            if key not in man:
                break
        img[vis] = col
        man[key] = label
        n += 1
    _write_png_u8(objid_png, img)
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            _json.dump(man, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _warn("merge_cloud_objid: JSON 更新に失敗: %s" % e)
    _log("ObjectID へ雲ID %d 件を合成: %s" % (n, objid_png))
    return objid_png


def blend_with_beauty(beauty_path, matte_path=None, objid_path=None,
                      matte_out=None, objid_out=None):
    """MRQ Beauty(レンダ画像) の RGB に、matte/objid のカバレッジをアルファとして合成し
    RGBA cutout を書き出す（Fill ではなく Beauty とブレンドする版）。出力パスのリストを返す。
    matte_out / objid_out で出力名を明示できる（クリーンな素材名用）。"""
    outs = []
    if not (_HAS_NUMPY and _HAS_PIL):
        return outs
    if not beauty_path or not os.path.isfile(beauty_path):
        _warn("Beauty 画像が無いため Beauty ブレンドをスキップ: %s" % beauty_path)
        return outs
    brgb = _np.asarray(_PILImage.open(beauty_path).convert("RGB"), dtype=_np.float32)
    H, W = brgb.shape[0], brgb.shape[1]

    def _fit(a):
        if a.shape[0] != H or a.shape[1] != W:
            im = _PILImage.fromarray(_np.clip(a, 0, 255).astype(_np.uint8)).resize((W, H))
            return _np.asarray(im, dtype=_np.float32)
        return a.astype(_np.float32)

    if matte_path and os.path.isfile(matte_path):
        m = _fit(_np.asarray(_PILImage.open(matte_path).convert("L"), dtype=_np.float32))
        out = matte_out or (matte_path[:-4] + "_fill.png")
        _write_png_u8(out, _np.dstack([brgb, m]))
        _log("Matte(Beauty+α) 出力: %s" % out)
        outs.append(out)
    if objid_path and os.path.isfile(objid_path):
        idimg = _np.asarray(_PILImage.open(objid_path).convert("RGB"), dtype=_np.float32)
        cov = _fit((idimg.max(axis=2) > 1.0).astype(_np.float32) * 255.0)
        out = objid_out or (objid_path[:-4] + "_fill.png")
        _write_png_u8(out, _np.dstack([brgb, cov]))
        _log("Object ID(Beauty+α) 出力: %s" % out)
        outs.append(out)
    return outs


# ----------------------------------------------------------------------------
# 一時 正規化深度 PostProcess マテリアル（シーケンスレンダの Z-Depth AOV 用）
#   MRQ の additional_post_process_materials は正規化されない生深度だと
#   8bit PNG / MP4 で白飛びするため、(SceneDepth - near)/(far - near) を
#   0..1 に clamp したマテリアルをレンダ時に一時生成し、完了後に削除する。
# ----------------------------------------------------------------------------
_TMP_MAT_PKG = "/Game/_UE5Capture_Tmp"
_TMP_MAT_NAME = "M_UE5Cap_DepthNorm"

# マット対象に付与する CustomDepth ステンシル値。ObjectID のステンシル(1..249)と
# 帯域を分けることで、ObjectID 対象の CustomDepth が Matte/MatteSil マスクへ
# 混入しない（マスクはステンシル一致で判定する）。
MATTE_STENCIL = 250


def _delete_temp_material(asset_name, label):
    full = _TMP_MAT_PKG + "/" + asset_name
    try:
        if not unreal.EditorAssetLibrary.does_asset_exist(full):
            return
        # 参照が残っていると delete_asset が False を返すため GC 後に再試行する
        if not unreal.EditorAssetLibrary.delete_asset(full):
            unreal.SystemLibrary.collect_garbage()
            if not unreal.EditorAssetLibrary.delete_asset(full):
                _warn("一時 %s マテリアルを削除できませんでした: %s" % (label, full))
    except Exception as e:
        _warn("一時 %s マテリアル削除に失敗: %s" % (label, e))


def delete_temp_depth_material():
    _delete_temp_material(_TMP_MAT_NAME, "深度")


def create_temp_depth_material(near, far, invert=True):
    """正規化深度を EmissiveColor に出す PostProcess マテリアルを用意して返す。
    invert=True で 手前=白/奥=黒（既存 Depth パスと同じ既定）。
    既存の一時アセットがあれば再利用して式だけ作り直す（undo バッファ等の参照で
    delete_asset が失敗することがあるため、削除前提にしない）。
    注意: MRQ 経由の PNG/MP4 は表示用エンコード（sRGB）で書かれるため視覚確認用。
    リニア厳密な深度が要る場合は従来の単発 Depth パス（16bit PNG）を使う。"""
    full = _TMP_MAT_PKG + "/" + _TMP_MAT_NAME
    mat = None
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        mat = unreal.EditorAssetLibrary.load_asset(full)
    if mat is None:
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset(_TMP_MAT_NAME, _TMP_MAT_PKG,
                              unreal.Material, unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("一時深度マテリアルの生成に失敗しました。")
    mat.set_editor_property("material_domain", unreal.MaterialDomain.MD_POST_PROCESS)
    mat.set_editor_property(
        "blendable_location",
        unreal.BlendableLocation.BL_SCENE_COLOR_AFTER_TONEMAPPING)

    MEL = unreal.MaterialEditingLibrary
    MEL.delete_all_material_expressions(mat)   # 再利用時は式を全消しして作り直す
    depth = MEL.create_material_expression(mat, unreal.MaterialExpressionSceneDepth, -800, 0)
    c_near = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -800, 200)
    c_near.set_editor_property("r", float(near))
    sub = MEL.create_material_expression(mat, unreal.MaterialExpressionSubtract, -600, 100)
    _mx_conn(depth, "", sub, "A")
    _mx_conn(c_near, "", sub, "B")
    c_inv = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -600, 300)
    c_inv.set_editor_property("r", 1.0 / max(float(far) - float(near), 1e-6))
    mul = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -400, 200)
    _mx_conn(sub, "", mul, "A")
    _mx_conn(c_inv, "", mul, "B")
    clamp = MEL.create_material_expression(mat, unreal.MaterialExpressionClamp, -250, 200)
    _mx_conn(mul, "", clamp, "")
    last = clamp
    if invert:
        inv = MEL.create_material_expression(mat, unreal.MaterialExpressionOneMinus, -120, 200)
        _mx_conn(clamp, "", inv, "")
        last = inv
    _mx_prop(last, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as e:
        _warn("一時深度マテリアルの保存に失敗（未保存のまま続行）: %s" % e)
    _log("一時深度マテリアル生成: near=%.0f far=%.0f invert=%s" % (near, far, invert))
    return mat


# --- マテリアルノード構築の共通ヘルパ（接続失敗は例外にする） ---
def _mx_conn(frm, out_name, to, in_name):
    """接続失敗（ピン名不一致）は False が返るだけなので必ず検証する。"""
    MEL = unreal.MaterialEditingLibrary
    if MEL.connect_material_expressions(frm, out_name, to, in_name):
        return
    if out_name and MEL.connect_material_expressions(frm, "", to, in_name):
        return
    raise RuntimeError("マテリアルのノード接続に失敗: %s.%s -> %s.%s"
                       % (type(frm).__name__, out_name, type(to).__name__, in_name))


def _mx_prop(expr, prop, out_name=""):
    """マテリアル出力ピンへの接続。こちらも失敗は False が返るだけなので検証する。"""
    if unreal.MaterialEditingLibrary.connect_material_property(expr, out_name, prop):
        return
    raise RuntimeError("マテリアル出力への接続に失敗: %s.%s -> %s"
                       % (type(expr).__name__, out_name, prop))


def _emissive_connected(mat):
    """Material の EmissiveColor に式が繋がっているか（判定 API が無ければ False）。"""
    try:
        node = unreal.MaterialEditingLibrary.get_material_property_input_node(
            mat, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    except Exception:
        return False
    return node is not None


def _mx_expr(mat, cls, x, y):
    return unreal.MaterialEditingLibrary.create_material_expression(mat, cls, x, y)


def _mx_const(mat, v, x, y):
    c = _mx_expr(mat, unreal.MaterialExpressionConstant, x, y)
    c.set_editor_property("r", float(v))
    return c


def _mx_scene_r(mat, tex_id, x, y):
    """SceneTexture の R チャンネルを取り出すノード対を作って Mask ノードを返す。"""
    st = _mx_expr(mat, unreal.MaterialExpressionSceneTexture, x, y)
    st.set_editor_property("scene_texture_id", tex_id)
    m = _mx_expr(mat, unreal.MaterialExpressionComponentMask, x + 150, y)
    m.set_editor_property("r", True)
    m.set_editor_property("g", False)
    m.set_editor_property("b", False)
    m.set_editor_property("a", False)
    _mx_conn(st, "Color", m, "")
    return m


def _mx_get_or_create_pp_material(name):
    """一時 PostProcess マテリアルを取得または新規作成し、式を全消しして返す。"""
    full = _TMP_MAT_PKG + "/" + name
    mat = None
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        mat = unreal.EditorAssetLibrary.load_asset(full)
    if mat is None:
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset(name, _TMP_MAT_PKG, unreal.Material, unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("一時マテリアルの生成に失敗しました: %s" % name)
    mat.set_editor_property("material_domain", unreal.MaterialDomain.MD_POST_PROCESS)
    mat.set_editor_property(
        "blendable_location",
        unreal.BlendableLocation.BL_SCENE_COLOR_AFTER_TONEMAPPING)
    unreal.MaterialEditingLibrary.delete_all_material_expressions(mat)
    return mat


_TMP_SKYBLACK_NAME = "M_UE5Cap_SkyBlack"


def delete_temp_sky_black_material():
    _delete_temp_material(_TMP_SKYBLACK_NAME, "SkyBlack")


def get_or_create_sky_black_material():
    """空マット用の黒スカイドーム材（Unlit/Opaque/TwoSided/is_sky=True）。
    is_sky=True で空気遠近(aerial perspective)を受けない＝純黒が保たれる
    （ue-sky-matte-capture スキルで実証済みの構成）。"""
    full = _TMP_MAT_PKG + "/" + _TMP_SKYBLACK_NAME
    mat = None
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        mat = unreal.EditorAssetLibrary.load_asset(full)
    if mat is None:
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset(_TMP_SKYBLACK_NAME, _TMP_MAT_PKG,
                              unreal.Material, unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("SkyBlack マテリアルの生成に失敗しました。")
    MEL = unreal.MaterialEditingLibrary
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
    mat.set_editor_property("two_sided", True)
    mat.set_editor_property("is_sky", True)
    MEL.delete_all_material_expressions(mat)
    c = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -300, 0)
    c.set_editor_property("r", 0.0)
    _mx_prop(c, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.recompile_material(mat)
    _log("一時 SkyBlack マテリアル生成")
    return mat


_TMP_NORMAL_NAME = "M_UE5Cap_Normal"


def delete_temp_normal_material():
    _delete_temp_material(_TMP_NORMAL_NAME, "Normal")


# カメラ法線の軸符号（ビュー空間 → 「正対=+Z青 / 右=+X赤 / 上=+Y緑」の規約合わせ）。
# UE5.7 のビュー空間は X=右 / Y=上 / Z=奥（2026-08-25 実測）なので Z のみ反転する。
_NORMAL_VIEW_FLIP = (1.0, 1.0, -1.0)


def create_temp_normal_material(camera_space=True):
    """法線を RGB に出す PostProcess マテリアルを用意して返す。
    camera_space=True で GBuffer WorldNormal をビュー空間へ変換（カメラ法線。
    正対面が青になる V-Ray SamplerInfo 等の規約）。False はワールド法線そのまま。
    いずれも -1..1 を *0.5+0.5 で 0..1 に詰める（法線マップ規約）。
    注意: MRQ 経由の PNG/MP4 は表示用エンコード（sRGB）で書かれるため、
    データとして使う場合はリニア化してから -1..1 へ戻すこと（8bit Depth と同じ制約）。"""
    mat = _mx_get_or_create_pp_material(_TMP_NORMAL_NAME)
    st = _mx_expr(mat, unreal.MaterialExpressionSceneTexture, -1050, 0)
    st.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_WORLD_NORMAL)
    m = _mx_expr(mat, unreal.MaterialExpressionComponentMask, -880, 0)
    m.set_editor_property("r", True)
    m.set_editor_property("g", True)
    m.set_editor_property("b", True)
    m.set_editor_property("a", False)
    _mx_conn(st, "Color", m, "")
    src = m
    if camera_space:
        tr = _mx_expr(mat, unreal.MaterialExpressionTransform, -730, 0)
        tr.set_editor_property(
            "transform_source_type",
            unreal.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_WORLD)
        tr.set_editor_property(
            "transform_type", unreal.MaterialVectorCoordTransform.TRANSFORM_VIEW)
        _mx_conn(m, "", tr, "")
        flip = _mx_expr(mat, unreal.MaterialExpressionConstant3Vector, -730, 220)
        flip.set_editor_property("constant", unreal.LinearColor(
            _NORMAL_VIEW_FLIP[0], _NORMAL_VIEW_FLIP[1], _NORMAL_VIEW_FLIP[2], 0.0))
        mulf = _mx_expr(mat, unreal.MaterialExpressionMultiply, -560, 0)
        _mx_conn(tr, "", mulf, "A")
        _mx_conn(flip, "", mulf, "B")
        src = mulf
    c_half = _mx_const(mat, 0.5, -560, 220)
    mul = _mx_expr(mat, unreal.MaterialExpressionMultiply, -400, 0)
    _mx_conn(src, "", mul, "A")
    _mx_conn(c_half, "", mul, "B")
    add = _mx_expr(mat, unreal.MaterialExpressionAdd, -250, 0)
    _mx_conn(mul, "", add, "A")
    _mx_conn(c_half, "", add, "B")
    # 非描画領域（空=ファープレーン）はGBuffer法線がクリア値のまま乗るので、
    # SceneDepth < 1e7 の valid 判定（Matte 材と同じしきい値）で黒に落とす
    d = _mx_expr(mat, unreal.MaterialExpressionSceneDepth, -560, 420)
    c_thresh = _mx_const(mat, 1.0e7, -560, 560)
    subv = _mx_expr(mat, unreal.MaterialExpressionSubtract, -400, 480)
    _mx_conn(c_thresh, "", subv, "A")
    _mx_conn(d, "", subv, "B")
    c_k = _mx_const(mat, 1.0e-2, -400, 620)
    mulv = _mx_expr(mat, unreal.MaterialExpressionMultiply, -250, 480)
    _mx_conn(subv, "", mulv, "A")
    _mx_conn(c_k, "", mulv, "B")
    clampv = _mx_expr(mat, unreal.MaterialExpressionClamp, -120, 480)
    _mx_conn(mulv, "", clampv, "")
    outm = _mx_expr(mat, unreal.MaterialExpressionMultiply, -60, 100)
    _mx_conn(add, "", outm, "A")
    _mx_conn(clampv, "", outm, "B")
    _mx_prop(outm, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as e:
        _warn("一時法線マテリアルの保存に失敗（未保存のまま続行）: %s" % e)
    _log("一時法線マテリアル生成 (%s*0.5+0.5)"
         % ("ViewNormal" if camera_space else "WorldNormal"))
    return mat


_TMP_GEOMASK_NAME = "M_UE5Cap_GeoMask"


def delete_temp_geomask_material():
    _delete_temp_material(_TMP_GEOMASK_NAME, "GeoMask")


def create_temp_geomask_material():
    """SceneDepth < 1e7（ジオメトリ有）の画素を白(1)にする PostProcess マテリアル。
    可視雲マットの「ジオメトリ画素は 1−T / 空画素は α」の分岐マスクに使う。"""
    mat = _mx_get_or_create_pp_material(_TMP_GEOMASK_NAME)
    d = _mx_expr(mat, unreal.MaterialExpressionSceneDepth, -700, 0)
    c_thresh = _mx_const(mat, 1.0e7, -700, 160)
    sub = _mx_expr(mat, unreal.MaterialExpressionSubtract, -550, 60)
    _mx_conn(c_thresh, "", sub, "A")
    _mx_conn(d, "", sub, "B")
    c_k = _mx_const(mat, 1.0e-2, -550, 220)
    mul = _mx_expr(mat, unreal.MaterialExpressionMultiply, -400, 100)
    _mx_conn(sub, "", mul, "A")
    _mx_conn(c_k, "", mul, "B")
    clamp = _mx_expr(mat, unreal.MaterialExpressionClamp, -250, 100)
    _mx_conn(mul, "", clamp, "")
    _mx_prop(clamp, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as e:
        _warn("一時 GeoMask マテリアルの保存に失敗（未保存のまま続行）: %s" % e)
    _log("一時 GeoMask マテリアル生成 (SceneDepth<1e7)")
    return mat


_TMP_MATTE_NAME = "M_UE5Cap_Matte"


def delete_temp_matte_material():
    _delete_temp_material(_TMP_MATTE_NAME, "Matte")


def _mx_stencil_eq(mat, value, x, y):
    """CustomStencil == value の画素で 1 になる式チェーンを組んで clamp ノードを返す。
    eq = clamp(1 - abs(stencil - value))。ステンシル未書き込み画素は 0 なので
    value>=1 なら自動的に除外される（CustomDepth の far-plane 検査は不要）。"""
    MEL = unreal.MaterialEditingLibrary
    st = _mx_scene_r(mat, unreal.SceneTextureId.PPI_CUSTOM_STENCIL, x, y)
    diff = MEL.create_material_expression(mat, unreal.MaterialExpressionSubtract, x + 250, y)
    _mx_conn(st, "", diff, "A")
    _mx_conn(_mx_const(mat, float(value), x + 100, y + 100), "", diff, "B")
    ab = MEL.create_material_expression(mat, unreal.MaterialExpressionAbs, x + 380, y)
    _mx_conn(diff, "", ab, "")
    inv = MEL.create_material_expression(mat, unreal.MaterialExpressionSubtract, x + 500, y)
    _mx_conn(_mx_const(mat, 1.0, x + 380, y + 100), "", inv, "A")
    _mx_conn(ab, "", inv, "B")
    eq = MEL.create_material_expression(mat, unreal.MaterialExpressionClamp, x + 630, y)
    _mx_conn(inv, "", eq, "")
    return eq


def create_temp_matte_material():
    """CustomDepth と SceneDepth の比較で「対象が最前面に見える画素」を出力する
    PostProcess マテリアルを用意して返す（選択=黒 / 周囲=白。画像タブの Matte と同じ）。
    対象アクターは render_in_main_pass=False + render_custom_depth=True +
    ステンシル=MATTE_STENCIL にしておく前提（ビューティには写らず CustomDepth に
    だけ写る＝クリーンプレートと両立）。対象の判定はステンシル一致で行うため、
    ObjectID 対象など他の CustomDepth 書き込みは混入しない。
    既存の一時アセットがあれば再利用して式だけ作り直す。"""
    mat = _mx_get_or_create_pp_material(_TMP_MATTE_NAME)
    MEL = unreal.MaterialEditingLibrary
    _conn = _mx_conn
    _const = lambda v, x, y: _mx_const(mat, v, x, y)
    _scene_r = lambda tex_id, x, y: _mx_scene_r(mat, tex_id, x, y)

    # black = in_front * stencil_eq,  out = 1 - black（選択=黒 / 周囲=白）
    #   in_front   = clamp((sd + tol - cd) * 1000)   対象がシーンより手前
    #   stencil_eq = CustomStencil == MATTE_STENCIL（未書き込み=0 は自動的に外れる）
    # If ノードを使わない算術のみの構成（ピン名依存を最小化）。
    cd = _scene_r(unreal.SceneTextureId.PPI_CUSTOM_DEPTH, -1250, 0)
    sd = _scene_r(unreal.SceneTextureId.PPI_SCENE_DEPTH, -1250, 250)
    mul = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -950, 250)
    _conn(sd, "", mul, "A")
    _conn(_const(0.02, -1100, 350), "", mul, "B")
    mx = MEL.create_material_expression(mat, unreal.MaterialExpressionMax, -800, 250)
    _conn(mul, "", mx, "A")
    _conn(_const(10.0, -950, 380), "", mx, "B")
    add = MEL.create_material_expression(mat, unreal.MaterialExpressionAdd, -650, 250)
    _conn(sd, "", add, "A")
    _conn(mx, "", add, "B")
    # in_front
    diff = MEL.create_material_expression(mat, unreal.MaterialExpressionSubtract, -500, 120)
    _conn(add, "", diff, "A")
    _conn(cd, "", diff, "B")
    scale = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -370, 120)
    _conn(diff, "", scale, "A")
    _conn(_const(1000.0, -500, 300), "", scale, "B")
    in_front = MEL.create_material_expression(mat, unreal.MaterialExpressionClamp, -240, 120)
    _conn(scale, "", in_front, "")
    # stencil_eq
    eq = _mx_stencil_eq(mat, MATTE_STENCIL, -1250, 480)
    # black = in_front * stencil_eq → out = 1 - black
    black = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -120, 260)
    _conn(in_front, "", black, "A")
    _conn(eq, "", black, "B")
    inv = MEL.create_material_expression(mat, unreal.MaterialExpressionOneMinus, -10, 260)
    _conn(black, "", inv, "")
    _mx_prop(inv, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as e:
        _warn("一時 Matte マテリアルの保存に失敗（未保存のまま続行）: %s" % e)
    _log("一時 Matte マテリアル生成（選択=黒/周囲=白・ステンシル判定）")
    return mat


_TMP_MATTE_SIL_NAME = "M_UE5Cap_MatteSil"


def delete_temp_matte_sil_material():
    _delete_temp_material(_TMP_MATTE_SIL_NAME, "MatteSil")


def create_temp_matte_sil_material():
    """マット対象の『全投影シルエット』を出力する PostProcess マテリアル
    （選択=黒 / 周囲=白）。Matte と違い SceneDepth と比較しない＝手前の遮蔽物を
    無視する（静止画側の show-only シルエットと同じ意味）。Behind 合成のマスク専用:
    可視性マスク（M_UE5Cap_Matte）だとマットより手前のオブジェクトの画素が
    「マットでない」になり、合成が Beauty 側（=手前オブジェクト入り）を選んでしまう。
    対象アクターは render_custom_depth=True + ステンシル=MATTE_STENCIL の前提
    （ステンシル一致判定なので ObjectID 対象の CustomDepth は混入しない）。"""
    mat = _mx_get_or_create_pp_material(_TMP_MATTE_SIL_NAME)
    MEL = unreal.MaterialEditingLibrary

    # black = stencil_eq（対象の全投影シルエット）→ out = 1 - black
    eq = _mx_stencil_eq(mat, MATTE_STENCIL, -1250, 120)
    inv = MEL.create_material_expression(mat, unreal.MaterialExpressionOneMinus, -10, 120)
    _mx_conn(eq, "", inv, "")
    _mx_prop(inv, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as e:
        _warn("一時 MatteSil マテリアルの保存に失敗（未保存のまま続行）: %s" % e)
    _log("一時 MatteSil マテリアル生成（選択=黒/周囲=白・遮蔽非依存・ステンシル判定）")
    return mat


_TMP_OBJID_NAME = "M_UE5Cap_ObjectID"


def delete_temp_objid_material():
    _delete_temp_material(_TMP_OBJID_NAME, "ObjectID")


def create_temp_objid_material():
    """CustomStencil の値をコサインパレットで色分けし、対象以外を黒にする
    PostProcess マテリアル。対象は main pass に写ったまま（CustomDepth と
    SceneDepth の一致でオクルージョンを解決）。レンダ中は r.CustomDepth=3
    （ステンシル有効）が必要（capture_mrq 側で切替・復元）。
    色は objid_stencil_color() と同式で、マニフェスト JSON と対応する。"""
    mat = _mx_get_or_create_pp_material(_TMP_OBJID_NAME)
    s = _mx_scene_r(mat, unreal.SceneTextureId.PPI_CUSTOM_STENCIL, -1500, 0)
    cd = _mx_scene_r(mat, unreal.SceneTextureId.PPI_CUSTOM_DEPTH, -1500, 220)
    sd = _mx_scene_r(mat, unreal.SceneTextureId.PPI_SCENE_DEPTH, -1500, 440)
    # occ = clamp((tol - |cd - sd|) * 1000)   対象が実際に最前面に見える画素のみ
    mul = _mx_expr(mat, unreal.MaterialExpressionMultiply, -1150, 440)
    _mx_conn(sd, "", mul, "A")
    _mx_conn(_mx_const(mat, 0.02, -1300, 540), "", mul, "B")
    mx = _mx_expr(mat, unreal.MaterialExpressionMax, -1000, 440)
    _mx_conn(mul, "", mx, "A")
    _mx_conn(_mx_const(mat, 10.0, -1150, 560), "", mx, "B")
    dsub = _mx_expr(mat, unreal.MaterialExpressionSubtract, -1150, 300)
    _mx_conn(cd, "", dsub, "A")
    _mx_conn(sd, "", dsub, "B")
    dabs = _mx_expr(mat, unreal.MaterialExpressionAbs, -1000, 300)
    _mx_conn(dsub, "", dabs, "")
    osub = _mx_expr(mat, unreal.MaterialExpressionSubtract, -850, 360)
    _mx_conn(mx, "", osub, "A")
    _mx_conn(dabs, "", osub, "B")
    oscale = _mx_expr(mat, unreal.MaterialExpressionMultiply, -700, 360)
    _mx_conn(osub, "", oscale, "A")
    _mx_conn(_mx_const(mat, 1000.0, -850, 480), "", oscale, "B")
    occ = _mx_expr(mat, unreal.MaterialExpressionClamp, -560, 360)
    _mx_conn(oscale, "", occ, "")
    # smask = clamp((s - 0.5) * 2)   ステンシル未書き込み(0)は黒
    ssub = _mx_expr(mat, unreal.MaterialExpressionSubtract, -1150, 80)
    _mx_conn(s, "", ssub, "A")
    _mx_conn(_mx_const(mat, 0.5, -1300, 160), "", ssub, "B")
    sscale = _mx_expr(mat, unreal.MaterialExpressionMultiply, -1000, 80)
    _mx_conn(ssub, "", sscale, "A")
    _mx_conn(_mx_const(mat, 2.0, -1150, 180), "", sscale, "B")
    smask = _mx_expr(mat, unreal.MaterialExpressionClamp, -860, 80)
    _mx_conn(sscale, "", smask, "")
    # コサインパレット: h = frac(s * 黄金比), ch = 0.5 + 0.5*cos(h + offset)
    hmul = _mx_expr(mat, unreal.MaterialExpressionMultiply, -1150, -60)
    _mx_conn(s, "", hmul, "A")
    _mx_conn(_mx_const(mat, 0.6180339887, -1300, -20), "", hmul, "B")
    h = _mx_expr(mat, unreal.MaterialExpressionFrac, -1000, -60)
    _mx_conn(hmul, "", h, "")
    chans = []
    for i, off in enumerate((0.0, -1.0 / 3.0, -2.0 / 3.0)):
        y = -220 + i * 90
        src = h
        if off != 0.0:
            adder = _mx_expr(mat, unreal.MaterialExpressionAdd, -860, y)
            _mx_conn(h, "", adder, "A")
            _mx_conn(_mx_const(mat, off, -1000, y + 40), "", adder, "B")
            src = adder
        cosn = _mx_expr(mat, unreal.MaterialExpressionCosine, -700, y)
        cosn.set_editor_property("period", 1.0)
        _mx_conn(src, "", cosn, "")
        halfm = _mx_expr(mat, unreal.MaterialExpressionMultiply, -560, y)
        _mx_conn(cosn, "", halfm, "A")
        _mx_conn(_mx_const(mat, 0.5, -700, y + 40), "", halfm, "B")
        halfa = _mx_expr(mat, unreal.MaterialExpressionAdd, -430, y)
        _mx_conn(halfm, "", halfa, "A")
        _mx_conn(_mx_const(mat, 0.5, -560, y + 40), "", halfa, "B")
        chans.append(halfa)
    ap1 = _mx_expr(mat, unreal.MaterialExpressionAppendVector, -300, -180)
    _mx_conn(chans[0], "", ap1, "A")
    _mx_conn(chans[1], "", ap1, "B")
    ap2 = _mx_expr(mat, unreal.MaterialExpressionAppendVector, -180, -160)
    _mx_conn(ap1, "", ap2, "A")
    _mx_conn(chans[2], "", ap2, "B")
    gate = _mx_expr(mat, unreal.MaterialExpressionMultiply, -400, 200)
    _mx_conn(occ, "", gate, "A")
    _mx_conn(smask, "", gate, "B")
    # マット予約ステンシル（MATTE_STENCIL）は ObjectID として着色しない
    # （マットレンダモードの板が ObjectID 映像に色付きで写り込むため）
    not_matte = _mx_expr(mat, unreal.MaterialExpressionOneMinus, -400, 620)
    _mx_conn(_mx_stencil_eq(mat, MATTE_STENCIL, -1500, 660), "", not_matte, "")
    gate2 = _mx_expr(mat, unreal.MaterialExpressionMultiply, -280, 200)
    _mx_conn(gate, "", gate2, "A")
    _mx_conn(not_matte, "", gate2, "B")
    outm = _mx_expr(mat, unreal.MaterialExpressionMultiply, -60, 40)
    _mx_conn(ap2, "", outm, "A")
    _mx_conn(gate2, "", outm, "B")
    _mx_prop(outm, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
    except Exception as e:
        _warn("一時 ObjectID マテリアルの保存に失敗（未保存のまま続行）: %s" % e)
    _log("一時 ObjectID マテリアル生成（ステンシル色分け・他は黒）")
    return mat


# ----------------------------------------------------------------------------
# エントリポイント
# ----------------------------------------------------------------------------
def run_capture(settings):
    """設定に従ってキャプチャを実行。生成オブジェクトは finally で必ず破棄。
    返り値: 出力ファイルパスのリスト。"""
    if not _HAS_NUMPY or not _HAS_PIL:
        _err("numpy と Pillow が必要です。README の手順で UE の Python に pip install してください。")
        return []

    problems = settings.validate()
    if problems:
        for p in problems:
            _warn(p)
        return []

    world = _get_editor_world()
    if world is None:
        _err("エディタワールドを取得できませんでした。")
        return []

    cam = get_camera_settings(settings.camera_actor)
    w, h = _resolve_resolution(settings, cam)
    # Overscan はカメラの filmback を呼び側で一時拡大して実現するため、ここでは何もしない
    # （get_camera_settings が拡大後の FOV を読み、RT のアスペクトで縦横が決まる）。
    ts = settings.take_suffix or _timestamp()
    spawned = []
    outputs = []

    passes = [
        (settings.do_color, "Color", _capture_color),
        (settings.do_depth, "Depth", _capture_depth),
        (settings.do_matte, "Matte", _capture_matte),
        (settings.do_object_id, "Object ID", _capture_object_id),
        (settings.do_behind_matte, "behind-matte", capture_behind_matte),
    ]
    _log("キャプチャ開始: %s  %dx%d (AA x%d)" %
         (settings.camera_actor.get_actor_label(), w, h, settings.aa_factor))
    try:
        for enabled, name, fn in passes:
            if not enabled:
                continue
            try:
                r = fn(world, settings, cam, w, h, ts, spawned)
                if r:
                    outputs.extend(r if isinstance(r, (list, tuple)) else [r])
            except Exception as e:
                _err("%s パス失敗: %s" % (name, e))
    finally:
        _destroy_actors(spawned)
        unreal.SystemLibrary.collect_garbage()

    outputs = [o for o in outputs if o]
    _log("キャプチャ完了。出力 %d 件。" % len(outputs))
    return outputs
