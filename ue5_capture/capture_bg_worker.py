# -*- coding: utf-8 -*-
"""
capture_bg_worker.py  --  background render worker (runs inside the offscreen worker editor)

Started by capture_bg.launch_worker as `UnrealEditor <uproject> -RenderOffScreen ... -ExecCmds="py
<this>"`. Does nothing unless the command line carries capture_bg.WORKER_MARKER.

Per job (oldest first): load the job's map if needed → open its LevelSequence in Sequencer
(spawnable cameras only exist then) → build a hidden (never shown) capture panel from the job's
settings snapshot → run the panel's own sequence handler → judge the result from the handler's
outcome and the frame files on disk → next job. Exits by itself when the queue is empty.
Never saves maps/assets, never writes the user's panel settings or ini files (-nowrite),
autosave and the background CPU throttle are disabled in memory for this process only.
"""

import gc
import os
import re
import sys
import time
import traceback

import unreal

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import capture_bg as bg

_TAG = "[SceneCapture/BG] "
# Settle time after a map load / opening the sequence before the job starts (streaming, spawnables).
_SETTLE_SEC = 5.0
# The worker's own helper-asset folder: the user's editor may run captures at the same time and
# creates/saves/deletes the same M_UE5Cap_* packages under /Game/_UE5Capture_Tmp. Removed on exit.
_BG_TMP_PKG = "/Game/_UE5Capture_Tmp/BG"
_W = {}             # worker state (module global; the tick callback closes over it)


def _log(m):
    unreal.log(_TAG + str(m))
    jl = _W.get("job_log")
    if jl:
        try:
            with open(jl, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%H:%M:%S"), m))
        except OSError:
            pass


def _set_cdo(path, prop, value):
    """Setting on a config CDO in memory only (notify NEVER → no PostEditChange → no SaveConfig)."""
    try:
        cdo = unreal.find_object(None, path)
        cdo.set_editor_property(prop, value, unreal.PropertyAccessChangeNotifyMode.NEVER)
        return True
    except Exception as e:
        unreal.log_warning(_TAG + "%s.%s not set: %s" % (path, prop, e))
        return False


def _use_own_tmp_pkg():
    import capture_core
    import capture_mrq
    capture_core._TMP_MAT_PKG = _BG_TMP_PKG
    capture_mrq._TMP_PKG = _BG_TMP_PKG


def _quit():
    _log("queue empty -> exit")
    _drop_window()
    unreal.SystemLibrary.collect_garbage()
    try:
        if unreal.EditorAssetLibrary.does_directory_exist(_BG_TMP_PKG):
            ok = unreal.EditorAssetLibrary.delete_directory(_BG_TMP_PKG)
            _log("helper assets %s removed: %s" % (_BG_TMP_PKG, ok))
    except Exception as e:
        _log("helper assets %s not removed: %s" % (_BG_TMP_PKG, e))
    bg.release_lock(_W["q"], os.getpid())
    try:
        unreal.unregister_slate_post_tick_callback(_W["h"])
    except Exception:
        pass
    unreal.SystemLibrary.quit_editor()


def _drop_window():
    """Releases the hidden panel (it holds actor references; keeping them across a map load
    trips the editor's World Memory Leaks check)."""
    _W.pop("win", None)
    gc.collect()


def _expected_frames(job):
    st = job.get("settings") or {}
    if st.get("seq_range_mode") == "custom":
        try:
            return int(st.get("seq_end")) - int(st.get("seq_start")) + 1
        except (TypeError, ValueError):
            return None
    try:
        seq = unreal.EditorAssetLibrary.load_asset(job["sequence"])
        ext = unreal.MovieSceneSequenceExtensions
        return int(ext.get_playback_end(seq)) - int(ext.get_playback_start(seq))
    except Exception:
        return None


def _judge(job, status_text, t0):
    """Success needs the handler's own completion message AND files on disk: every pass whose
    image sequence was requested has exactly the expected number of new frames (MP4-only passes:
    a new .mp4 with the pass name)."""
    import capture_ui
    st = job.get("settings") or {}
    ok_msg = status_text.startswith("シーケンスレンダ完了")
    m = re.match(r"シーケンスレンダ完了: (.+?)(?:（|$)", status_text)
    out = m.group(1).strip() if m else (st.get("seq_out") or "")
    new = []
    if out and os.path.isdir(out):
        for root, _d, files in os.walk(out):
            for f in files:
                try:
                    if os.path.getmtime(os.path.join(root, f)) >= t0:
                        new.append(f)
                except OSError:
                    pass
    expected = _expected_frames(job)
    wants = st.get("seq_outputs") or {}
    frames, ok_pass = {}, True
    for key, _label, pass_name in capture_ui._SEQ_OUTPUTS:
        png, mp4 = (list(wants.get(key) or [False, False]) + [False, False])[:2]
        tag = "_%s_" % pass_name
        if png:
            n = sum(1 for f in new if tag in f and f.lower().endswith((".png", ".jpg", ".exr")))
            frames[pass_name] = n
            ok_pass = ok_pass and (expected is None or n == expected) and n > 0
        if mp4 and not any(tag in f and f.lower().endswith(".mp4") for f in new):
            frames[pass_name + ".mp4"] = 0
            ok_pass = False
    ok = ok_msg and bool(new) and ok_pass
    return ok, {"output_dir": out, "new_files": len(new), "frames": frames,
                "beauty_frames": frames.get("Beauty"), "expected_frames": expected,
                "status": status_text}


def _start_job():
    import capture_ui
    import capture_mrq
    job = _W["job"]
    q = _W["q"]
    # stale busy state guard: _KEEP left over from an earlier chain blocks render_sequence
    capture_mrq.force_reset()
    unreal._ue5capture_busy = False
    _use_own_tmp_pkg()
    settings_file = os.path.join(bg.logs_dir(q), job["id"] + "_settings.json")
    bg._write_json(settings_file, job.get("settings") or {})
    _W["win"] = capture_ui.CaptureWindow(headless=True, settings_path=settings_file)
    if (job.get("settings") or {}).get("seq_viewport_look"):
        _log("ビューポートのルック: MRQ 近似（ワーカーにはパイロット中のビューポートが無いのでカメラのポスプロは外す）")
    _W["t_job"] = time.time()
    _W["win"]._on_seq_render()
    if not capture_ui.CaptureWindow._is_busy(_W["win"]):
        _finish(False)              # the handler refused to start (status text says why)
        return
    _W["phase"] = "wait"


def _finish(started):
    q, job = _W["q"], _W["job"]
    status_text = ""
    try:
        status_text = _W["win"].status_var.get()
    except Exception:
        pass
    ok, ev = _judge(job, status_text, _W.get("t_job", 0.0))
    if bg.cancel_requested(q, job["id"]):
        state, msg = "cancelled", "中断しました"
    else:
        state, msg = ("done" if ok else "failed"), status_text
    bg.update_job(q, job["id"], state=state, finished=time.time(), message=msg, result=ev,
                  log=_W.get("job_log"))
    _log("job %s -> %s (%s)" % (job["id"], state, ev))
    _W["job"] = None
    _W["job_log"] = None
    _drop_window()
    _W["phase"] = "pick"


def _tick(dt):
    # load_level / open_editor_for_assets / the handler pump Slate ticks: never re-enter
    if _W.get("in_tick"):
        return
    _W["in_tick"] = True
    try:
        _tick_body()
    finally:
        _W["in_tick"] = False


def _tick_body():
    try:
        ph = _W["phase"]
        now = time.time()
        q = _W["q"]
        if ph == "boot":
            if now - _W["t0"] < _SETTLE_SEC:
                return
            if unreal.AssetRegistryHelpers.get_asset_registry().is_loading_assets():
                return
            _W["phase"] = "pick"
        elif ph == "pick":
            job = bg.take_next(q, os.getpid())
            if job is None:
                _quit()
                _W["phase"] = "exit"
                return
            rec = bg.read_launch(q) or {}
            if int(rec.get("pid") or 0) == os.getpid():
                bg.note_launch(q, jobs_taken=int(rec.get("jobs_taken") or 0) + 1)
            _W["job"] = job
            _W["job_log"] = os.path.join(bg.logs_dir(q), job["id"] + ".log")
            _log("job %s start: %s map=%s seq=%s" % (job["id"], job.get("name"), job.get("map"),
                                                     job.get("sequence")))
            if bg.cancel_requested(q, job["id"]):
                bg.update_job(q, job["id"], state="cancelled", finished=now, message="中断しました")
                _W["phase"] = "pick"
                return
            world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
            cur = world.get_path_name().split(".")[0] if world else ""
            del world
            if cur != job["map"]:
                _drop_window()
                unreal.SystemLibrary.collect_garbage()
                if not unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level(job["map"]):
                    bg.update_job(q, job["id"], state="failed", finished=time.time(),
                                  message="レベルを開けません: %s" % job["map"])
                    _W["phase"] = "pick"
                    return
            seq = unreal.EditorAssetLibrary.load_asset(job["sequence"])
            if seq is None:
                bg.update_job(q, job["id"], state="failed", finished=time.time(),
                              message="シーケンスが見つかりません: %s" % job["sequence"])
                _W["phase"] = "pick"
                return
            unreal.get_editor_subsystem(unreal.AssetEditorSubsystem).open_editor_for_assets([seq])
            del seq
            _W["t_settle"] = now
            _W["phase"] = "settle"
        elif ph == "settle":
            if now - _W["t_settle"] < _SETTLE_SEC:
                return
            if unreal.AssetRegistryHelpers.get_asset_registry().is_loading_assets():
                return
            _W["phase"] = "start"
            _start_job()
        elif ph == "wait":
            if bg.cancel_requested(q, _W["job"]["id"]):
                import capture_mrq
                ex = capture_mrq._KEEP.get("executor")
                if ex is not None and ex is not _W.get("cancelled_ex"):
                    _log("cancel: stopping the running MRQ job")
                    _W["cancelled_ex"] = ex
                    try:
                        ex.cancel_all_jobs()
                    except Exception as e:
                        _log("cancel failed: %s" % e)
            if not getattr(unreal, "_ue5capture_busy", False):
                _finish(True)
    except Exception:
        err = traceback.format_exc()
        unreal.log_error(_TAG + err)
        if _W.get("job"):
            bg.update_job(_W["q"], _W["job"]["id"], state="failed", finished=time.time(),
                          message="ワーカー例外: %s" % err.strip().splitlines()[-1])
            _W["job"] = None
        _drop_window()
        unreal._ue5capture_busy = False
        _W["phase"] = "pick"


def _main():
    cl = unreal.SystemLibrary.get_command_line()
    if bg.WORKER_MARKER.lower() not in cl.lower():
        return
    saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
    q = bg.queue_dir(saved)
    project = unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())
    _W.update(q=q, phase="boot", t0=time.time(), job=None)
    if not bg.acquire_lock(q, os.getpid(), project):
        unreal.log_warning(_TAG + "another worker owns the queue -> exit")
        unreal.SystemLibrary.quit_editor()
        return
    rec = bg.read_launch(q) or {}
    if int(rec.get("pid") or 0) == os.getpid():
        bg.note_launch(q, acquired=True)
    # this process only, in memory: no autosave (it would overwrite the user's Autosaves),
    # no background throttle (an offscreen editor is never foreground)
    _set_cdo("/Script/UnrealEd.Default__EditorLoadingSavingSettings", "bAutoSaveEnable", False)
    _set_cdo("/Script/UnrealEd.Default__EditorPerformanceSettings",
             "bThrottleCPUWhenNotForeground", False)
    _log("worker pid %d owns %s" % (os.getpid(), q))
    _W["h"] = unreal.register_slate_post_tick_callback(_tick)


_main()
