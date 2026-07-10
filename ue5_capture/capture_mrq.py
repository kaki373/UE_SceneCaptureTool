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


def _create_temp_sequence(camera_actor, fps=24):
    """対象カメラを 1 フレームのカメラカットにした一時 LevelSequence を作成して返す。"""
    ext = unreal.MovieSceneSequenceExtensions
    _delete_temp_sequence()
    at = unreal.AssetToolsHelpers.get_asset_tools()
    seq = at.create_asset(_TMP_NAME, _TMP_PKG, unreal.LevelSequence,
                          unreal.LevelSequenceFactoryNew())
    if seq is None:
        raise RuntimeError("一時 LevelSequence の生成に失敗しました。")
    ext.set_display_rate(seq, unreal.FrameRate(int(fps), 1))
    ext.set_playback_start(seq, 0)
    ext.set_playback_end(seq, 1)            # 1 フレーム
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


# 影/GI を高品質にするコンソールコマンド（シーケンサ書き出し相当）
_HQ_CONSOLE = [
    "r.MotionBlurQuality 0",
    "r.Shadow.Virtual.ResolutionLodBiasDirectional -1.5",
    "r.Shadow.Virtual.ResolutionLodBiasLocal -1.5",
    "r.Shadow.Virtual.SMRT.RayCountDirectional 16",
    "r.Shadow.Virtual.SMRT.SamplesPerRayDirectional 8",
    "r.Lumen.ScreenProbeGather.RadianceCache.NumProbesToTraceBudget 600",
    "r.Lumen.ScreenProbeGather.TraceMeshSDFs 1",
    "r.Lumen.Reflections.Quality 4",
    "r.TextureStreaming 0",
]


def render_beauty(camera_actor, output_dir, width, height,
                  use_exr=False, spatial_samples=1, temporal_samples=8,
                  warmup=32, file_basename="beauty", hidden_actors=None, on_done=None,
                  near_clip_cm=None, overscan=0.0, fog_off=False):
    """対象カメラを MRQ で Beauty レンダリング（非同期）。executor を返す。
    hidden_actors を渡すと、そのアクターを非表示にしてレンダ（Beauty 品質のクリーンプレート）。
    near_clip_cm を渡すと、その距離(cm)より手前を描画時クリップする（fronto-parallel 近似の behind-matte）。
    出力は output_dir 直下に file_basename.png (or .exr)。完了時 on_done(success, out_dir) を呼ぶ。"""
    output_dir = os.path.normpath(output_dir)
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # 多重起動防止: 既に MRQ レンダ中なら弾く（PIE 衝突で破損 PNG が出るのを防ぐ）
    sub = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    if sub.is_rendering() or _KEEP.get("executor") is not None:
        raise RuntimeError("MRQ は既にレンダリング中です。完了までお待ちください（多重起動防止）。")

    # 対象を非表示（レンダ後に復元）
    restore = []
    if hidden_actors:
        for a in hidden_actors:
            try:
                a.set_actor_hidden_in_game(True)
                restore.append(a)
            except Exception:
                pass

    def _restore_hidden():
        for a in restore:
            try:
                a.set_actor_hidden_in_game(False)
            except Exception:
                pass

    try:
        return _start_render(sub, camera_actor, output_dir, width, height,
                             use_exr, spatial_samples, temporal_samples, warmup,
                             file_basename, on_done, near_clip_cm, overscan,
                             fog_off, _restore_hidden)
    except Exception:
        # 起動に失敗したら状態を巻き戻す（非表示を残さない・次回レンダを塞がない）
        _restore_hidden()
        _delete_temp_sequence()
        _KEEP.clear()
        raise


def _start_render(sub, camera_actor, output_dir, width, height,
                  use_exr, spatial_samples, temporal_samples, warmup,
                  file_basename, on_done, near_clip_cm, overscan,
                  fog_off, restore_hidden):
    seq, seq_path = _create_temp_sequence(camera_actor)

    queue = sub.get_queue()
    for j in list(queue.get_jobs()):
        queue.delete_job(j)
    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.job_name = "UE5Capture_Beauty"
    job.map = unreal.SoftObjectPath(_current_map_softpath())
    job.sequence = unreal.SoftObjectPath(seq_path)

    cfg = job.get_configuration()
    cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
    if use_exr:
        out_fmt = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_EXR)
    else:
        out_fmt = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
        try:
            out_fmt.set_editor_property("write_alpha", False)
        except Exception:
            pass

    out = cfg.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    out.set_editor_property("output_directory", unreal.DirectoryPath(output_dir))
    out.set_editor_property("output_resolution", unreal.IntPoint(int(width), int(height)))
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

    cmds = list(_HQ_CONSOLE)
    if near_clip_cm is not None:
        # マット面までの距離より手前をクリップ（fronto-parallel 近似）。形状は後段でαマスク。
        cmds.append("r.SetNearClipPlane %f" % float(near_clip_cm))
        _log("near clip = %.1f cm" % float(near_clip_cm))
    if fog_off:
        cmds += ["r.Fog 0", "r.VolumetricFog 0"]   # Fog を OFF にして書き出す
        _log("fog off")
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cv.set_editor_property("start_console_commands", cmds)

    executor = unreal.MoviePipelinePIEExecutor()

    def _on_finished(exec_obj, success):
        _log("MRQ レンダ完了 success=%s -> %s" % (success, output_dir))
        _delete_temp_sequence()
        restore_hidden()                        # 非表示にしたアクターを元に戻す
        if near_clip_cm is not None:
            # near clip はグローバルに残るので必ず既定(10cm)へ戻す（ビューポート破壊防止）
            try:
                w = _editor_world()
                unreal.SystemLibrary.execute_console_command(w, "r.SetNearClipPlane 10")
            except Exception:
                pass
        if fog_off:
            # r.Fog/r.VolumetricFog もグローバルに残るので必ず ON へ戻す
            try:
                w = _editor_world()
                unreal.SystemLibrary.execute_console_command(w, "r.Fog 1")
                unreal.SystemLibrary.execute_console_command(w, "r.VolumetricFog 1")
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
def render_sequence(level_sequence, output_dir, width, height, name_body, take_str,
                    do_png=True, do_mp4=False, mp4_crf=20,
                    temporal_samples=8, warmup=32,
                    custom_start=None, custom_end=None,
                    depth_material=None, fog_off=False, on_done=None):
    """開いている/指定の LevelSequence を MRQ でレンダリング（非同期）。
    一時シーケンスは作らず job.sequence に直接指定し、カメラはシーケンスの
    カメラカットトラックに従う。fps はシーケンスの Display Rate。
    do_png=PNG連番 / do_mp4=内蔵 H.264 MP4（CRF 指定・音声なし）。両方同時可。
    depth_material を渡すと additional_post_process_materials で Depth パスが増え、
    パス毎に別ファイルで出力される（動画はフレーム番号が自動で外れて1本になる）。
    出力名: name_body_{render_pass}_take.{frame_number} 。レンダパス名 FinalImage は
    完了時に Beauty へリネームする。custom_start/custom_end は上書き範囲（end 排他）。"""
    output_dir = os.path.normpath(output_dir)
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    if not (do_png or do_mp4):
        raise RuntimeError("PNG連番 / MP4 のどちらも選ばれていません。")

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

    try:
        return _start_sequence_render(sub, level_sequence, output_dir, width, height,
                                      name_body, take_str, do_png, do_mp4, mp4_crf,
                                      temporal_samples, warmup, custom_start, custom_end,
                                      depth_material, fog_off, on_done)
    except Exception:
        _KEEP.clear()      # 起動失敗時に次回レンダを塞がない
        raise


def _start_sequence_render(sub, level_sequence, output_dir, width, height,
                           name_body, take_str, do_png, do_mp4, mp4_crf,
                           temporal_samples, warmup, custom_start, custom_end,
                           depth_material, fog_off, on_done):
    queue = sub.get_queue()
    for j in list(queue.get_jobs()):
        queue.delete_job(j)
    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.job_name = "UE5Capture_Sequence"
    job.map = unreal.SoftObjectPath(_current_map_softpath())
    job.sequence = unreal.SoftObjectPath(level_sequence.get_path_name())

    cfg = job.get_configuration()
    deferred = cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
    if depth_material is not None:
        ppp = unreal.MoviePipelinePostProcessPass()
        ppp.set_editor_property("enabled", True)
        ppp.set_editor_property("name", "Depth")
        ppp.set_editor_property("material", depth_material)
        deferred.set_editor_property("additional_post_process_materials", [ppp])

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

    cmds = list(_HQ_CONSOLE)
    if fog_off:
        cmds += ["r.Fog 0", "r.VolumetricFog 0"]
        _log("fog off")
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cv.set_editor_property("start_console_commands", cmds)

    executor = unreal.MoviePipelinePIEExecutor()

    def _on_finished(exec_obj, success):
        _log("MRQ シーケンスレンダ完了 success=%s -> %s" % (success, output_dir))
        if fog_off:
            try:
                w = _editor_world()
                unreal.SystemLibrary.execute_console_command(w, "r.Fog 1")
                unreal.SystemLibrary.execute_console_command(w, "r.VolumetricFog 1")
            except Exception:
                pass
        _rename_final_image(output_dir)     # FinalImage -> Beauty
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


def _rename_final_image(output_dir):
    """MRQ のレンダパス名をツールの素材名に揃える。
    追加 PP パスの識別子は "FinalImage"+Name（例 FinalImageDepth）なので、
    長い方を先に置換してから素の FinalImage を Beauty にする。"""
    try:
        for f in os.listdir(output_dir):
            if "_FinalImage" not in f:
                continue
            nf = f.replace("_FinalImageDepth", "_Depth").replace("_FinalImage", "_Beauty")
            try:
                os.replace(os.path.join(output_dir, f), os.path.join(output_dir, nf))
            except Exception as e:
                _warn("リネーム失敗 %s: %s" % (f, e))
    except Exception as e:
        _warn("FinalImage リネームに失敗: %s" % e)
