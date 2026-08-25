# -*- coding: utf-8 -*-
"""
capture_mrq.py  --  Movie Render Queue による Beauty(Fill) 高品質レンダリング

SceneCapture2D では CineCamera の物理露出やシーケンサ相当の品質（影/GI/TSR/
ウォームアップ）を再現できないため、Beauty パスだけは MRQ で「対象カメラを通して」
レンダリングする。これによりビューポート/シーケンサ書き出しと同じ露出・品質になる。

仕組み:
  1) 対象 CineCamera を 1 フレームのカメラカットにした一時 LevelSequence を生成
  2) MRQ ジョブ（現在のマップ + その一時シーケンス）を構築
  3) Deferred(Beauty) + PNG/EXR 出力 + AA(temporal) + ウォームアップ + 高品質影 を設定
  4) MoviePipelinePIEExecutor で非同期レンダ（PIE 経由＝フル品質）
  5) 終了後に一時シーケンスを削除

非同期のため render_beauty() は即戻り、完了は on_done コールバック / 出力ファイル監視で判定する。
"""

import os
import unreal

from capture_core import (MATTE_STENCIL, _HV_COMP_CLASS, _is_hv_comp,
                          is_volumetric_actor)

_TAG = "[SceneCapture/MRQ] "
def _log(m): unreal.log(_TAG + str(m))
def _warn(m): unreal.log_warning(_TAG + str(m))
def _err(m): unreal.log_error(_TAG + str(m))

_TMP_PKG = "/Game/_UE5Capture_Tmp"
_TMP_NAME = "MRQ_TempSeq"

# GC 防止（executor / queue を保持）
_KEEP = {}


def _editor_world():
    return unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()


def _current_map_softpath():
    """現在の永続レベルのソフトパス文字列（job.map 用）。"""
    return _editor_world().get_path_name()


def _delete_temp_sequence():
    full = _TMP_PKG + "/" + _TMP_NAME
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(full):
            unreal.EditorAssetLibrary.delete_asset(full)
    except Exception as e:
        _warn("一時シーケンス削除に失敗: %s" % e)


def _create_temp_sequence(camera_actor, fps=24, scene_sequence=None, scene_frame=None):
    """対象カメラを 1 フレームのカメラカットにした一時 LevelSequence を作成して返す。

    scene_sequence / scene_frame を渡すと、そのシーケンスをサブシーケンスとして
    埋め込み「フレーム scene_frame の評価」で固定する。PIE はエディタの
    シーケンサー評価ポーズを引き継がず、possessable がスポーン時のベースライン
    （=フレーム0 の姿勢）へ戻るため、埋め込みで PIE 側にも同フレームを評価させる
    （2026-07-13 実測: マスク=現在フレーム / Beauty=フレーム0 のズレ）。
    カメラカットはこの一時シーケンス（ルート）側のみ＝ツールで選んだカメラが使われる
    （ルートのカメラカットはサブシーケンス内のカメラカットより優先される）。"""
    ext = unreal.MovieSceneSequenceExtensions
    _delete_temp_sequence()
    at = unreal.AssetToolsHelpers.get_asset_tools()
    seq = at.create_asset(_TMP_NAME, _TMP_PKG, unreal.LevelSequence,
                          unreal.LevelSequenceFactoryNew())
    if seq is None:
        raise RuntimeError("一時 LevelSequence の生成に失敗しました。")
    if scene_sequence is not None and scene_frame is not None:
        # レート差でフレーム写像が狂わないよう表示レートは元シーケンスに合わせる
        ext.set_display_rate(seq, ext.get_display_rate(scene_sequence))
    else:
        ext.set_display_rate(seq, unreal.FrameRate(int(fps), 1))
    ext.set_playback_start(seq, 0)
    ext.set_playback_end(seq, 1)            # 1 フレーム
    if scene_sequence is not None and scene_frame is not None:
        # サブセクションの開始位置では内側シーケンスは「その playback start」から
        # 始まる（フレーム0 からではない）。一時フレーム0 = 内側フレーム n に
        # なるよう開始を -(n - playback_start) に置く。範囲外の n はクランプ。
        inner_start = int(ext.get_playback_start(scene_sequence))
        inner_end = int(ext.get_playback_end(scene_sequence))
        n = max(inner_start, min(int(scene_frame), inner_end - 1))
        if n != int(scene_frame):
            _warn("現在フレーム %d は再生範囲 [%d..%d) 外のため %d でキャプチャします"
                  % (int(scene_frame), inner_start, inner_end, n))
        sub = ext.add_track(seq, unreal.MovieSceneSubTrack)
        sub_sec = sub.add_section()
        sub_sec.set_sequence(scene_sequence)
        frozen = False
        try:
            # play rate 0 + start_frame_offset で「フレーム n ちょうど」に完全凍結。
            # テンポラルサンプルはシャッター区間で時間を進めながら蓄積するため、
            # 凍結しないと評価時刻が [n, n+シャッター) に広がり、動いている
            # カメラの平均位置が焼かれて SceneCapture 系マスク（ちょうど n）と
            # 画がズレる（実測: 1080 高で 14px / 2159 高で 28px）。
            tick = ext.get_tick_resolution(scene_sequence)
            disp = ext.get_display_rate(scene_sequence)
            tpf = int(round((tick.numerator * disp.denominator)
                            / float(tick.denominator * disp.numerator)))
            params = sub_sec.get_editor_property("parameters")
            tw = params.get_editor_property("time_scale")
            tw.set_fixed_play_rate(0.0)
            params.set_editor_property("time_scale", tw)
            params.set_editor_property(
                "start_frame_offset", unreal.FrameNumber((n - inner_start) * tpf))
            sub_sec.set_editor_property("parameters", params)
            sub_sec.set_range(0, 1)
            frozen = True
        except Exception as e:
            _warn("サブシーケンスの時間凍結に失敗（範囲写像で継続。カメラが動く"
                  "フレームではマスクと僅かにズレ得る）: %s" % e)
            start = -(n - inner_start)
            sub_sec.set_range(start, max(1, start + (inner_end - inner_start)))
        _log("一時シーケンス: %s をフレーム %d で固定評価%s"
             % (scene_sequence.get_name(), n, "（時間凍結）" if frozen else ""))
    # カメラを possessable で追加
    binding = ext.add_possessable(seq, camera_actor)
    # カメラカットトラック
    cct = ext.add_track(seq, unreal.MovieSceneCameraCutTrack)
    sec = cct.add_section()
    sec.set_range(0, 1)
    binding_id = ext.get_binding_id(seq, binding)
    sec.set_camera_binding_id(binding_id)
    unreal.EditorAssetLibrary.save_loaded_asset(seq)
    full = _TMP_PKG + "/" + _TMP_NAME + "." + _TMP_NAME
    return seq, full


# 影/GI を高品質にする cvar（シーケンサ書き出し相当）。
# MoviePipelineConsoleVariableSetting の cvars 配列で渡す＝エンジンがレンダ後に
# 元値へ自動復元する。start_console_commands は復元されないため、cvar をそちらで
# 送るとエディタへ恒久的にリークする（旧実装は r.TextureStreaming 0 等が残留）。
_HQ_CVARS = [
    ("r.Shadow.Virtual.ResolutionLodBiasDirectional", -1.5),
    ("r.Shadow.Virtual.ResolutionLodBiasLocal", -1.5),
    ("r.Shadow.Virtual.SMRT.RayCountDirectional", 16),
    ("r.Shadow.Virtual.SMRT.SamplesPerRayDirectional", 8),
    ("r.Lumen.ScreenProbeGather.RadianceCache.NumProbesToTraceBudget", 600),
    ("r.Lumen.ScreenProbeGather.TraceMeshSDFs", 1),
    ("r.Lumen.Reflections.Quality", 4),
    ("r.TextureStreaming", 0),
]


# Raw Lighting Direct 用: GI/スカイライト/AO を切って直接光のみにする ShowFlag cvar
# （cvar なのでレンダ後にエンジンが自動復元。静止画/シーケンスの両ジョブで共用）
_DIRECT_ONLY_CVARS = [
    ("ShowFlag.GlobalIllumination", 0),
    ("ShowFlag.SkyLighting", 0),
    ("ShowFlag.AmbientOcclusion", 0),
]


def _lighting_only_class():
    cls = getattr(unreal, "MoviePipelineDeferredPass_LightingOnly", None)
    if cls is None:
        raise RuntimeError("この UE には LightingOnly パス "
                           "(MoviePipelineDeferredPass_LightingOnly) がありません。")
    return cls


def _cv_entries(pairs):
    """(name, value) の並びを MoviePipelineConsoleVariableEntry 配列にする。"""
    out = []
    for name, val in pairs:
        e = unreal.MoviePipelineConsoleVariableEntry()
        e.set_editor_property("name", name)
        e.set_editor_property("value", float(val))
        out.append(e)
    return out


def _suppress_autoplay_players():
    """レベル内 LevelSequenceActor の auto_play を一時 False にし、変更した
    アクターのリストを返す。max2ue インポータ等が置く自動再生プレイヤーが
    PIE レンダ中にシーケンスを再生してカメラが飛び、静止画のテンポラル
    サンプルが放射状スメアになる（2026-07-13 実測）。レンダ後に復元する。"""
    saved = []
    try:
        actors = unreal.GameplayStatics.get_all_actors_of_class(
            _editor_world(), unreal.LevelSequenceActor)
    except Exception:
        actors = []
    for a in actors:
        try:
            ps = a.get_editor_property("playback_settings")
            if bool(ps.get_editor_property("auto_play")):
                ps.set_editor_property("auto_play", False)
                a.set_editor_property("playback_settings", ps)
                saved.append(a)
        except Exception:
            pass
    if saved:
        _log("auto-play の LevelSequenceActor を一時停止: %d 台" % len(saved))
    return saved


def _restore_autoplay_players(saved):
    for a in saved or []:
        try:
            ps = a.get_editor_property("playback_settings")
            ps.set_editor_property("auto_play", True)
            a.set_editor_property("playback_settings", ps)
        except Exception:
            pass


def render_beauty(camera_actor, output_dir, width, height,
                  use_exr=False, image_format=None, also_png=False,
                  spatial_samples=1, temporal_samples=8,
                  warmup=32, file_basename="beauty", hidden_actors=None, on_done=None,
                  near_clip_cm=None, overscan=0.0, fog_off=False,
                  scene_sequence=None, scene_frame=None,
                  matte_material=None, matte_actors=None, depth_material=None,
                  normal_material=None,
                  light_pass=False, light_direct=False,
                  cloud_matte_actors=None, cloud_visible=False,
                  geomask_material=None,
                  backing_actors=None, backing_white=False):
    """対象カメラを MRQ で Beauty レンダリング（非同期）。executor を返す。
    cloud_visible=True（要 cloud_matte_actors）は可視雲モード: 何も隠さず PIE 側で
    全ジオメトリを白発光材へ差し替え、EXR の RGB=W(=T×素板レベル)・α=全投影雲αを
    1ジョブで出す（geomask_material を渡すと GeoMask PP パスも同乗）。
    後段の core.compose_visible_cloud で深度順序どおりの可視雲αに合成する。
    cloud_matte_actors を渡すと CloudMatte ジョブになる: 対象 HV ボリューム(雲)を
    holdout・他プリミティブも holdout・大気/フォグ OFF で、PNG のαに可視雲
    不透明度が入る（RGB はほぼ黒＝マット専用）。要 cloud_matte_ready()。
    light_pass=True で LightingOnly レンダパス（アルベド無視のライティングのみ＝
    落ち影+シェーディング）を同一ジョブに追加する（出力: file_basename_LightingOnly.*）。
    light_direct=True はこのジョブ全体の GI/スカイライト/AO を ShowFlag cvar で切り、
    LightingOnly を直射のみにする（Beauty パスも直射のみになるため専用ジョブで使う）。
    hidden_actors を渡すと、そのアクターを非表示にしてレンダ（Beauty 品質のクリーンプレート）。
    near_clip_cm を渡すと、その距離(cm)より手前を描画時クリップする（fronto-parallel 近似の behind-matte）。
    scene_sequence / scene_frame を渡すと、シーンをそのシーケンスの指定フレームの
    評価で固定してレンダ（シーケンサーの現在フレームの静止画。カメラは camera_actor）。
    matte_material / matte_actors を渡すと、対象をマットレンダモード（Beauty 非表示 +
    CustomDepth ステンシル）にして同一ジョブの追加 PP パスで Matte マスクも出力する
    （出力: file_basename_Matte.png）。depth_material を渡すと正規化深度も同一ジョブの
    PP パスで出力する（出力: file_basename_Depth.png）。normal_material も同様に
    同一ジョブの PP パスでワールド法線を出力する（出力: file_basename_Normal.png）。
    SceneCapture 別撮りだと
    WPO/風で揺れる前景のシルエット位相が Beauty とズレるため、同一ジョブで撮って
    画素整合を保証する。
    出力は output_dir 直下に file_basename.png (or .exr)。完了時 on_done(success, out_dir) を呼ぶ。"""
    output_dir = os.path.normpath(output_dir)
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # 多重起動防止: 既に MRQ レンダ中なら弾く（PIE 衝突で破損 PNG が出るのを防ぐ）
    sub = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    if sub.is_rendering() or _KEEP.get("executor") is not None:
        raise RuntimeError("MRQ は既にレンダリング中です。完了までお待ちください（多重起動防止）。")
    if matte_material is not None and not matte_actors:
        raise RuntimeError("Matte パスには対象アクターが必要です。")

    # シーン状態の変更（レンダ後・起動失敗時に必ず復元）: 非表示指定 / マットレンダ
    # モード / auto-play プレイヤー停止（PIE 中に勝手に再生されるとカメラ・シーンが
    # 動く）/ カメラのアスペクト拘束解除（出力アスペクト≠filmback のときの黒帯防止）
    restore = []
    if hidden_actors:
        for a in hidden_actors:
            try:
                a.set_actor_hidden_in_game(True)
                restore.append(a)
            except Exception:
                pass
    saved_matte = None
    if matte_material is not None:
        saved_matte = _set_matte_render_mode(matte_actors)
    saved_cloud = None
    saved_vis = None
    if cloud_matte_actors and cloud_visible:
        # 可視雲モード: 何も隠さず PIE 側で白バッキング化（深度順序が保たれる）
        saved_vis = {"pie": _start_pie_backing_white(cloud_matte_actors),
                     "fills": _spawn_cloud_fill_lights(5.0)}
    elif cloud_matte_actors:
        # UE5.7 は holdout 方式のα出力が実質壊れている（cvarペア有効でも最大2/255・
        # 2026-07-24実測）ため分離モード固定。エンジン修正後に
        # use_holdout=cloud_matte_holdout_ready() へ戻す。
        saved_cloud = _set_cloud_matte_mode(cloud_matte_actors, use_holdout=False)
    saved_backing = None
    if backing_actors:
        saved_backing = _set_backing_materials(backing_actors, backing_white)
    saved_players = _suppress_autoplay_players()
    saved_cam = [c for c in (_fill_aspect_comp(camera_actor, width, height),)
                 if c is not None]
    if saved_cam:
        _log("カメラのアスペクト拘束を一時解除（静止画）")

    def _restore_scene():
        for a in restore:
            try:
                a.set_actor_hidden_in_game(False)
            except Exception:
                pass
        if saved_matte:
            _restore_matte_render_mode(saved_matte)
        if saved_cloud:
            _restore_cloud_matte_mode(saved_cloud)
        if saved_vis:
            _restore_visible_cloud_mode(saved_vis)
        if saved_backing:
            _restore_backing_materials(saved_backing)
        _restore_autoplay_players(saved_players)
        _restore_cameras_aspect(saved_cam)

    try:
        return _start_render(sub, camera_actor, output_dir, width, height,
                             use_exr, image_format, also_png,
                             spatial_samples, temporal_samples, warmup,
                             file_basename, on_done, near_clip_cm, overscan,
                             fog_off, _restore_scene, scene_sequence, scene_frame,
                             matte_material, depth_material, normal_material,
                             light_pass, light_direct,
                             cloud_matte=bool(cloud_matte_actors),
                             cloud_visible=cloud_visible,
                             geomask_material=geomask_material,
                             backing=bool(backing_actors))
    except Exception:
        # 起動に失敗したら状態を巻き戻す（次回レンダを塞がない）
        _restore_scene()
        if near_clip_cm is not None:
            # 起動前にグローバル適用済みの near clip を戻す
            try:
                unreal.SystemLibrary.execute_console_command(
                    _editor_world(), "r.SetNearClipPlane 10")
            except Exception:
                pass
        _delete_temp_sequence()
        _KEEP.clear()
        raise


def _start_render(sub, camera_actor, output_dir, width, height,
                  use_exr, image_format, also_png,
                  spatial_samples, temporal_samples, warmup,
                  file_basename, on_done, near_clip_cm, overscan,
                  fog_off, restore_scene, scene_sequence=None, scene_frame=None,
                  matte_material=None, depth_material=None, normal_material=None,
                  light_pass=False, light_direct=False, cloud_matte=False,
                  cloud_visible=False, geomask_material=None,
                  backing=False):
    seq, seq_path = _create_temp_sequence(camera_actor,
                                          scene_sequence=scene_sequence,
                                          scene_frame=scene_frame)

    queue = sub.get_queue()
    for j in list(queue.get_jobs()):
        queue.delete_job(j)
    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.job_name = "UE5Capture_Beauty"
    job.map = unreal.SoftObjectPath(_current_map_softpath())
    job.sequence = unreal.SoftObjectPath(seq_path)

    cfg = job.get_configuration()
    extra_passes = []
    for pass_name, pass_mat in (("Matte", matte_material), ("Depth", depth_material),
                                ("Normal", normal_material),
                                ("GeoMask", geomask_material)):
        if pass_mat is None:
            continue
        ppp = unreal.MoviePipelinePostProcessPass()
        ppp.set_editor_property("enabled", True)
        ppp.set_editor_property("name", pass_name)
        ppp.set_editor_property("material", pass_mat)
        extra_passes.append(ppp)
    # 直射専用ジョブ（light_direct）は Beauty(FinalImage) パスを持たない＝捨てる
    # だけの出力にレンダ時間を払わない。追加 PP 材は DeferredPassBase が搬送役
    # なので、あるときはパスを残す。
    if extra_passes or not light_direct:
        deferred = cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
        if extra_passes:
            deferred.set_editor_property("additional_post_process_materials", extra_passes)
        if cloud_matte:
            # タイル蓄積にαを含める（これが無いと最終画像のαが 1 固定になる）
            deferred.set_editor_property("accumulator_includes_alpha", True)
    if light_pass:
        # LightingOnly は独立したレンダパス（追加 PP 材とは別系統）。
        # 出力は <basename>_LightingOnly.* になる（{render_pass} 命名が必須になる）。
        cfg.find_or_add_setting_by_class(_lighting_only_class())
    # image_format: "png"（既定）/ "jpg" / "exr"。exr のとき also_png=True で
    # PNG も同時出力する（Matte 系合成が PIL で読める画像を必要とするため）。
    fmt = (image_format or ("exr" if use_exr else "png")).lower()
    if fmt == "exr":
        exr_out = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_EXR)
        if light_pass or cloud_visible:
            # マルチレイヤ EXR だと LightingOnly/追加PPパスが別ファイルにならないため分割
            exr_out.set_editor_property("multilayer", False)
        if also_png:
            png_fmt = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
            try:
                png_fmt.set_editor_property("write_alpha", False)
            except Exception:
                pass
    elif fmt == "jpg":
        jpg_cls = getattr(unreal, "MoviePipelineImageSequenceOutput_JPG", None)
        if jpg_cls is None:
            raise RuntimeError("この UE には JPG 出力 (MoviePipelineImageSequenceOutput_JPG) がありません。")
        cfg.find_or_add_setting_by_class(jpg_cls)
    else:
        out_fmt = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
        try:
            out_fmt.set_editor_property("write_alpha", bool(cloud_matte))
        except Exception:
            pass

    out = cfg.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    out.set_editor_property("output_directory", unreal.DirectoryPath(output_dir))
    out.set_editor_property("output_resolution", unreal.IntPoint(int(width), int(height)))
    if extra_passes or light_pass:
        # 追加パスがあるときは {render_pass} が必須（完了時にリネームで整える）
        out.set_editor_property("file_name_format", file_basename + "_{render_pass}")
    else:
        out.set_editor_property("file_name_format", file_basename)   # 単一フレームなのでフレーム番号なし
    out.set_editor_property("override_existing_output", True)
    out.set_editor_property("zero_pad_frame_numbers", 4)
    try:
        out.set_editor_property("flush_disk_writes_per_shot", True)  # 完了前に確実に書き出す
    except Exception:
        pass
    # 1 フレームだけ出す（end は排他的なので [0,1) = フレーム0 のみ）
    out.set_editor_property("use_custom_playback_range", True)
    out.set_editor_property("custom_start_frame", 0)
    out.set_editor_property("custom_end_frame", 1)

    aa = cfg.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    aa.set_editor_property("override_anti_aliasing", True)
    aa.set_editor_property("spatial_sample_count", int(spatial_samples))
    aa.set_editor_property("temporal_sample_count", int(temporal_samples))
    aa.set_editor_property("engine_warm_up_count", int(warmup))
    aa.set_editor_property("render_warm_up_count", int(warmup))
    try:
        aa.set_editor_property("anti_aliasing_method",
                               unreal.AntiAliasingMethod.AAM_TSR)
    except Exception:
        pass

    # Overscan（元カメラを変えずレンダ時だけ余白を追加。解像度も増える＝周囲にピクセルを足す）
    if overscan and float(overscan) > 0.0:
        camset = cfg.find_or_add_setting_by_class(unreal.MoviePipelineCameraSetting)
        camset.set_editor_property("override_camera_overscan", True)
        camset.set_editor_property("overscan_percentage", float(overscan))
        _log("overscan = %.1f%%" % (float(overscan) * 100.0))

    go = cfg.find_or_add_setting_by_class(unreal.MoviePipelineGameOverrideSetting)
    go.set_editor_property("use_high_quality_shadows", True)
    go.set_editor_property("use_lod_zero", True)
    go.set_editor_property("flush_grass_streaming", True)
    go.set_editor_property("flush_streaming_managers", True)
    try:
        go.set_editor_property("texture_streaming",
                               unreal.MoviePipelineTextureStreamingMethod.FULLY_LOAD)
    except Exception:
        pass

    pairs = list(_HQ_CVARS) + [("r.MotionBlurQuality", 0)]   # 静止画はブラー無し
    if matte_material is not None:
        pairs.append(("r.CustomDepth", 3))   # ステンシル書き込みに必要（自動復元）
    if fog_off:
        pairs += [("r.Fog", 0), ("r.VolumetricFog", 0)]
        _log("fog off")
    if light_direct:
        # 直射のみ: LightingOnly が「直接光の落ち影+シェーディングのみ・影は完全な黒」になる。
        pairs += list(_DIRECT_ONLY_CVARS)
        _log("direct lighting only (GI/Sky/AO off)")
    if cloud_matte:
        # α伝播（MRQ 既定でも ON になるが明示）+ αを埋める大気/フォグを OFF。
        # ⚠️ ShowFlag.Cloud 0 は VolumetricCloud だけでなく HV(VDB雲)も消す（実測）
        # ため使用禁止。
        # ⚠️ UDS の雲レイヤー（VolumetricCloud=大気雲）は α マットに乗せられない:
        # Atmosphere ON だと大気自体が全画素 α=1 で埋め、SkyAtmosphere の holdout +
        # SupportPrimitiveAlphaHoldout も UE5.7 では無効（全て 2026-08-26 実測）。
        # マスク対象は HV(VDB) 雲のみ。
        pairs += [("r.PostProcessing.PropagateAlpha", 1),
                  ("ShowFlag.Atmosphere", 0), ("ShowFlag.Fog", 0),
                  ("ShowFlag.VolumetricFog", 0)]
        _log("cloud matte (alpha / atmosphere+fog off)")
    if backing or cloud_visible:
        # 露出適応は白板に反応して画面全体を沈める（-18%実測）ため、適応と
        # ローカル露出をジョブ内で無効化。白板は発光100なのでブルームと
        # Lumen スクリーントレースの撒き散らしも切る（×100で無視できなくなる）。
        pairs += [("r.EyeAdaptationQuality", 0),
                  ("r.LocalExposure.HighlightContrastScale", 1.0),
                  ("r.LocalExposure.ShadowContrastScale", 1.0),
                  ("r.BloomQuality", 0),
                  ("r.Lumen.ScreenProbeGather.ScreenTraces", 0),
                  ("r.Lumen.Reflections.ScreenTraces", 0)]
        # 既定の EXR はトーンカーブ適用済み（発光100が~1.0に圧縮される実測）。
        # T=W/素板レベル の線形性が前提なのでトーンカーブを切ってシーンリニアで書く。
        try:
            col = cfg.find_or_add_setting_by_class(unreal.MoviePipelineColorSetting)
            col.set_editor_property("disable_tone_curve", True)
        except Exception as e:
            _warn("backing: トーンカーブ無効化に失敗（Tが非線形になる）: %s" % e)
        _log("backing render (exposure locked / bloom+screen traces off / linear)")
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cv.set_editor_property("cvars", _cv_entries(pairs))   # レンダ後にエンジンが自動復元
    cmds = []
    if near_clip_cm is not None:
        # マット面までの距離より手前をクリップ（fronto-parallel 近似）。形状は後段でαマスク。
        # r.SetNearClipPlane は cvar でなくコマンド＝自動復元されない（完了時に手動復元）。
        cmds.append("r.SetNearClipPlane %f" % float(near_clip_cm))
        _log("near clip = %.1f cm" % float(near_clip_cm))
        # start_console_commands だけだとウォームアップ/最初のサブフレームに間に合わず、
        # クリップ有り/無しが平均されて手前オブジェクトが半透明ゴースト化する（実測）。
        # レンダ起動前にグローバルへも適用しておく（_on_finished が既定 10cm へ復元）。
        unreal.SystemLibrary.execute_console_command(
            _editor_world(), "r.SetNearClipPlane %f" % float(near_clip_cm))
    cv.set_editor_property("start_console_commands", cmds)

    executor = unreal.MoviePipelinePIEExecutor()

    def _on_finished(exec_obj, success):
        _log("MRQ レンダ完了 success=%s -> %s" % (success, output_dir))
        _delete_temp_sequence()
        restore_scene()                 # 非表示 / matte / auto-play / アスペクトを戻す
        if extra_passes or light_pass:
            # <base>_FinalImage<Name>.* → <base>_<Name>.* / <base>_FinalImage.* → <base>.*
            try:
                for f in os.listdir(output_dir):
                    if not f.startswith(file_basename + "_FinalImage"):
                        continue
                    nf = (f.replace("_FinalImageMatte", "_Matte")
                           .replace("_FinalImageDepth", "_Depth")
                           .replace("_FinalImageNormal", "_Normal")
                           .replace("_FinalImageGeoMask", "_GeoMask")
                           .replace("_FinalImage", ""))
                    os.replace(os.path.join(output_dir, f), os.path.join(output_dir, nf))
            except Exception as e:
                _warn("追加パスのリネームに失敗: %s" % e)
        if near_clip_cm is not None:
            # near clip はコマンドでグローバルに残るので必ず既定(10cm)へ戻す
            try:
                unreal.SystemLibrary.execute_console_command(
                    _editor_world(), "r.SetNearClipPlane 10")
            except Exception:
                pass
        _KEEP.clear()                           # 先にクリア（on_done がチェインで次の render を張る場合があるため）
        if on_done:
            try:
                on_done(bool(success), output_dir)
            except Exception as e:
                _warn("on_done でエラー: %s" % e)

    executor.on_executor_finished_delegate.add_callable(_on_finished)
    _KEEP["executor"] = executor
    _KEEP["queue"] = queue

    _log("MRQ レンダ開始: %s  %dx%d  TS=%d warmup=%d  out=%s"
         % (camera_actor.get_actor_label(), width, height, temporal_samples, warmup, output_dir))
    sub.render_queue_with_executor_instance(executor)
    return executor


# ----------------------------------------------------------------------------
# シーケンスレンダ（PNG連番 / MP4。ユーザーの LevelSequence を直接レンダ）
# ----------------------------------------------------------------------------
def _set_matte_render_mode(actors):
    """マット対象を「ビューティに写らず CustomDepth にだけ写る」状態にする。
    影も落とさない（クリーンプレートと Matte パスを1ジョブで両立するため）。
    ステンシル=MATTE_STENCIL を付与し、Matte/MatteSil マテリアルはステンシル一致で
    対象を判定する（ObjectID 対象の CustomDepth 書き込みと混ざらないため）。
    返り値は復元用の (component, main_pass, custom_depth, cast_shadow, stencil) リスト。"""
    saved = []
    for a in actors or []:
        if a is None:
            continue
        for comp in a.get_components_by_class(unreal.PrimitiveComponent):
            if _is_hv_comp(comp):
                continue   # HV(雲)は CustomDepth に写らない: cloud_matte 側で扱う
            try:
                saved.append((comp,
                              comp.get_editor_property("render_in_main_pass"),
                              comp.get_editor_property("render_custom_depth"),
                              comp.get_editor_property("cast_shadow"),
                              comp.get_editor_property("custom_depth_stencil_value"),
                              comp.get_editor_property("affect_distance_field_lighting"),
                              comp.get_editor_property("affect_dynamic_indirect_lighting")))
                comp.set_editor_property("render_in_main_pass", False)
                comp.set_editor_property("render_custom_depth", True)
                comp.set_editor_property("cast_shadow", False)
                comp.set_editor_property("custom_depth_stencil_value", MATTE_STENCIL)
                # main pass 非表示でも距離フィールド/Lumen には残り、クリーン
                # プレートの AO/GI を板が暗くする → レンダ中は寄与を切る
                comp.set_editor_property("affect_distance_field_lighting", False)
                comp.set_editor_property("affect_dynamic_indirect_lighting", False)
            except Exception as e:
                _warn("Matte レンダモード設定に失敗: %s" % e)
    return saved


def _restore_matte_render_mode(saved):
    for comp, mp, cd, cs, st, dfl, dil in saved or []:
        try:
            comp.set_editor_property("render_in_main_pass", mp)
            comp.set_editor_property("render_custom_depth", cd)
            comp.set_editor_property("cast_shadow", cs)
            comp.set_editor_property("custom_depth_stencil_value", st)
            comp.set_editor_property("affect_distance_field_lighting", dfl)
            comp.set_editor_property("affect_dynamic_indirect_lighting", dil)
        except Exception as e:
            _warn("Matte レンダモード復元に失敗: %s" % e)


def cloud_matte_holdout_ready():
    """ホールドアウト方式（遮蔽考慮の雲マット）が使えるか。
    r.Deferred.SupportPrimitiveAlphaHoldout は読み取り専用 cvar（ini・要再起動）。
    ⚠️ UE5.7 では有効化するとエディタビューポートの HV 描画で
    RWHoldoutTexture 未束縛の Fatal クラッシュ（エンジンバグ・2026-07-24 実測）。
    通常は無効のままで、CloudMatte は分離モード（遮蔽なし）で撮る。"""
    try:
        return unreal.SystemLibrary.get_console_variable_int_value(
            "r.Deferred.SupportPrimitiveAlphaHoldout") != 0
    except Exception:
        return False


def _set_cloud_matte_mode(vol_targets, use_holdout=False):
    """CloudMatte ジョブ用のシーン状態。
    use_holdout=True（要 cloud_matte_holdout_ready・UE5.7 ではエンジンバグで通常不可）:
      - 対象ボリューム: holdout=True（合成シェーダでαに可視不透明度が加算される）
      - 対象外のボリューム: 非表示 / その他の全プリミティブ: holdout（遮蔽のみ）
    use_holdout=False（分離モード・既定）:
      - 対象以外のアクターを**アクター単位で**隠す。ただし**ライトコンポーネントを
        持つアクター（UDS/UDW/各ライト）は隠さない** — HV は無照明だとαも出ず全黒
        （2026-07-24実測）。ライト持ちの見た目（大気/フォグ）は ShowFlag 側で消す。
      - ⚠️ 他アクターの SCS コンポーネント単位で hidden_in_game/visible を編集する
        方式は不可（BP再構築の副作用で HV が全黒になる・実測）。ShowFlag.Cloud も
        HV ごと消すため不可。αは遮蔽を考慮しない全投影の雲不透明度になる。
    返り値: (holdout解除リスト, アクター再表示リスト, コンポーネント再表示リスト)。"""
    targets = set(a.get_name() for a in vol_targets or [])
    holdout_comps, hidden_actors, hidden_comps = [], [], []
    actors = unreal.get_editor_subsystem(
        unreal.EditorActorSubsystem).get_all_level_actors()
    for a in actors:
        if a.get_name() in targets:
            if use_holdout and _HV_COMP_CLASS is not None:
                for c in a.get_components_by_class(_HV_COMP_CLASS):
                    try:
                        if not c.get_editor_property("holdout"):
                            c.set_editor_property("holdout", True)
                            holdout_comps.append(c)
                    except Exception:
                        pass
            continue
        if not use_holdout:
            try:
                # 太陽/スカイライト持ちだけ残す（隠すと HV が無照明でα全黒・2026-07-24実測）。
                # ローカルライト（Point/Spot/Rect）しか持たないアクターは隠す — 雲の照明は
                # 太陽/スカイ支配で、残すと不透明ジオメトリがα=1でマスクを汚す
                # （AV024 でライト内蔵の街BPが雲マットに白く混入した実測 2026-08-25）。
                lights = a.get_components_by_class(unreal.LightComponentBase)
                if lights and any(isinstance(c, (unreal.DirectionalLightComponent,
                                                 unreal.SkyLightComponent))
                                  for c in lights):
                    continue
                if not a.get_editor_property("hidden"):
                    a.set_actor_hidden_in_game(True)
                    hidden_actors.append(a)
            except Exception:
                pass
            continue
        if is_volumetric_actor(a):
            try:
                if not a.get_editor_property("hidden"):
                    a.set_actor_hidden_in_game(True)
                    hidden_actors.append(a)
            except Exception:
                pass
            continue
        for c in a.get_components_by_class(unreal.PrimitiveComponent):
            try:
                if not c.get_editor_property("holdout"):
                    c.set_editor_property("holdout", True)
                    holdout_comps.append(c)
            except Exception:
                pass
    _log("CloudMatte(%s): holdout %d comps / 非表示 %d actors / %d comps"
         % ("holdout" if use_holdout else "分離", len(holdout_comps),
            len(hidden_actors), len(hidden_comps)))
    pie_state = None
    fill_lights = []
    if not use_holdout:
        pie_state = _start_pie_cloud_hider(vol_targets)
        fill_lights = _spawn_cloud_fill_lights()
    return holdout_comps, hidden_actors, hidden_comps, pie_state, fill_lights


def _spawn_cloud_fill_lights(intensity=20.0):
    """CloudMatte 分離レンダ用の無影フィルライトを一時スポーンする。
    HV(VDB雲) の α は照明依存（無照明で α0・照明の弱い遠景雲が α から消える実測）
    なので、シーン照明に依らず全方位からフラットに照らして α を安定させる。
    RGB は捨てるので過露光は問題ない（可視雲モードでは W=T×素板 の誤差 ≈
    雲散乱/素板レベルになるため低強度を渡す）。返り値: レンダ後に破棄するリスト。"""
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    out = []
    for i, (pitch, yaw) in enumerate(((-90.0, 0.0), (30.0, 45.0), (30.0, 225.0))):
        try:
            a = eas.spawn_actor_from_class(
                unreal.DirectionalLight, unreal.Vector(0.0, 0.0, 200000.0),
                unreal.Rotator(roll=0.0, pitch=pitch, yaw=yaw))
            a.set_actor_label("UE5Cap_CloudFill_%d" % i)
            lc = a.get_editor_property("light_component")
            lc.set_editor_property("intensity", float(intensity))
            lc.set_editor_property("cast_shadows", False)
            lc.set_editor_property("atmosphere_sun_light", False)
            out.append(a)
        except Exception as e:
            _warn("CloudMatte: フィルライト生成に失敗: %s" % e)
    if out:
        _log("CloudMatte: 無影フィルライト %d 灯を一時スポーン (%.1f lux)"
             % (len(out), intensity))
    return out


def _start_pie_backing_white(vol_targets):
    """可視雲マット用: PIE ワールド側で全メッシュ材を白発光アンリット材へ差し替える
    ウォッチャ（バッキング差分の一般化）。何も隠さないので深度順序が保たれ、
    線形色 W = T×素板レベル から「雲より手前/奥」を画素毎に正しく分離できる。
    LevelInstance 内包・スポーナブルにも効かせるため PIE 側で毎 tick 冪等に行う
    （PIE は破棄されるので復元不要）。"""
    from capture_core import get_or_create_backing_white_material, _is_hv_comp
    mat = get_or_create_backing_white_material()
    tnames = set()
    for a in vol_targets or []:
        try:
            tnames.add(a.get_name())
            tnames.add(a.get_actor_label())
        except Exception:
            pass
    state = {"h": None, "done": set(), "logged": False}

    def _stop():
        h = state.pop("h", None)
        if h is not None:
            try:
                unreal.unregister_slate_post_tick_callback(h)
            except Exception:
                pass

    def _tick(dt):
        pie = None
        try:
            pie = unreal.get_editor_subsystem(
                unreal.UnrealEditorSubsystem).get_game_world()
        except Exception:
            try:
                pie = unreal.EditorLevelLibrary.get_game_world()
            except Exception:
                pie = None
        if pie is None:
            if state["logged"]:
                _stop()
            return
        n = 0
        try:
            for a in unreal.GameplayStatics.get_all_actors_of_class(pie, unreal.Actor):
                try:
                    key = a.get_path_name()
                    if key in state["done"]:
                        continue
                    if a.get_name() in tnames or a.get_actor_label() in tnames:
                        continue
                    if isinstance(a, unreal.CameraActor):
                        continue
                    comps = a.get_components_by_class(unreal.MeshComponent)
                    if not comps:
                        continue
                    for comp in comps:
                        if _is_hv_comp(comp):
                            continue      # 雲(HV)は差し替え不可・対象そのもの
                        try:
                            for i in range(comp.get_num_materials()):
                                comp.set_material(i, mat)
                        except Exception:
                            pass
                    state["done"].add(key)
                    n += 1
                except Exception:
                    pass
        except Exception:
            pass
        if not state["logged"]:
            state["logged"] = True
            _log("可視雲: PIE 側で %d アクターを白バッキング材へ差替" % n)

    state["h"] = unreal.register_slate_post_tick_callback(_tick)
    state["stop"] = _stop
    return state


def _start_pie_cloud_hider(vol_targets):
    """PIE ワールド側で雲以外の描画アクターを隠すウォッチャを開始する。
    LevelInstance 内包アクターは PIE で資産から再生成されるため、エディタ側の
    hidden が伝搬しない（AV024 の街 LevelInstance の StaticMeshActor 184台が
    隠れずαを汚した実測 2026-08-25）。Sequencer スポーナブルも同様。
    PIE 出現を slate tick で待ち、毎 tick 冪等に隠す（ストリーミングの遅延流入や
    スポーナブルも拾う）。PIE は終了時に破棄されるので復元不要。"""
    tnames = set()
    for a in vol_targets or []:
        try:
            tnames.add(a.get_name())
            tnames.add(a.get_actor_label())
        except Exception:
            pass
    state = {"h": None, "n": 0, "logged": False}

    def _stop():
        h = state.pop("h", None)
        if h is not None:
            try:
                unreal.unregister_slate_post_tick_callback(h)
            except Exception:
                pass

    def _tick(dt):
        pie = None
        try:
            pie = unreal.get_editor_subsystem(
                unreal.UnrealEditorSubsystem).get_game_world()
        except Exception:
            try:
                pie = unreal.EditorLevelLibrary.get_game_world()
            except Exception:
                pie = None
        if pie is None:
            if state["logged"]:
                _stop()        # PIE が終わった → 監視終了
            return
        n = 0
        try:
            for a in unreal.GameplayStatics.get_all_actors_of_class(pie, unreal.Actor):
                try:
                    if a.get_name() in tnames or a.get_actor_label() in tnames:
                        continue
                    if isinstance(a, unreal.CameraActor):
                        continue      # レンダ視点（スポーナブルカメラ含む）は触らない
                    if not a.get_components_by_class(unreal.PrimitiveComponent):
                        continue
                    if a.get_editor_property("hidden"):
                        continue      # エディタ側で隠した分の複製・処理済み分
                    lights = a.get_components_by_class(unreal.LightComponentBase)
                    if lights and any(isinstance(c, (unreal.DirectionalLightComponent,
                                                     unreal.SkyLightComponent))
                                      for c in lights):
                        continue      # 太陽/スカイライト持ちは残す（雲の照明）
                    a.set_actor_hidden_in_game(True)
                    n += 1
                except Exception:
                    pass
        except Exception:
            pass
        state["n"] += n
        if not state["logged"]:
            state["logged"] = True
            _log("CloudMatte: PIE 側で %d アクターを追加で隠しました" % n)

    state["h"] = unreal.register_slate_post_tick_callback(_tick)
    state["stop"] = _stop
    return state


def _restore_visible_cloud_mode(saved):
    """可視雲モードの後始末: PIE 白差替ウォッチャ停止 + フィルライト破棄。
    PIE ワールド側の材差替は PIE 破棄で消えるので復元不要。"""
    pie_state = (saved or {}).get("pie")
    if pie_state and pie_state.get("stop"):
        try:
            pie_state["stop"]()
        except Exception:
            pass
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (saved or {}).get("fills") or []:
        try:
            eas.destroy_actor(a)
        except Exception:
            pass


def _restore_cloud_matte_mode(saved):
    saved = tuple(saved or ([], [], []))
    if len(saved) == 3:               # 旧形式互換
        saved += (None,)
    if len(saved) == 4:
        saved += ([],)
    holdout_comps, hidden_actors, hidden_comps, pie_state, fill_lights = saved
    if pie_state and pie_state.get("stop"):
        try:
            pie_state["stop"]()
        except Exception:
            pass
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in fill_lights or []:
        try:
            eas.destroy_actor(a)
        except Exception:
            pass
    for c in holdout_comps:
        try:
            c.set_editor_property("holdout", False)
        except Exception:
            pass
    for a in hidden_actors:
        try:
            a.set_actor_hidden_in_game(False)
        except Exception:
            pass
    for c, prop, val in hidden_comps:
        try:
            c.set_editor_property(prop, val)
        except Exception:
            pass


def _set_backing_materials(actors, white):
    """バッキング差分レンダ用: マット板の全メッシュスロットを白(1.0)/黒(板の常用
    アンリット材)へ一時差替え（アクター単位の property でなく material のみ・
    HV コンポーネントは対象外）。復元用 [(comp, slot, 元material)] を返す。"""
    from capture_core import (get_or_create_backing_white_material,
                              get_or_create_matteboard_material)
    mat = (get_or_create_backing_white_material() if white
           else get_or_create_matteboard_material())
    saved = []
    for a in actors or []:
        try:
            comps = a.get_components_by_class(unreal.MeshComponent)
        except Exception:
            continue
        for comp in comps:
            if _is_hv_comp(comp):
                continue
            for i in range(comp.get_num_materials()):
                saved.append((comp, i, comp.get_material(i)))
                comp.set_material(i, mat)
    return saved


def _restore_backing_materials(saved):
    for comp, i, mat in saved or []:
        try:
            comp.set_material(i, mat)
        except Exception:
            pass


def _set_objid_render_mode(actors):
    """ObjectID 対象に CustomDepth+ステンシル値（リスト順に 1..N）を付与する。
    main pass の表示はそのまま（オクルージョンはマテリアル側で深度一致判定）。
    返り値は復元用の (component, custom_depth, stencil) リスト。"""
    saved = []
    for idx, a in enumerate(actors or []):
        if a is None:
            continue
        stencil = idx + 1
        if stencil >= MATTE_STENCIL:
            _warn("ObjectID 対象が %d を超えたため以降をスキップします"
                  "（%d はマット用に予約）" % (MATTE_STENCIL - 1, MATTE_STENCIL))
            break
        for comp in a.get_components_by_class(unreal.PrimitiveComponent):
            try:
                saved.append((comp,
                              comp.get_editor_property("render_custom_depth"),
                              comp.get_editor_property("custom_depth_stencil_value")))
                comp.set_editor_property("render_custom_depth", True)
                comp.set_editor_property("custom_depth_stencil_value", stencil)
            except Exception as e:
                _warn("ObjectID レンダモード設定に失敗: %s" % e)
    return saved


def _restore_objid_render_mode(saved):
    for comp, cd, st in saved or []:
        try:
            comp.set_editor_property("render_custom_depth", cd)
            comp.set_editor_property("custom_depth_stencil_value", st)
        except Exception as e:
            _warn("ObjectID レンダモード復元に失敗: %s" % e)


def _camera_cut_camera_actors(level_sequence, world):
    """カメラカットに束縛されたカメラアクターを全セクションから解決して返す
    （CineCameraActor / 素の CameraActor の両方。スポーナブルは Sequencer が
    プレビュー用にスポーンした実体がワールドに居ても locate_bound_objects では
    解決できない＝既知の限界。呼び出し側でワールド走査にフォールバックする）。"""
    ext = unreal.MovieSceneSequenceExtensions
    actors = []
    guids = []
    for tr in (ext.find_tracks_by_exact_type(
            level_sequence, unreal.MovieSceneCameraCutTrack) or []):
        for sec in tr.get_sections():
            try:
                # Guid 構造体の == は UE5.7 Python では常に False（実測）。
                # export_text() の文字列で比較する。
                guids.append(sec.get_camera_binding_id()
                             .get_editor_property("guid").export_text())
            except Exception:
                pass
    for b in ext.get_bindings(level_sequence):
        if b.get_id().export_text() in guids:
            for o in ext.locate_bound_objects(level_sequence, b, world):
                if isinstance(o, unreal.CameraActor) and o not in actors:
                    actors.append(o)
    return actors


def _fill_aspect_comp(camera_actor, width, height):
    """カメラのアスペクト拘束が出力解像度とミスマッチなら constrain_aspect_ratio を
    False にしてそのコンポーネントを返す（一致 or 非拘束なら None）。
    CineCamera は filmback、素の CameraActor は aspect_ratio プロパティで判定する
    （素のカメラも constrain_aspect_ratio + aspect_ratio で黒帯が出る。従来は
    Cine 専用で素のカメラは放置＝黒帯+パス間ズレになっていた）。"""
    try:
        comp = camera_actor.camera_component
        if not bool(comp.get_editor_property("constrain_aspect_ratio")):
            return None
        try:
            fb = comp.get_editor_property("filmback")
            cam_asp = (float(fb.get_editor_property("sensor_width"))
                       / max(float(fb.get_editor_property("sensor_height")), 1e-6))
        except Exception:
            cam_asp = float(comp.get_editor_property("aspect_ratio"))
        if cam_asp <= 0.0 or abs(cam_asp - float(width) / float(height)) < 1e-3:
            return None            # 一致していれば拘束は無害（黒帯が出ない）
        comp.set_editor_property("constrain_aspect_ratio", False)
        return comp
    except Exception:
        return None


def _set_cameras_fill_aspect(level_sequence, width, height):
    """出力解像度のアスペクトが filmback と違うカメラの拘束を一時解除し、変更した
    コンポーネントのリストを返す。拘束ONのままだと FinalImage は中央寄せ黒帯・
    追加 PP パス（Depth/Matte/MatteSil/ObjectID）は左詰め書き込みになり、パス間・
    ジョブ間で画がズレる（2026-07-13 実測: 2048x858 × filmback 1.778 → Depth が
    x=1526 から右黒帯）。拘束を外しても水平 FOV は filmback 由来のまま。"""
    try:
        actors = _camera_cut_camera_actors(level_sequence, _editor_world())
    except Exception as e:
        _warn("カメラカットのカメラ解決に失敗: %s" % e)
        actors = []
    if not actors:
        # フォールバック: レベル内の全カメラ（ミスマッチのものだけ触り、復元する）
        try:
            actors = unreal.GameplayStatics.get_all_actors_of_class(
                _editor_world(), unreal.CameraActor)
        except Exception:
            actors = []
    saved = [c for c in (_fill_aspect_comp(a, width, height) for a in actors)
             if c is not None]
    if saved:
        _log("カメラのアスペクト拘束を一時解除: %d 台（黒帯/ジョブ間ズレ防止）" % len(saved))
    return saved


def _restore_cameras_aspect(saved):
    for comp in saved or []:
        try:
            comp.set_editor_property("constrain_aspect_ratio", True)
        except Exception:
            pass


def render_sequence(level_sequence, output_dir, width, height, name_body, take_str,
                    do_png=True, do_mp4=False, mp4_crf=20,
                    temporal_samples=8, warmup=32,
                    custom_start=None, custom_end=None,
                    depth_material=None, matte_material=None, matte_actors=None,
                    matte_sil_material=None, normal_material=None,
                    objid_material=None, objid_actors=None,
                    hidden_actors=None, near_clip_cm=None, beauty_label="Beauty",
                    fog_off=False, on_done=None,
                    light_pass=False, light_direct=False,
                    light_label="RawLightingFull",
                    cloud_matte_actors=None, cloud_visible=False,
                    geomask_material=None,
                    backing_actors=None, backing_white=False, use_exr=False):
    """開いている/指定の LevelSequence を MRQ でレンダリング（非同期）。
    一時シーケンスは作らず job.sequence に直接指定し、カメラはシーケンスの
    カメラカットトラックに従う。fps はシーケンスの Display Rate。
    静止画と違いモーションブラーは殺さない（切ると動きがストロボ状になる）。
    do_png=PNG連番 / do_mp4=内蔵 H.264 MP4（CRF 指定・音声なし）。両方同時可。
    depth_material / matte_material / normal_material を渡すと
    additional_post_process_materials でパスが増え、パス毎に別ファイルで出力される。matte_material / matte_sil_material
    には matte_actors も必須（対象を main pass 非表示 + CustomDepth 書き込みに切替え、
    完了時に復元）。matte_sil_material は遮蔽非依存の全投影シルエット（Behind 合成用）。
    hidden_actors は単純な非表示（クリーンプレートのみ。matte とは排他で使う）。
    near_clip_cm でその距離より手前を描画時クリップ（behind-matte のプレート用。
    グローバル cvar なので完了時に既定 10cm へ戻す）。
    出力名: name_body_{render_pass}_take.{frame_number} 。レンダパス名は完了時に
    FinalImage→beauty_label（既定 Beauty）/ FinalImageDepth→Depth /
    FinalImageMatte→Matte にリネーム。beauty_label は behind プレートの2本目ジョブが
    メインの Beauty と衝突しないための上書き用（例 "BehindPlate"）。
    light_pass=True で LightingOnly レンダパス（ライティングのみ素材）を追加し、
    完了時に _LightingOnly → _light_label にリネーム。light_direct=True はジョブ全体の
    GI/スカイライト/AO を ShowFlag cvar で切る（直射のみ。Beauty パスも汚れるため
    beauty_label を内部名にして専用ジョブで使う）。"""
    output_dir = os.path.normpath(output_dir)
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    if not (do_png or do_mp4):
        raise RuntimeError("PNG連番 / MP4 のどちらも選ばれていません。")
    if (matte_material is not None or matte_sil_material is not None) and not matte_actors:
        raise RuntimeError("Matte 出力には対象アクターが必要です。")
    if objid_material is not None and not objid_actors:
        raise RuntimeError("ObjectID 出力には対象アクターが必要です。")

    sub = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    if sub.is_rendering() or _KEEP.get("executor") is not None:
        raise RuntimeError("MRQ は既にレンダリング中です。完了までお待ちください（多重起動防止）。")

    # カメラカットトラックが無いと何も映らないので先に弾く
    try:
        cuts = unreal.MovieSceneSequenceExtensions.find_tracks_by_exact_type(
            level_sequence, unreal.MovieSceneCameraCutTrack)
    except Exception:
        cuts = None
    if cuts is not None and not list(cuts):
        raise RuntimeError("シーケンスにカメラカットトラックがありません。"
                           "Sequencer でカメラカットを追加してください。")

    # シーン状態の変更（レンダ後・起動失敗時に必ず復元）
    saved_matte = None
    saved_objid = None
    saved_cloud = None
    hidden = []
    if matte_material is not None or matte_sil_material is not None:
        saved_matte = _set_matte_render_mode(matte_actors)
    if hidden_actors:
        # matte レンダモードと独立に適用する（板＝matte機構 / 雲＝単純非表示 の併用）
        for a in hidden_actors:
            try:
                a.set_actor_hidden_in_game(True)
                hidden.append(a)
            except Exception:
                pass
    if objid_material is not None:
        saved_objid = _set_objid_render_mode(objid_actors)
    saved_vis = None
    if cloud_matte_actors and cloud_visible:
        # 可視雲モード: 何も隠さず PIE 側で白バッキング化（深度順序が保たれる）
        saved_vis = {"pie": _start_pie_backing_white(cloud_matte_actors),
                     "fills": _spawn_cloud_fill_lights(5.0)}
    elif cloud_matte_actors:
        # 分離モード固定（render_beauty 側の注記参照。holdout は UE5.7 で出力が壊れている）
        saved_cloud = _set_cloud_matte_mode(cloud_matte_actors, use_holdout=False)
    saved_backing = None
    if backing_actors:
        saved_backing = _set_backing_materials(backing_actors, backing_white)
    # レンダ対象以外の auto-play プレイヤーが PIE で並走するとシーンが二重評価される
    saved_players = _suppress_autoplay_players()
    # 全ジョブで適用する。メイン/BehindPlate 間でビュー矩形が食い違うと
    # per-frame 合成がズレるため（ミスマッチのカメラだけ触る）。
    saved_cam_aspect = _set_cameras_fill_aspect(level_sequence, width, height)

    def _restore_scene():
        if saved_matte:
            _restore_matte_render_mode(saved_matte)
        if saved_objid:
            _restore_objid_render_mode(saved_objid)
        if saved_cloud:
            _restore_cloud_matte_mode(saved_cloud)
        if saved_vis:
            _restore_visible_cloud_mode(saved_vis)
        if saved_backing:
            _restore_backing_materials(saved_backing)
        for a in hidden:
            try:
                a.set_actor_hidden_in_game(False)
            except Exception:
                pass
        _restore_autoplay_players(saved_players)
        _restore_cameras_aspect(saved_cam_aspect)

    try:
        return _start_sequence_render(sub, level_sequence, output_dir, width, height,
                                      name_body, take_str, do_png, do_mp4, mp4_crf,
                                      temporal_samples, warmup, custom_start, custom_end,
                                      depth_material, matte_material, matte_sil_material,
                                      objid_material, normal_material,
                                      near_clip_cm, beauty_label, fog_off,
                                      _restore_scene, on_done,
                                      light_pass, light_direct, light_label,
                                      cloud_matte=bool(cloud_matte_actors),
                                      cloud_visible=cloud_visible,
                                      geomask_material=geomask_material,
                                      backing=bool(backing_actors), use_exr=use_exr)
    except Exception:
        _restore_scene()
        if near_clip_cm is not None:
            # 起動前にグローバル適用済みの near clip を戻す
            try:
                unreal.SystemLibrary.execute_console_command(
                    _editor_world(), "r.SetNearClipPlane 10")
            except Exception:
                pass
        _KEEP.clear()      # 起動失敗時に次回レンダを塞がない
        raise


def _start_sequence_render(sub, level_sequence, output_dir, width, height,
                           name_body, take_str, do_png, do_mp4, mp4_crf,
                           temporal_samples, warmup, custom_start, custom_end,
                           depth_material, matte_material, matte_sil_material,
                           objid_material, normal_material,
                           near_clip_cm, beauty_label, fog_off, restore_scene, on_done,
                           light_pass=False, light_direct=False,
                           light_label="RawLightingFull", cloud_matte=False,
                           cloud_visible=False, geomask_material=None,
                           backing=False, use_exr=False):
    queue = sub.get_queue()
    for j in list(queue.get_jobs()):
        queue.delete_job(j)
    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.job_name = "UE5Capture_Sequence"
    job.map = unreal.SoftObjectPath(_current_map_softpath())
    job.sequence = unreal.SoftObjectPath(level_sequence.get_path_name())

    cfg = job.get_configuration()
    extra_passes = []
    for pass_name, pass_mat in (("Depth", depth_material), ("Matte", matte_material),
                                ("MatteSil", matte_sil_material),
                                ("ObjectID", objid_material),
                                ("Normal", normal_material),
                                ("GeoMask", geomask_material)):
        if pass_mat is None:
            continue
        ppp = unreal.MoviePipelinePostProcessPass()
        ppp.set_editor_property("enabled", True)
        ppp.set_editor_property("name", pass_name)
        ppp.set_editor_property("material", pass_mat)
        extra_passes.append(ppp)
    # 直射専用ジョブは Beauty(FinalImage) パスを持たない（全フレーム分の捨て出力を
    # レンダしない）。追加 PP 材があるときは搬送役の DeferredPassBase を残す。
    if extra_passes or not light_direct:
        deferred = cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
        if extra_passes:
            deferred.set_editor_property("additional_post_process_materials", extra_passes)
        if cloud_matte:
            deferred.set_editor_property("accumulator_includes_alpha", True)
    if light_pass:
        # LightingOnly は独立したレンダパス（出力 <name>_LightingOnly_take.####、
        # 完了時に _light_label へリネーム）
        cfg.find_or_add_setting_by_class(_lighting_only_class())

    if do_png:
        if use_exr:
            # バッキング差分は線形色の減算が要るため EXR（PNG はトーンマップ後で非線形）
            exr = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_EXR)
            # multilayer だと {render_pass} トークンが空になり W/B ジョブが同名で
            # 上書きし合う（実測）。分割出力で FinalImage 名を出しリネームに乗せる。
            exr.set_editor_property("multilayer", False)
        else:
            png = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
            try:
                png.set_editor_property("write_alpha", bool(cloud_matte))
            except Exception:
                pass
    if do_mp4:
        mp4 = cfg.find_or_add_setting_by_class(unreal.MoviePipelineMP4EncoderOutput)
        mp4.set_editor_property("constant_rate_factor", int(mp4_crf))
        mp4.set_editor_property("include_audio", False)

    out = cfg.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    out.set_editor_property("output_directory", unreal.DirectoryPath(output_dir))
    out.set_editor_property("output_resolution", unreal.IntPoint(int(width), int(height)))
    # フレームレートはシーケンスの Display Rate を明示指定する（既定継承に任せず
    # 24fps 等を確実に反映。エンコード側も同じ値を使う）
    try:
        fr = unreal.MovieSceneSequenceExtensions.get_display_rate(level_sequence)
        out.set_editor_property("use_custom_frame_rate", True)
        out.set_editor_property("output_frame_rate", fr)
        _log("output frame rate = %d/%d" % (fr.numerator, fr.denominator))
    except Exception as e:
        _warn("フレームレート明示指定に失敗（シーケンス既定を使用）: %s" % e)
    # 動画出力側は {frame_number} を自動で外して1ファイルにする（エンジン仕様）
    out.set_editor_property("file_name_format",
                            "%s_{render_pass}_%s.{frame_number}" % (name_body, take_str))
    out.set_editor_property("override_existing_output", True)
    out.set_editor_property("zero_pad_frame_numbers", 4)
    try:
        out.set_editor_property("flush_disk_writes_per_shot", True)
    except Exception:
        pass
    if custom_start is not None and custom_end is not None:
        out.set_editor_property("use_custom_playback_range", True)
        out.set_editor_property("custom_start_frame", int(custom_start))
        out.set_editor_property("custom_end_frame", int(custom_end))

    aa = cfg.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    aa.set_editor_property("override_anti_aliasing", True)
    aa.set_editor_property("spatial_sample_count", 1)
    aa.set_editor_property("temporal_sample_count", int(temporal_samples))
    aa.set_editor_property("engine_warm_up_count", int(warmup))
    aa.set_editor_property("render_warm_up_count", int(warmup))
    try:
        aa.set_editor_property("anti_aliasing_method", unreal.AntiAliasingMethod.AAM_TSR)
    except Exception:
        pass

    go = cfg.find_or_add_setting_by_class(unreal.MoviePipelineGameOverrideSetting)
    go.set_editor_property("use_high_quality_shadows", True)
    go.set_editor_property("use_lod_zero", True)
    go.set_editor_property("flush_grass_streaming", True)
    go.set_editor_property("flush_streaming_managers", True)
    try:
        go.set_editor_property("texture_streaming",
                               unreal.MoviePipelineTextureStreamingMethod.FULLY_LOAD)
    except Exception:
        pass

    # モーションブラーは切らない（切ると 24fps でストロボ状の動きになり、
    # MRQ のテンポラルサンプル蓄積ブラーも効かなくなる）→ _HQ_CVARS に含めていない。
    pairs = list(_HQ_CVARS)
    if (objid_material is not None or matte_material is not None
            or matte_sil_material is not None):
        # CustomDepth ステンシル書き込みには r.CustomDepth=3 が必要
        # （ObjectID の色分けも Matte/MatteSil のステンシル判定も使う）
        pairs.append(("r.CustomDepth", 3))
    if fog_off:
        pairs += [("r.Fog", 0), ("r.VolumetricFog", 0)]
        _log("fog off")
    if light_direct:
        pairs += list(_DIRECT_ONLY_CVARS)
        _log("direct lighting only (GI/Sky/AO off)")
    if cloud_matte:
        # ShowFlag.Cloud は HV も巻き込むため不使用（render_beauty 側の注記参照）
        pairs += [("r.PostProcessing.PropagateAlpha", 1),
                  ("ShowFlag.Atmosphere", 0), ("ShowFlag.Fog", 0),
                  ("ShowFlag.VolumetricFog", 0)]
        _log("cloud matte (alpha / atmosphere+fog off)")
    if backing or cloud_visible:
        # 露出適応は白板に反応して画面全体を沈める（-18%実測）ため、適応と
        # ローカル露出をジョブ内で無効化。白板は発光100なのでブルームと
        # Lumen スクリーントレースの撒き散らしも切る（×100で無視できなくなる）。
        pairs += [("r.EyeAdaptationQuality", 0),
                  ("r.LocalExposure.HighlightContrastScale", 1.0),
                  ("r.LocalExposure.ShadowContrastScale", 1.0),
                  ("r.BloomQuality", 0),
                  ("r.Lumen.ScreenProbeGather.ScreenTraces", 0),
                  ("r.Lumen.Reflections.ScreenTraces", 0)]
        # 既定の EXR はトーンカーブ適用済み（発光100が~1.0に圧縮される実測）。
        # T=W/素板レベル の線形性が前提なのでトーンカーブを切ってシーンリニアで書く。
        try:
            col = cfg.find_or_add_setting_by_class(unreal.MoviePipelineColorSetting)
            col.set_editor_property("disable_tone_curve", True)
        except Exception as e:
            _warn("backing: トーンカーブ無効化に失敗（Tが非線形になる）: %s" % e)
        _log("backing render (exposure locked / bloom+screen traces off / linear)")
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cv.set_editor_property("cvars", _cv_entries(pairs))   # レンダ後にエンジンが自動復元
    cmds = []
    if near_clip_cm is not None:
        # r.SetNearClipPlane は cvar でなくコマンド＝自動復元されない（完了時に手動復元）
        cmds.append("r.SetNearClipPlane %f" % float(near_clip_cm))
        _log("near clip = %.1f cm" % float(near_clip_cm))
        # ウォームアップ/先頭サブフレームにも効かせるため起動前にグローバル適用
        # （半透明ゴースト防止・_on_finished が既定 10cm へ復元）
        unreal.SystemLibrary.execute_console_command(
            _editor_world(), "r.SetNearClipPlane %f" % float(near_clip_cm))
    cv.set_editor_property("start_console_commands", cmds)

    executor = unreal.MoviePipelinePIEExecutor()

    def _on_finished(exec_obj, success):
        _log("MRQ シーケンスレンダ完了 success=%s -> %s" % (success, output_dir))
        restore_scene()             # matte/objid/hidden/auto-play/アスペクトを戻す
        if near_clip_cm is not None:
            # near clip はコマンドでグローバルに残るので必ず既定(10cm)へ戻す
            try:
                unreal.SystemLibrary.execute_console_command(
                    _editor_world(), "r.SetNearClipPlane 10")
            except Exception:
                pass
        _rename_final_image(output_dir, beauty_label,
                            light_label if light_pass else None)   # FinalImage -> Beauty ほか
        _KEEP.clear()
        if on_done:
            try:
                on_done(bool(success), output_dir)
            except Exception as e:
                _warn("on_done でエラー: %s" % e)

    executor.on_executor_finished_delegate.add_callable(_on_finished)
    _KEEP["executor"] = executor
    _KEEP["queue"] = queue

    _log("MRQ シーケンスレンダ開始: %s  %dx%d  TS=%d warmup=%d  PNG=%s MP4=%s(CRF%d)  out=%s"
         % (level_sequence.get_name(), width, height, temporal_samples, warmup,
            do_png, do_mp4, mp4_crf, output_dir))
    sub.render_queue_with_executor_instance(executor)
    return executor


def _rename_final_image(output_dir, beauty_label="Beauty", light_label=None):
    """MRQ のレンダパス名をツールの素材名に揃える。
    追加 PP パスの識別子は "FinalImage"+Name（例 FinalImageDepth）なので、
    長い方を先に置換してから素の FinalImage を beauty_label にする。
    light_label を渡すと LightingOnly レンダパスも _light_label にリネームする。"""
    try:
        for f in os.listdir(output_dir):
            if "_FinalImage" not in f and (light_label is None
                                           or "_LightingOnly" not in f):
                continue
            nf = f
            # MatteSil は Matte より先（長い識別子から置換しないと _MatteSil が壊れる）
            for pass_name in ("Depth", "MatteSil", "Matte", "ObjectID", "Normal",
                              "GeoMask"):
                nf = nf.replace("_FinalImage" + pass_name, "_" + pass_name)
            nf = nf.replace("_FinalImage", "_" + beauty_label)
            if light_label:
                nf = nf.replace("_LightingOnly", "_" + light_label)
            try:
                os.replace(os.path.join(output_dir, f), os.path.join(output_dir, nf))
            except Exception as e:
                _warn("リネーム失敗 %s: %s" % (f, e))
    except Exception as e:
        _warn("FinalImage リネームに失敗: %s" % e)
