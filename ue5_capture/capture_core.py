# -*- coding: utf-8 -*-
"""
capture_core.py  --  UE5.7 Scene Capture core logic

SceneCaptureComponent2D + TextureRenderTarget2D をランタイムで Spawn / 生成し、
Color / Z-Depth / Matte Alpha の各パスをキャプチャしてディスクへ書き出す。
すべて Transient パッケージ上で生成し、キャプチャ後（例外時含む）に確実に破棄する。
.uasset の保存は一切行わない。

外部依存（仕様書 ue5_capture_tool_prompt.md の「外部依存」節を参照）:
  - numpy   : 必須（RenderTarget 画素の後処理。AA ダウンスケール / Depth 正規化 / Matte 閾値）
  - Pillow  : 必須（PNG / 16bit PNG 書き出し、PNG 読み込み）
  - OpenEXR + Imath か imageio : 任意（Depth 16bit を EXR で書き出す場合のみ。無ければ 16bit PNG にフォールバック）

UE の RenderTarget は全画素を Python から高速に読み取る API が無いため、
一旦 export_render_target() で一時ファイルへ書き出してから numpy/Pillow で後処理する。
"""

import os
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


# ----------------------------------------------------------------------------
# 設定オブジェクト
# ----------------------------------------------------------------------------
class CaptureSettings(object):
    """キャプチャ1回分の全パラメータ。"""

    def __init__(self):
        self.camera_actor = None          # unreal.CameraActor / CineCameraActor
        self.use_camera_resolution = True  # True: カメラのアスペクト維持 / False: override
        self.override_width = 3840
        self.override_height = 2160
        self.base_height = 1080            # use_camera_resolution 時の基準高さ
        self.aa_factor = 2                 # 1 / 2 / 4 （Spatial Supersample 倍率）
        self.output_dir = ""               # 出力フォルダ

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
# エディタ / アクター取得ヘルパ（UE5.7 サブシステム API）
# ----------------------------------------------------------------------------
def _get_editor_world():
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return ues.get_editor_world()


def _actor_subsystem():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def list_cameras():
    """シーン内の CameraActor / CineCameraActor を返す。"""
    actors = _actor_subsystem().get_all_level_actors()
    cams = []
    for a in actors:
        if isinstance(a, (unreal.CameraActor, unreal.CineCameraActor)):
            cams.append(a)
    return cams


def get_selected_actors():
    return list(_actor_subsystem().get_selected_level_actors())


def _camera_component(camera_actor):
    # ACameraActor / ACineCameraActor とも camera_component プロパティを持つ
    return camera_actor.camera_component


def get_camera_settings(camera_actor):
    """カメラの Transform / FOV / アスペクト / PostProcess を取得。"""
    cam_comp = _camera_component(camera_actor)
    return {
        "transform": camera_actor.get_actor_transform(),
        "fov": float(cam_comp.get_editor_property("field_of_view")),
        "aspect_ratio": float(cam_comp.get_editor_property("aspect_ratio")),
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


def _apply_game_show_flags(comp):
    """キャプチャコンポーネントのエディタ専用表示を OFF にする（ゲーム表示相当）。"""
    try:
        settings = []
        for name in _EDITOR_ONLY_SHOW_FLAGS:
            try:
                fs = unreal.EngineShowFlagsSetting()
                fs.set_editor_property("show_flag_name", name)
                fs.set_editor_property("enabled", False)
                settings.append(fs)
            except Exception:
                pass
        if settings:
            comp.set_editor_property("show_flag_settings", settings)
    except Exception as e:
        _warn("ShowFlags(ゲーム表示) 設定に失敗: %s" % e)


def _spawn_capture(world, transform, fov, rt, source,
                   show_only_actors=None, hidden_actors=None, post_process=None,
                   clip_base=None, clip_normal=None):
    """SceneCapture2D アクターを Spawn して1フレームキャプチャし、アクターを返す。
    clip_base/clip_normal を渡すと、その平面の normal-負側（手前）を描画時にクリップする。"""
    loc = transform.translation
    rot = transform.rotation.rotator()
    actor = _actor_subsystem().spawn_actor_from_class(unreal.SceneCapture2D, loc, rot)
    if actor is None:
        raise RuntimeError("SceneCapture2D の Spawn に失敗しました。")

    comp = actor.capture_component2d
    comp.set_editor_property("fov_angle", float(fov))
    comp.set_editor_property("texture_target", rt)
    comp.set_editor_property("capture_source", source)
    comp.set_editor_property("capture_every_frame", False)
    comp.set_editor_property("capture_on_movement", False)
    _apply_game_show_flags(comp)   # エディタ専用表示（アイコン/ギズモ/グリッド等）を消す

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
    return actor


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
    fname = "rt_tmp_%d%s" % (id(rt), ext)
    unreal.RenderingLibrary.export_render_target(world, rt, tdir, fname)
    path = os.path.join(tdir, fname)
    if not os.path.isfile(path):
        raise RuntimeError("RenderTarget のエクスポートに失敗しました: %s" % path)
    return path


def _read_ldr(path):
    """PNG など LDR 画像を numpy uint8 (H,W,C) で読む。"""
    img = _PILImage.open(path)
    return _np.asarray(img)


def _read_float(path):
    """.hdr / .exr を numpy float32 (H,W,C) で読む。任意ライブラリを順に試す。"""
    ext = os.path.splitext(path)[1].lower()
    # imageio（freeimage プラグインで hdr/exr 対応）
    try:
        import imageio
        arr = imageio.imread(path)
        return _np.asarray(arr, dtype=_np.float32)
    except Exception:
        pass
    # OpenCV（.hdr/.exr を float で読める。UE 同梱の cv2 を利用。BGR→RGB に並べ替え）
    try:
        import cv2
        arr = cv2.imread(path, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
        if arr is not None:
            arr = _np.asarray(arr, dtype=_np.float32)
            if arr.ndim == 3 and arr.shape[2] >= 3:
                arr = arr[:, :, ::-1]  # BGR -> RGB（R=depth を index 0 に）
            return arr
    except Exception:
        pass
    # OpenEXR（.exr のみ）
    if ext == ".exr":
        try:
            import OpenEXR
            import Imath
            f = OpenEXR.InputFile(path)
            dw = f.header()["dataWindow"]
            w = dw.max.x - dw.min.x + 1
            h = dw.max.y - dw.min.y + 1
            pt = Imath.PixelType(Imath.PixelType.FLOAT)
            chans = f.header()["channels"].keys()
            ch = "R" if "R" in chans else list(chans)[0]
            raw = f.channel(ch, pt)
            return _np.frombuffer(raw, dtype=_np.float32).reshape(h, w, 1).copy()
        except Exception:
            pass
    raise RuntimeError(
        "float 画像 (%s) を読めるライブラリ（imageio または OpenEXR）が見つかりません。" % ext)


# ----------------------------------------------------------------------------
# RenderTarget 直接読み出し（UE5.7: .hdr 経由不要。read_render_target_raw を使う）
#   - R32F へ SceneDepth を書くと UE5.7 では壊れる（全画素一定値）。
#     深度は RGBA16F に撮り、ここで R チャンネル（cm）を取り出す。
# ----------------------------------------------------------------------------
def _read_rt_raw_r(world, rt, width, height):
    """RenderTarget の R チャンネルを生値のまま numpy float32 (H,W) で返す。"""
    samples = unreal.RenderingLibrary.read_render_target_raw(world, rt, False)
    n = width * height
    a = _np.fromiter((c.r for c in samples), dtype=_np.float32, count=min(n, len(samples)))
    if a.size < n:
        a = _np.resize(a, n)
    return a.reshape(height, width)


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
    actor = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                           unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR,
                           post_process=_manual_exposure_pp(bias))
    RL = unreal.RenderingLibrary
    vals = []
    for yy in range(4, ph, 8):
        for xx in range(4, pw, 8):
            px = RL.read_render_target_pixel(world, rt, xx, yy)
            vals.append((px.r + px.g + px.b) / 3.0)
    _destroy_actors([actor])
    vals.sort()
    return vals[len(vals) // 2] if vals else 0.0


def _resolve_exposure_bias(world, settings, cam):
    """反復プローブで目標 median 輝度に合う EV を求めて返す（auto 用）。
    トーンマッパーは非線形なので、測定→補正→再測定を数回まわして収束させる。"""
    import math
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
    _PILImage.fromarray(a, "I;16").save(path)


def _write_exr_gray(path, arr):
    """単一チャンネル float を EXR で書く。失敗時 False。"""
    try:
        import imageio
        imageio.imwrite(path, arr.astype(_np.float32))
        return True
    except Exception:
        pass
    try:
        import OpenEXR
        import Imath
        h, w = arr.shape[0], arr.shape[1]
        hdr = OpenEXR.Header(w, h)
        half = Imath.Channel(Imath.PixelType(Imath.PixelType.HALF))
        hdr["channels"] = {"R": half, "G": half, "B": half}
        out = OpenEXR.OutputFile(path, hdr)
        data = arr.astype(_np.float16).tobytes()
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


def _out_path(output_dir, cam_name, ts, pass_type, ext):
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in cam_name)
    return os.path.join(output_dir, "%s_%s_%s%s" % (safe, ts, pass_type, ext))


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
    mode = getattr(settings, "exposure_mode", "auto")
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
    pp = _resolve_color_pp(world, settings, cam)
    rt = _make_render_target(world, w * aa, h * aa,
                             unreal.TextureRenderTargetFormat.RTF_RGBA8, False)
    actor = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                           unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR, post_process=pp)
    spawned.append(actor)
    arr = _read_ldr(_export_rt(world, rt, ".png"))
    arr = _downscale(arr, aa)
    return arr[:, :, :3] if (arr.ndim == 3 and arr.shape[2] >= 3) else arr


def _capture_color(world, settings, cam, w, h, ts, spawned):
    aa = settings.aa_factor
    pp = _resolve_color_pp(world, settings, cam)
    # color_hide_matte: Matte 対象を非表示にして撮る（クリーンプレート）。背景が代わりに写る。
    hidden = None
    if getattr(settings, "color_hide_matte", False):
        hidden = _resolve_target_actors(settings.matte_actors, settings.matte_actor_names)
        if hidden:
            _log("Color: Matte 対象 %d 個を非表示にして撮影（クリーンプレート）" % len(hidden))
        else:
            _warn("color_hide_matte 指定ですが Matte 対象が見つかりません。通常の Color を撮ります。")
    rt = _make_render_target(world, w * aa, h * aa,
                             unreal.TextureRenderTargetFormat.RTF_RGBA8, False)
    actor = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                           unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR,
                           hidden_actors=hidden, post_process=pp)
    spawned.append(actor)
    tmp = _export_rt(world, rt, ".png")
    arr = _read_ldr(tmp)
    arr = _downscale(arr, aa)
    # FinalColor のアルファは 0 になりがちで、そのままだと透明 PNG（ビューアで白）になる。
    # Color パスは不透明画像として出すためアルファを 255 に固定する。
    if arr.ndim == 3 and arr.shape[2] == 4:
        arr[:, :, 3] = 255.0
    out = _out_path(settings.output_dir, settings.camera_actor.get_actor_label(),
                    ts, "color", ".png")
    _write_png_u8(out, arr)
    _log("Color 出力: %s" % out)
    return out


def _capture_depth(world, settings, cam, w, h, ts, spawned):
    aa = settings.aa_factor
    # depth_hide_matte: マット対象を深度から除外（マットの位置は奥の深度になる）
    hidden = None
    if getattr(settings, "depth_hide_matte", False):
        hidden = _resolve_target_actors(settings.matte_actors, settings.matte_actor_names)
        if hidden:
            _log("Depth: Matte 対象 %d 個を深度から除外" % len(hidden))
    # UE5.7: SceneDepth を R32F へ撮ると全画素一定値になる不具合あり。RGBA16F を使う。
    rt = _make_render_target(world, w * aa, h * aa,
                             unreal.TextureRenderTargetFormat.RTF_RGBA16F, True)
    actor = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                           unreal.SceneCaptureSource.SCS_SCENE_DEPTH,
                           hidden_actors=hidden)
    spawned.append(actor)
    # .hdr 経由ではなく RenderTarget を直接読み出す（R チャンネル＝cm 距離）
    depth = _read_rt_raw_r(world, rt, w * aa, h * aa)
    depth = _downscale(depth, aa)  # cm 単位の距離（空/未ヒットは half 最大 ~65504）

    near, far = settings.depth_near, settings.depth_far

    def _normalized():
        """Near/Far で 0..1 に正規化。depth_invert なら手前=1(白)/奥=0(黒)。"""
        n = _np.clip((depth - near) / max(far - near, 1e-6), 0.0, 1.0)
        if getattr(settings, "depth_invert", True):
            n = 1.0 - n
        return n

    label = settings.camera_actor.get_actor_label()
    bit = (settings.depth_bit or "16bit").lower()

    if bit in ("8bit", "8", "8bit_png", "8bitpng", "png8"):
        out = _out_path(settings.output_dir, label, ts, "depth", ".png")
        _write_png_u8(out, _normalized() * 255.0)
        _log("Depth(8bit PNG%s) 出力: %s" % (" 反転" if settings.depth_invert else "", out))
        return out

    if bit in ("16bit", "16", "16bit_png", "16bitpng", "png16"):
        out = _out_path(settings.output_dir, label, ts, "depth", ".png")
        _write_png_u16_gray(out, _normalized())
        _log("Depth(16bit PNG%s) 出力: %s" % (" 反転" if settings.depth_invert else "", out))
        return out

    # EXR（float リニア距離 cm をそのまま。データ用途のため反転/正規化はしない）
    out = _out_path(settings.output_dir, label, ts, "depth", ".exr")
    if _write_exr_gray(out, depth):
        _log("Depth(EXR float, 生cm) 出力: %s" % out)
        return out
    # EXR ライブラリが無い → 16bit PNG（Near/Far 正規化）にフォールバック
    out = _out_path(settings.output_dir, label, ts, "depth", ".png")
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
        rtf = unreal.TextureRenderTargetFormat.RTF_RGBA16F
        src = unreal.SceneCaptureSource.SCS_SCENE_DEPTH
        # 1) 全シーン深度（手前の遮蔽物を判定するため）
        rt_full = _make_render_target(world, w * aa, h * aa, rtf, True)
        a_full = _spawn_capture(world, cam["transform"], cam["fov"], rt_full, src)
        spawned.append(a_full)
        full = _read_rt_raw_r(world, rt_full, w * aa, h * aa)
        # 2) 対象アクターのみの深度（show-only）
        rt = _make_render_target(world, w * aa, h * aa, rtf, True)
        actor = _spawn_capture(world, cam["transform"], cam["fov"], rt, src,
                               show_only_actors=actors)
        spawned.append(actor)
        grp = _read_rt_raw_r(world, rt, w * aa, h * aa)
        # 対象が「実際に最前面に見えている」画素だけマット化する。
        # 手前に非対象物がある所は full < grp となり tol を超えて除外される（オクルージョン考慮）。
        tol = _np.maximum(full * 0.02, 10.0)
        vis = (grp < 60000.0) & (_np.abs(grp - full) <= tol)
        sel = vis.astype(_np.float32)
        if getattr(settings, "matte_invert", False):
            sel = 1.0 - sel          # 選択=黒 / 周囲=白
        mask = sel * 255.0
        mask = _downscale(mask, aa)  # 縁が AA される
        label = settings.camera_actor.get_actor_label()
        outs = []
        out = _out_path(settings.output_dir, label, ts, "matte", ".png")
        _write_png_u8(out, mask)
        _log("Matte(白黒) 出力: %s" % out)
        outs.append(out)
        # Fill（レンダリング画像）に matte をアルファとして組み込んだ RGBA も出力
        if getattr(settings, "matte_fill_alpha", False):
            rgb = _render_fill_rgb(world, settings, cam, w, h, aa, spawned)
            rgba = _np.dstack([rgb, mask[:, :, None]]) if mask.ndim == 2 else _np.dstack([rgb, mask])
            out_fill = _out_path(settings.output_dir, label, ts, "matte_fill", ".png")
            _write_png_u8(out_fill, rgba)
            _log("Matte(Fill+α) 出力: %s" % out_fill)
            outs.append(out_fill)
        return outs
    finally:
        _restore_custom_depth(saved)


def _capture_object_id(world, settings, cam, w, h, ts, spawned):
    """選択/指定アクターを色分けした Object ID 1枚 + 色→名前の対応 JSON を出力。
    オクルージョンは全シーン深度と各アクター深度の一致で正しく解決する。"""
    import colorsys, json
    actors = _resolve_target_actors(settings.objid_actors, settings.objid_actor_names)
    if not actors:
        _warn("Object ID 対象アクターがありません（選択 or 名前リストを指定）。スキップします。")
        return None

    rtf = unreal.TextureRenderTargetFormat.RTF_RGBA16F
    src = unreal.SceneCaptureSource.SCS_SCENE_DEPTH
    # 1) 全シーン深度（オクルージョン解決用）
    rt_full = _make_render_target(world, w, h, rtf, True)
    a_full = _spawn_capture(world, cam["transform"], cam["fov"], rt_full, src)
    spawned.append(a_full)
    full = _read_rt_raw_r(world, rt_full, w, h)

    id_rgb = _np.zeros((h, w, 3), dtype=_np.float32)
    coverage = _np.zeros((h, w), dtype=bool)   # 対象全体の可視カバレッジ（アルファ用）
    manifest = {}
    FAR = 60000.0
    idx = 0
    for act in actors:
        try:
            label = act.get_actor_label()
        except Exception:
            continue
        rt = _make_render_target(world, w, h, rtf, True)
        cap = _spawn_capture(world, cam["transform"], cam["fov"], rt, src,
                             show_only_actors=[act])
        spawned.append(cap)
        ad = _read_rt_raw_r(world, rt, w, h)
        tol = _np.maximum(full * 0.02, 10.0)               # 深度一致の許容（cm）
        vis = (ad < FAR) & (_np.abs(ad - full) <= tol)     # そのアクターが最前面に見える画素
        if not bool(vis.any()):
            continue
        coverage |= vis
        # 黄金角でホールド相を回し、確実に色が離れるようにする（彩度/明度も少し振る）
        hue = (idx * 0.6180339887498949) % 1.0
        sat = 0.75 + 0.20 * ((idx // 6) % 2)
        val = 1.0 - 0.20 * ((idx // 3) % 2)
        idx += 1
        r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
        col = _np.array([r, g, b], dtype=_np.float32) * 255.0
        id_rgb[vis] = col
        manifest["#%02X%02X%02X" % (int(col[0]), int(col[1]), int(col[2]))] = label

    label0 = settings.camera_actor.get_actor_label()
    outs = []
    out = _out_path(settings.output_dir, label0, ts, "objectid", ".png")
    _write_png_u8(out, id_rgb)
    try:
        with open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _warn("Object ID マニフェスト書き出しに失敗: %s" % e)
    _log("Object ID 出力: %s (%d obj)" % (out, len(manifest)))
    outs.append(out)

    aa = settings.aa_factor
    # Fill + Object ID カバレッジ(アルファ) の RGBA
    if getattr(settings, "objid_fill_alpha", False):
        rgb = _render_fill_rgb(world, settings, cam, w, h, 1, spawned)  # objid は等倍運用
        alpha = (coverage.astype(_np.float32) * 255.0)[:, :, None]
        rgba = _np.dstack([rgb, alpha])
        out_fa = _out_path(settings.output_dir, label0, ts, "objectid_fill", ".png")
        _write_png_u8(out_fa, rgba)
        _log("Object ID(Fill+α) 出力: %s" % out_fa)
        outs.append(out_fa)
    # Object ID 対象を非表示にして Fill をレンダリング（クリーンプレート）
    if getattr(settings, "objid_hide_render", False):
        pp = _resolve_color_pp(world, settings, cam)
        rt_h = _make_render_target(world, w * aa, h * aa,
                                   unreal.TextureRenderTargetFormat.RTF_RGBA8, False)
        a_h = _spawn_capture(world, cam["transform"], cam["fov"], rt_h,
                             unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR,
                             hidden_actors=actors, post_process=pp)
        spawned.append(a_h)
        arr = _read_ldr(_export_rt(world, rt_h, ".png"))
        arr = _downscale(arr, aa)
        if arr.ndim == 3 and arr.shape[2] == 4:
            arr[:, :, 3] = 255
        out_hd = _out_path(settings.output_dir, label0, ts, "objectid_hidden", ".png")
        _write_png_u8(out_hd, arr)
        _log("Object ID 非表示レンダー 出力: %s" % out_hd)
        outs.append(out_hd)
    return outs


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
            try:
                comp.set_editor_property("cast_shadow", bool(enabled))
            except Exception:
                pass
            for prop in ("affect_distance_field_lighting",
                         "affect_dynamic_indirect_lighting"):
                try:
                    comp.set_editor_property(prop, bool(enabled))
                except Exception:
                    pass
            n += 1
    _log("Matte 影/オクルージョン = %s（%d component）" % ("ON" if enabled else "OFF", n))
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
    rt = _make_render_target(world, width, height,
                             unreal.TextureRenderTargetFormat.RTF_RGBA16F, True)
    a = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                       unreal.SceneCaptureSource.SCS_SCENE_DEPTH, show_only_actors=matte_actors)
    md = _read_rt_raw_r(world, rt, width, height)
    _destroy_actors([a])
    mask = (md < 60000.0).astype(_np.float32)
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


def matte_near_clip_cm(actors, cam):
    """カメラからマット代表点までの『視線方向』距離(cm)を返す。MRQ の r.SetNearClipPlane 用
    （fronto-parallel 近似の behind-matte）。マット面がほぼ正対している前提。"""
    cam_loc = cam["transform"].translation
    fwd = cam["transform"].rotation.rotator().get_forward_vector()
    a = min(actors, key=lambda x: (x.get_actor_location() - cam_loc).length())
    base = a.get_actor_location()
    to = unreal.Vector(base.x - cam_loc.x, base.y - cam_loc.y, base.z - cam_loc.z)
    dist = to.x * fwd.x + to.y * fwd.y + to.z * fwd.z
    return max(1.0, float(dist))


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
    label = settings.camera_actor.get_actor_label()
    outs = []

    # 1) マットのシルエット（形状・per-pixel）を show-only 深度で取得
    rtf = unreal.TextureRenderTargetFormat.RTF_RGBA16F
    rt_m = _make_render_target(world, w * aa, h * aa, rtf, True)
    a_m = _spawn_capture(world, cam["transform"], cam["fov"], rt_m,
                         unreal.SceneCaptureSource.SCS_SCENE_DEPTH, show_only_actors=actors)
    spawned.append(a_m)
    matte_d = _read_rt_raw_r(world, rt_m, w * aa, h * aa)
    matte_vis = (matte_d < 60000.0).astype(_np.float32)

    # 2) マット面からクリッププレーンを導出
    base, normal = _matte_clip_plane(actors, cam)
    _log("behind-matte clip: base=(%.0f,%.0f,%.0f) normal=(%.2f,%.2f,%.2f)"
         % (base.x, base.y, base.z, normal.x, normal.y, normal.z))

    # 3) クリップ有効・マット自身は隠して Beauty(Fill) を撮る → 手前が消え奥が出る
    pp = _resolve_color_pp(world, settings, cam)
    rt_c = _make_render_target(world, w * aa, h * aa,
                               unreal.TextureRenderTargetFormat.RTF_RGBA8, False)
    a_c = _spawn_capture(world, cam["transform"], cam["fov"], rt_c,
                         unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR,
                         hidden_actors=actors, post_process=pp,
                         clip_base=base, clip_normal=normal)
    spawned.append(a_c)
    rgb = _read_ldr(_export_rt(world, rt_c, ".png"))
    rgb = _downscale(rgb, aa)
    rgb = rgb[:, :, :3] if (rgb.ndim == 3 and rgb.shape[2] >= 3) else rgb

    # 4a) 全画面クリップ版（マット面より手前は全部消える）
    out_full = _out_path(settings.output_dir, label, ts, "behindmatte_full", ".png")
    _write_png_u8(out_full, rgb)
    _log("behind-matte(全画面クリップ) 出力: %s" % out_full)
    outs.append(out_full)

    # 4b) マット形状に切り抜いた RGBA（α=マットシルエット）
    alpha = _downscale(matte_vis * 255.0, aa)
    rgba = _np.dstack([rgb, alpha[:, :, None]]) if alpha.ndim == 2 else _np.dstack([rgb, alpha])
    out = _out_path(settings.output_dir, label, ts, "behindmatte", ".png")
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
    rtf = unreal.TextureRenderTargetFormat.RTF_RGBA16F
    rt = _make_render_target(world, w * aa, h * aa, rtf, True)
    a = _spawn_capture(world, cam["transform"], cam["fov"], rt,
                       unreal.SceneCaptureSource.SCS_SCENE_DEPTH, show_only_actors=actors)
    spawned.append(a)
    grp = _read_rt_raw_r(world, rt, w * aa, h * aa)
    mask = (grp < 60000.0).astype(_np.float32) * 255.0     # 全シルエット（手前遮蔽は無視）
    mask = _downscale(mask, aa)
    out = _out_path(settings.output_dir, settings.camera_actor.get_actor_label(),
                    ts, "behindmatte_mask", ".png")
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


def blend_with_beauty(beauty_path, matte_path=None, objid_path=None):
    """MRQ Beauty(レンダ画像) の RGB に、matte/objid のカバレッジをアルファとして合成し
    RGBA cutout を書き出す（Fill ではなく Beauty とブレンドする版）。出力パスのリストを返す。"""
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
        out = matte_path.replace("_matte.png", "_matte_fill.png")
        _write_png_u8(out, _np.dstack([brgb, m]))
        _log("Matte(Beauty+α) 出力: %s" % out)
        outs.append(out)
    if objid_path and os.path.isfile(objid_path):
        idimg = _np.asarray(_PILImage.open(objid_path).convert("RGB"), dtype=_np.float32)
        cov = _fit((idimg.max(axis=2) > 1.0).astype(_np.float32) * 255.0)
        out = objid_path.replace("_objectid.png", "_objectid_fill.png")
        _write_png_u8(out, _np.dstack([brgb, cov]))
        _log("Object ID(Beauty+α) 出力: %s" % out)
        outs.append(out)
    return outs


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
    ts = _timestamp()
    spawned = []
    outputs = []

    _log("キャプチャ開始: %s  %dx%d (AA x%d)" %
         (settings.camera_actor.get_actor_label(), w, h, settings.aa_factor))
    try:
        if settings.do_color:
            try:
                outputs.append(_capture_color(world, settings, cam, w, h, ts, spawned))
            except Exception as e:
                _err("Color パス失敗: %s" % e)
        if settings.do_depth:
            try:
                outputs.append(_capture_depth(world, settings, cam, w, h, ts, spawned))
            except Exception as e:
                _err("Depth パス失敗: %s" % e)
        if settings.do_matte:
            try:
                r = _capture_matte(world, settings, cam, w, h, ts, spawned)
                if r:
                    outputs.extend(r if isinstance(r, (list, tuple)) else [r])
            except Exception as e:
                _err("Matte パス失敗: %s" % e)
        if getattr(settings, "do_object_id", False):
            try:
                r = _capture_object_id(world, settings, cam, w, h, ts, spawned)
                if r:
                    outputs.extend(r if isinstance(r, (list, tuple)) else [r])
            except Exception as e:
                _err("Object ID パス失敗: %s" % e)
        if getattr(settings, "do_behind_matte", False):
            try:
                r = capture_behind_matte(world, settings, cam, w, h, ts, spawned)
                if r:
                    outputs.extend(r if isinstance(r, (list, tuple)) else [r])
            except Exception as e:
                _err("behind-matte パス失敗: %s" % e)
    finally:
        _destroy_actors(spawned)
        unreal.SystemLibrary.collect_garbage()

    outputs = [o for o in outputs if o]
    _log("キャプチャ完了。出力 %d 件。" % len(outputs))
    return outputs
