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
                  near_clip_cm=None):
    """対象カメラを MRQ で Beauty レンダリング（非同期）。executor を返す。
    hidden_actors を渡すと、そのアクターを非表示にしてレンダ（Beauty 品質のクリーンプレート）。
    near_clip_cm を渡すと、その距離(cm)より手前を描画時クリップする（fronto-parallel 近似の behind-matte）。
    出力は output_dir 直下に file_basename.png (or .exr)。完了時 on_done(success, out_dir) を呼ぶ。"""
    output_dir = os.path.normpath(output_dir)
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # 対象を非表示（レンダ後に復元）
    restore = []
    if hidden_actors:
        for a in hidden_actors:
            try:
                a.set_actor_hidden_in_game(True)
                restore.append(a)
            except Exception:
                pass

    seq, seq_path = _create_temp_sequence(camera_actor)

    sub = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
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
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cv.set_editor_property("start_console_commands", cmds)

    executor = unreal.MoviePipelinePIEExecutor()

    def _on_finished(exec_obj, success):
        _log("MRQ レンダ完了 success=%s -> %s" % (success, output_dir))
        _delete_temp_sequence()
        for a in restore:                       # 非表示にしたアクターを元に戻す
            try:
                a.set_actor_hidden_in_game(False)
            except Exception:
                pass
        if near_clip_cm is not None:
            # near clip はグローバルに残るので必ず既定(10cm)へ戻す（ビューポート破壊防止）
            try:
                w = _editor_world()
                unreal.SystemLibrary.execute_console_command(w, "r.SetNearClipPlane 10")
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
