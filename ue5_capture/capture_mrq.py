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

from capture_core import MATTE_STENCIL

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
                  light_pass=False, light_direct=False):
    """対象カメラを MRQ で Beauty レンダリング（非同期）。executor を返す。
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
    PP パスで出力する（出力: file_basename_Depth.png）。SceneCapture 別撮りだと
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
        _restore_autoplay_players(saved_players)
        _restore_cameras_aspect(saved_cam)

    try:
        return _start_render(sub, camera_actor, output_dir, width, height,
                             use_exr, image_format, also_png,
                             spatial_samples, temporal_samples, warmup,
                             file_basename, on_done, near_clip_cm, overscan,
                             fog_off, _restore_scene, scene_sequence, scene_frame,
                             matte_material, depth_material,
                             light_pass, light_direct)
    except Exception:
        # 起動に失敗したら状態を巻き戻す（次回レンダを塞がない）
        _restore_scene()
        _delete_temp_sequence()
        _KEEP.clear()
        raise


def _start_render(sub, camera_actor, output_dir, width, height,
                  use_exr, image_format, also_png,
                  spatial_samples, temporal_samples, warmup,
                  file_basename, on_done, near_clip_cm, overscan,
                  fog_off, restore_scene, scene_sequence=None, scene_frame=None,
                  matte_material=None, depth_material=None,
                  light_pass=False, light_direct=False):
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
    for pass_name, pass_mat in (("Matte", matte_material), ("Depth", depth_material)):
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
    if light_pass:
        # LightingOnly は独立したレンダパス（追加 PP 材とは別系統）。
        # 出力は <basename>_LightingOnly.* になる（{render_pass} 命名が必須になる）。
        cfg.find_or_add_setting_by_class(_lighting_only_class())
    # image_format: "png"（既定）/ "jpg" / "exr"。exr のとき also_png=True で
    # PNG も同時出力する（Matte 系合成が PIL で読める画像を必要とするため）。
    fmt = (image_format or ("exr" if use_exr else "png")).lower()
    if fmt == "exr":
        exr_out = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_EXR)
        if light_pass:
            # マルチレイヤ EXR だと LightingOnly が別ファイルにならないため分割する
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
            out_fmt.set_editor_property("write_alpha", False)
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
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cv.set_editor_property("cvars", _cv_entries(pairs))   # レンダ後にエンジンが自動復元
    cmds = []
    if near_clip_cm is not None:
        # マット面までの距離より手前をクリップ（fronto-parallel 近似）。形状は後段でαマスク。
        # r.SetNearClipPlane は cvar でなくコマンド＝自動復元されない（完了時に手動復元）。
        cmds.append("r.SetNearClipPlane %f" % float(near_clip_cm))
        _log("near clip = %.1f cm" % float(near_clip_cm))
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
    """カメラカットに束縛された CineCameraActor を全セクションから解決して返す
    （スポーナブルはエディタワールドに実体が無く解決できない＝既知の限界）。"""
    ext = unreal.MovieSceneSequenceExtensions
    actors = []
    guids = []
    for tr in (ext.find_tracks_by_exact_type(
            level_sequence, unreal.MovieSceneCameraCutTrack) or []):
        for sec in tr.get_sections():
            try:
                guids.append(sec.get_camera_binding_id().get_editor_property("guid"))
            except Exception:
                pass
    for b in ext.get_bindings(level_sequence):
        if any(b.get_id() == g for g in guids):
            for o in ext.locate_bound_objects(level_sequence, b, world):
                if isinstance(o, unreal.CineCameraActor) and o not in actors:
                    actors.append(o)
    return actors


def _fill_aspect_comp(camera_actor, width, height):
    """カメラのアスペクト拘束が出力解像度とミスマッチなら constrain_aspect_ratio を
    False にしてそのコンポーネントを返す（一致 or 非拘束 or 非Cineカメラは None）。"""
    try:
        comp = camera_actor.get_cine_camera_component()
        if not bool(comp.get_editor_property("constrain_aspect_ratio")):
            return None
        fb = comp.get_editor_property("filmback")
        fb_asp = (float(fb.get_editor_property("sensor_width"))
                  / max(float(fb.get_editor_property("sensor_height")), 1e-6))
        if abs(fb_asp - float(width) / float(height)) < 1e-3:
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
        # フォールバック: レベル内の全 CineCamera（ミスマッチのものだけ触り、復元する）
        try:
            actors = unreal.GameplayStatics.get_all_actors_of_class(
                _editor_world(), unreal.CineCameraActor)
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
                    matte_sil_material=None,
                    objid_material=None, objid_actors=None,
                    hidden_actors=None, near_clip_cm=None, beauty_label="Beauty",
                    fog_off=False, on_done=None,
                    light_pass=False, light_direct=False,
                    light_label="RawLightingFull"):
    """開いている/指定の LevelSequence を MRQ でレンダリング（非同期）。
    一時シーケンスは作らず job.sequence に直接指定し、カメラはシーケンスの
    カメラカットトラックに従う。fps はシーケンスの Display Rate。
    静止画と違いモーションブラーは殺さない（切ると動きがストロボ状になる）。
    do_png=PNG連番 / do_mp4=内蔵 H.264 MP4（CRF 指定・音声なし）。両方同時可。
    depth_material / matte_material を渡すと additional_post_process_materials で
    パスが増え、パス毎に別ファイルで出力される。matte_material / matte_sil_material
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
    hidden = []
    if matte_material is not None or matte_sil_material is not None:
        saved_matte = _set_matte_render_mode(matte_actors)
    elif hidden_actors:
        for a in hidden_actors:
            try:
                a.set_actor_hidden_in_game(True)
                hidden.append(a)
            except Exception:
                pass
    if objid_material is not None:
        saved_objid = _set_objid_render_mode(objid_actors)
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
                                      objid_material,
                                      near_clip_cm, beauty_label, fog_off,
                                      _restore_scene, on_done,
                                      light_pass, light_direct, light_label)
    except Exception:
        _restore_scene()
        _KEEP.clear()      # 起動失敗時に次回レンダを塞がない
        raise


def _start_sequence_render(sub, level_sequence, output_dir, width, height,
                           name_body, take_str, do_png, do_mp4, mp4_crf,
                           temporal_samples, warmup, custom_start, custom_end,
                           depth_material, matte_material, matte_sil_material,
                           objid_material,
                           near_clip_cm, beauty_label, fog_off, restore_scene, on_done,
                           light_pass=False, light_direct=False,
                           light_label="RawLightingFull"):
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
                                ("ObjectID", objid_material)):
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
    if light_pass:
        # LightingOnly は独立したレンダパス（出力 <name>_LightingOnly_take.####、
        # 完了時に _light_label へリネーム）
        cfg.find_or_add_setting_by_class(_lighting_only_class())

    if do_png:
        png = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
        try:
            png.set_editor_property("write_alpha", False)
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
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cv.set_editor_property("cvars", _cv_entries(pairs))   # レンダ後にエンジンが自動復元
    cmds = []
    if near_clip_cm is not None:
        # r.SetNearClipPlane は cvar でなくコマンド＝自動復元されない（完了時に手動復元）
        cmds.append("r.SetNearClipPlane %f" % float(near_clip_cm))
        _log("near clip = %.1f cm" % float(near_clip_cm))
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
            for pass_name in ("Depth", "MatteSil", "Matte", "ObjectID"):
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
