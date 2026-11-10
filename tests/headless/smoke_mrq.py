# Headless smoke test of the UE_capture tool modules on UE 5.7 / 5.8 (throwaway project only).
import os, sys, json, traceback, shutil
import unreal

SP = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(os.path.dirname(SP)), "ue5_capture")
OUTD = os.environ.get("UE_CAPTURE_SMOKE_OUT") or SP
os.makedirs(OUTD, exist_ok=True)
RES = []


def P(msg):
    print("[SMOKE] " + msg)


def step(name, fn):
    try:
        r = fn()
        RES.append((name, "OK", repr(r)[:400]))
        P("OK   %s -> %s" % (name, repr(r)[:400]))
        return r
    except Exception as e:
        RES.append((name, "FAIL", "%s: %s" % (type(e).__name__, e)))
        P("FAIL %s -> %s: %s" % (name, type(e).__name__, e))
        for line in traceback.format_exc().splitlines():
            P("     " + line)
        return None


ver = unreal.SystemLibrary.get_engine_version()
VSHORT = ver.split("-")[0]
OUT = os.path.join(OUTD, "smoke_mrq_out_%s" % VSHORT)
P("engine=%s python=%s" % (ver, sys.version.split()[0]))
try:
    import numpy, PIL  # noqa
    P("numpy/PIL from engine site-packages")
except ImportError:
    _site = os.environ.get("UE_CAPTURE_SMOKE_SITE")
    if not _site:
        raise ImportError("numpy/Pillow missing in engine Python: run Install.bat or set UE_CAPTURE_SMOKE_SITE")
    sys.path.insert(0, _site)
    import numpy, PIL  # noqa
    P("numpy/PIL missing in engine Python -> using UE_CAPTURE_SMOKE_SITE=" + _site)
if TOOL not in sys.path:
    sys.path.insert(0, TOOL)

core = step("import capture_core", lambda: __import__("capture_core"))
mrq = step("import capture_mrq", lambda: __import__("capture_mrq"))
ui = step("import capture_ui", lambda: __import__("capture_ui"))
step("import capture_menu (registers menus)", lambda: __import__("capture_menu") and "imported")

step("core._HAS_NUMPY/_HAS_PIL", lambda: (core._HAS_NUMPY, core._HAS_PIL))
step("engine version (SystemLibrary)", lambda: unreal.SystemLibrary.get_engine_version())
step("find_ffmpeg()", core.find_ffmpeg)


def _take():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    open(os.path.join(OUT, "Cam_Beauty_004_x.png"), "w").close()
    os.makedirs(os.path.join(OUT, "Seq_007"))
    return core.next_take_number(OUT)


step("next_take_number (expect 8)", _take)

# --- level content -------------------------------------------------------
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
step("editor world", lambda: core._get_editor_world().get_path_name())
step("list_cameras() on empty level (expect [])",
     lambda: [c.get_actor_label() for c in core.list_cameras()])


def _spawn():
    cube = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(500, 0, 0))
    cube.static_mesh_component.set_static_mesh(
        unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube"))
    cube.set_actor_label("SmokeCube")
    cam = eas.spawn_actor_from_class(unreal.CineCameraActor, unreal.Vector(0, 0, 0))
    cam.set_actor_label("SmokeCam")
    sun = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 300))
    return cube, cam, sun


cube, cam, sun = step("spawn cube + CineCamera + DirectionalLight", _spawn) or (None, None, None)
step("list_cameras() after spawn (expect ['SmokeCam'])",
     lambda: [c.get_actor_label() for c in core.list_cameras()])
step("get_camera_settings",
     lambda: {k: v for k, v in core.get_camera_settings(cam).items() if k != "transform"})
step("is_volumetric_actor(cube)", lambda: core.is_volumetric_actor(cube))
step("level_has_volumetrics()", core.level_has_volumetrics)


# --- render targets ----------------------------------------------------
def _rt():
    w = core._get_editor_world()
    out = {}
    for f in ("RTF_RGBA32F", "RTF_RGBA16F", "RTF_RGBA8"):
        rt = core._make_render_target(w, 64, 32, getattr(unreal.TextureRenderTargetFormat, f), True)
        px = unreal.RenderingLibrary.read_render_target_raw(w, rt, False)
        out[f] = (str(rt.get_editor_property("render_target_format")), len(px))
    return out


step("create_render_target2d + read_render_target_raw", _rt)


# --- temp PP materials (create -> exists -> delete) --------------------
def _mats():
    out = {}
    makers = [
        ("depth", lambda: core.create_temp_depth_material(0.0, 10000.0), core.delete_temp_depth_material),
        ("normal", core.create_temp_normal_material, core.delete_temp_normal_material),
        ("geomask", core.create_temp_geomask_material, core.delete_temp_geomask_material),
        ("matte", core.create_temp_matte_material, core.delete_temp_matte_material),
        ("matte_sil", core.create_temp_matte_sil_material, core.delete_temp_matte_sil_material),
        ("objid", core.create_temp_objid_material, core.delete_temp_objid_material),
        ("sky_black", core.get_or_create_sky_black_material, core.delete_temp_sky_black_material),
        ("matteboard", core.get_or_create_matteboard_material, None),
        ("backing_clear", core.get_or_create_backing_clear_material, None),
        ("backing_white", core.get_or_create_backing_white_material, None),
    ]
    for name, mk, rm in makers:
        m = mk()
        path = m.get_path_name() if m else None
        n_expr = unreal.MaterialEditingLibrary.get_num_material_expressions(m) if m else -1
        out[name] = [path, n_expr]
        if rm:
            rm()
            out[name].append("deleted" if not unreal.EditorAssetLibrary.does_asset_exist(
                path.split(".")[0]) else "STILL-EXISTS")
    return out


step("temp PP materials create/delete", _mats)

# --- SceneCapture passes (need rendering) ------------------------------
step("measure_depth_range(cam)", lambda: core.measure_depth_range(cam))


def _run_capture():
    s = core.CaptureSettings()
    s.camera_actor = cam
    s.output_dir = OUT
    s.base_height = 216
    s.aa_factor = 1
    s.do_color = False
    s.do_depth = True
    s.do_matte = True
    s.do_object_id = True
    s.matte_actors = [cube]
    s.objid_actors = [cube]
    s.depth_near = 100.0
    s.depth_far = 2000.0
    s.take_suffix = "001"
    return [os.path.basename(o) for o in core.run_capture(s)]


step("run_capture depth+matte+objid", _run_capture)


def _png_stats():
    from PIL import Image
    import numpy as np
    r = {}
    for f in sorted(os.listdir(OUT)):
        if f.endswith(".png") and "_001" in f:
            a = np.asarray(Image.open(os.path.join(OUT, f)))
            r[f] = (a.shape, str(a.dtype), float(a.min()), float(a.max()), round(float(a.mean()), 2))
    return r


step("output PNG stats", _png_stats)


# --- MRQ job build (no render: executor launch is intercepted) ---------
class _FakeSub(object):
    def __init__(self, real):
        self.real = real
        self.launched = None

    def get_queue(self):
        return self.real.get_queue()

    def is_rendering(self):
        return False

    def render_queue_with_executor_instance(self, ex):
        self.launched = ex


def _job_summary(job):
    cfg = job.get_configuration()
    names = sorted(type(s).__name__ for s in cfg.get_all_settings())
    cv = cfg.find_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    cvars = [(str(e.get_editor_property("name")), e.get_editor_property("value"))
             for e in cv.get_editor_property("cvars")] if cv else []
    mode = str(job.get_config_mode()) if hasattr(job, "get_config_mode") else "(no config modes)"
    return {"config_mode": mode, "settings": names, "n_cvars": len(cvars), "cvars_head": cvars[:3]}


def _mrq_beauty():
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    fs = _FakeSub(real)
    mats = dict(matte_material=core.create_temp_matte_material(),
                depth_material=core.create_temp_depth_material(0.0, 10000.0),
                normal_material=core.create_temp_normal_material())
    try:
        mrq._start_render(fs, cam, os.path.join(OUTD, "smoke_mrq"), 640, 360,
                          False, "png", False, 1, 4, 8, "Smoke", None, None, 0.0,
                          True, None, **mats)
        job = real.get_queue().get_jobs()[0]
        r = _job_summary(job)
        r["executor"] = type(fs.launched).__name__
        r["temp_seq_exists"] = unreal.EditorAssetLibrary.does_asset_exist(
            mrq._TMP_PKG + "/" + mrq._TMP_NAME)
        return r
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
        mrq._delete_temp_sequence()
        core.delete_temp_matte_material()
        core.delete_temp_depth_material()
        core.delete_temp_normal_material()


step("MRQ _start_render job build (Beauty, render intercepted)", _mrq_beauty)


def _make_scene_seq():
    at = unreal.AssetToolsHelpers.get_asset_tools()
    p = "/Game/SmokeSeq"
    if unreal.EditorAssetLibrary.does_asset_exist(p + "/SEQ_Smoke"):
        unreal.EditorAssetLibrary.delete_asset(p + "/SEQ_Smoke")
    seq = at.create_asset("SEQ_Smoke", p, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    ext = unreal.MovieSceneSequenceExtensions
    ext.set_display_rate(seq, unreal.FrameRate(24, 1))
    ext.set_playback_start(seq, 0)
    ext.set_playback_end(seq, 48)
    b = ext.add_possessable(seq, cam)
    cct = ext.add_track(seq, unreal.MovieSceneCameraCutTrack)
    sec = cct.add_section()
    sec.set_range(0, 48)
    sec.set_camera_binding_id(ext.get_binding_id(seq, b))
    unreal.EditorAssetLibrary.save_loaded_asset(seq)
    return seq


scene_seq = step("create scene LevelSequence (48f, camera cut)", _make_scene_seq)


def _temp_seq_frozen():
    seq, full = mrq._create_temp_sequence(cam, scene_sequence=scene_seq, scene_frame=12)
    ext = unreal.MovieSceneSequenceExtensions
    subs = ext.find_tracks_by_exact_type(seq, unreal.MovieSceneSubTrack)
    sec = subs[0].get_sections()[0]
    params = sec.get_editor_property("parameters")
    tw = params.get_editor_property("time_scale")
    r = {"path": full, "time_scale_export": tw.export_text(),
         "start_frame_offset": params.get_editor_property("start_frame_offset").value}
    try:
        r["fixed_play_rate"] = tw.to_fixed_play_rate()
    except Exception as e:
        r["fixed_play_rate"] = "ERR %s" % e
    # standalone probe: does set_fixed_play_rate stick on a fresh variant?
    v = unreal.MovieSceneTimeWarpVariant()
    r["fresh_default_export"] = v.export_text()
    v.set_fixed_play_rate(0.0)
    r["fresh_after_set0_export"] = v.export_text()
    v2 = unreal.MovieSceneTimeWarpVariant()
    v2.set_fixed_play_rate(0.5)
    r["fresh_after_set05_export"] = v2.export_text()
    try:
        r["fresh_after_set05_to_fixed"] = v2.to_fixed_play_rate()
    except Exception as e:
        r["fresh_after_set05_to_fixed"] = "ERR %s" % e
    mrq._delete_temp_sequence()
    return r


step("_create_temp_sequence with TimeWarp freeze (expect rate 0.0)", _temp_seq_frozen)
step("_camera_cut_camera_actors(scene_seq) (Guid export_text compare)",
     lambda: [a.get_actor_label() for a in mrq._camera_cut_camera_actors(scene_seq, core._get_editor_world())])
step("get_display_rate(scene_seq)",
     lambda: str(unreal.MovieSceneSequenceExtensions.get_display_rate(scene_seq)))


def _mrq_seq():
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    fs = _FakeSub(real)
    try:
        mrq._start_sequence_render(fs, scene_seq, os.path.join(OUTD, "smoke_mrq_seq"), 640, 360,
                                   "Smoke", "001", True, True, 18, 4, 8, None, None,
                                   None, None, None, None, None,
                                   None, "Beauty", False, None, None)
        job = real.get_queue().get_jobs()[0]
        r = _job_summary(job)
        r["executor"] = type(fs.launched).__name__
        return r
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)


step("MRQ _start_sequence_render job build (PNG+MP4, render intercepted)", _mrq_seq)
step("LevelSequenceEditorBlueprintLibrary.get_current_time (deprecated API, no seq open)",
     lambda: unreal.LevelSequenceEditorBlueprintLibrary.get_current_time())
step("cloud_matte_holdout_ready()", mrq.cloud_matte_holdout_ready)


# ===================== capture_mrq review-fix checks (smoke_mrq) =====================
def _ok(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _outputs(job):
    cfg = job.get_configuration()
    exr = cfg.find_setting_by_class(unreal.MoviePipelineImageSequenceOutput_EXR)
    png = cfg.find_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
    jpg = cfg.find_setting_by_class(unreal.MoviePipelineImageSequenceOutput_JPG)
    return exr, png, jpg


def _seq_fmt(image_format, also_png):
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    fs = _FakeSub(real)
    try:
        mrq._start_sequence_render(fs, scene_seq, os.path.join(OUTD, "smoke_mrq_seq2"), 640, 360,
                                   "Smoke", "001", True, False, 18, 4, 8, None, None,
                                   core.create_temp_depth_material(0.0, 10000.0), None, None,
                                   None, None, None, "Beauty", False, None, None,
                                   image_format=image_format, also_png=also_png)
        job = real.get_queue().get_jobs()[0]
        exr, png, jpg = _outputs(job)
        r = {"exr": exr is not None, "png": png is not None, "jpg": jpg is not None,
             "exr_multilayer": exr.get_editor_property("multilayer") if exr else None,
             "png_write_alpha": png.get_editor_property("write_alpha") if png else None}
        return r
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
        core.delete_temp_depth_material()


def _seq_exr_png():
    r = _seq_fmt("exr", True)
    _ok(r["exr"] and r["png"] and not r["jpg"], r)
    _ok(r["exr_multilayer"] is False, r)
    _ok(r["png_write_alpha"] is False, r)
    return r


def _seq_jpg_png():
    r = _seq_fmt("jpg", True)
    _ok(r["jpg"] and r["png"] and not r["exr"], r)
    _ok(r["png_write_alpha"] is False, r)
    return r


def _seq_default_png():
    # 旧呼び出し（image_format 無し）は PNG のみ = 5.7 既存挙動
    r = _seq_fmt(None, False)
    _ok(r["png"] and not r["exr"] and not r["jpg"], r)
    return r


step("render_sequence job: image_format=exr also_png=True (EXR+PNG, multilayer False)", _seq_exr_png)
step("render_sequence job: image_format=jpg also_png=True (JPG+PNG)", _seq_jpg_png)
step("render_sequence job: default (PNG only)", _seq_default_png)


def _beauty_fmt(fmt, also_png, with_pass):
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    fs = _FakeSub(real)
    mats = {}
    if with_pass:
        mats["depth_material"] = core.create_temp_depth_material(0.0, 10000.0)
    try:
        mrq._start_render(fs, cam, os.path.join(OUTD, "smoke_mrq_b"), 640, 360,
                          False, fmt, also_png, 1, 4, 8, "Smoke", None, None, 0.0,
                          False, None, **mats)
        job = real.get_queue().get_jobs()[0]
        exr, png, jpg = _outputs(job)
        out = job.get_configuration().find_setting_by_class(unreal.MoviePipelineOutputSetting)
        return {"exr": exr is not None, "png": png is not None, "jpg": jpg is not None,
                "exr_multilayer": exr.get_editor_property("multilayer") if exr else None,
                "file_name_format": out.get_editor_property("file_name_format")}
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
        mrq._delete_temp_sequence()
        core.delete_temp_depth_material()


def _beauty_exr_pass():
    r = _beauty_fmt("exr", True, True)
    _ok(r["exr"] and r["png"] and r["exr_multilayer"] is False, r)   # H4
    return r


def _beauty_jpg_png():
    r = _beauty_fmt("jpg", True, True)
    _ok(r["jpg"] and r["png"] and not r["exr"], r)
    return r


step("render_beauty job: exr + Depth pass (H4 multilayer False)", _beauty_exr_pass)
step("render_beauty job: jpg also_png=True (JPG+PNG)", _beauty_jpg_png)


def _queue_backup():
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    q = real.get_queue()
    for j in list(q.get_jobs()):
        q.delete_job(j)
    uj = q.allocate_new_job(unreal.MoviePipelineExecutorJob)
    uj.job_name = "UserJob_KeepMe"
    fs = _FakeSub(real)
    try:
        mrq._start_render(fs, cam, os.path.join(OUTD, "smoke_mrq_b"), 640, 360,
                          False, "png", False, 1, 4, 8, "Smoke", None, None, 0.0,
                          False, None)
        during = [str(j.job_name) for j in q.get_jobs()]
        has_backup = mrq._KEEP.get("queue_backup") is not None
        mrq._restore_queue()
        after = [str(j.job_name) for j in real.get_queue().get_jobs()]
        _ok(during == ["UE5Capture_Beauty"] and has_backup, (during, has_backup))
        _ok(after == ["UserJob_KeepMe"], after)
        return {"during": during, "after": after}
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
        mrq._delete_temp_sequence()


step("M1 MRQ queue backup -> restore (user job survives)", _queue_backup)


def _force_reset():
    mrq._KEEP.clear()
    r1 = mrq.force_reset()
    flag = {"n": 0}
    mrq._KEEP["restore"] = lambda: flag.__setitem__("n", flag["n"] + 1)
    mrq._KEEP["executor"] = object()       # 「完了通知が来なかった」状態を模擬
    r2 = mrq.force_reset()
    _ok(r1 is True and r2 is True and flag["n"] == 1 and mrq._KEEP == {},
        (r1, r2, flag, mrq._KEEP))
    return {"empty": r1, "stuck": r2, "restore_called": flag["n"]}


step("M3 force_reset() with nothing rendering (and with stuck _KEEP)", _force_reset)


def _menu_force_reset():
    import capture_menu
    unreal._ue5capture_busy = True
    capture_menu.on_force_reset()
    _ok(getattr(unreal, "_ue5capture_busy", None) is False, "busy flag not cleared")
    return "called, _ue5capture_busy cleared"


step("capture_menu.on_force_reset()", _menu_force_reset)


def _pos_key():
    k = mrq._actor_pos_key(cube)
    _ok(len(k) == 6 and k[0] == "StaticMeshActor", k)
    _ok(k[4].startswith("/Engine/BasicShapes/Cube"), k)
    _ok(k[5] == (50, 50, 50), k)
    kc = mrq._actor_pos_key(cam)        # メッシュ無しアクター: "" / None
    _ok(kc[4] == "" and kc[5] is None, kc)
    st = mrq._start_pie_actor_hider([cube])   # 登録→停止（PIE 無しで例外にならない）
    st["stop"]()
    return {"cube": k, "cam": kc}


step("H2 _actor_pos_key on placed StaticMeshActor (+ hider start/stop)", _pos_key)


def _hide_h1():
    cube.set_actor_hidden_in_game(True)
    try:
        ch = mrq._hide_actors([cube, sun])
        _ok(ch == [sun], [a.get_actor_label() for a in ch])
        mrq._restore_saved_scene({"hidden": ch})
        _ok(cube.get_editor_property("hidden") is True, "pre-hidden cube was un-hidden")
        _ok(sun.get_editor_property("hidden") is False, "sun not restored")
        return "pre-hidden kept hidden, changed one restored"
    finally:
        cube.set_actor_hidden_in_game(False)


step("H1 hidden_actors: only not-already-hidden are hidden/restored", _hide_h1)


def _c1_beauty():
    mrq._KEEP.clear()
    try:
        mrq.render_beauty(cam, os.path.join(OUTD, "smoke_mrq_c1"), 640, 360,
                          hidden_actors=[cube], sky_matte=True)
    except RuntimeError as e:
        msg = str(e)
    else:
        raise AssertionError("expected sky matte RuntimeError (no UDS in level)")
    _ok(cube.get_editor_property("hidden") is False, "cube left hidden")
    _ok(mrq._KEEP == {}, mrq._KEEP)
    return msg


def _c1_seq():
    mrq._KEEP.clear()
    try:
        mrq.render_sequence(scene_seq, os.path.join(OUTD, "smoke_mrq_c1s"), 640, 360,
                            "Smoke", "001", hidden_actors=[cube], sky_matte=True)
    except RuntimeError as e:
        msg = str(e)
    else:
        raise AssertionError("expected sky matte RuntimeError (no UDS in level)")
    _ok(cube.get_editor_property("hidden") is False, "cube left hidden")
    _ok(mrq._KEEP == {}, mrq._KEEP)
    return msg


step("C1 render_beauty: setup failure (no UDS) restores hidden actor", _c1_beauty)
step("C1 render_sequence: setup failure (no UDS) restores hidden actor", _c1_seq)
step("L2 _first_cut_camera(scene_seq)", lambda: mrq._first_cut_camera(scene_seq).get_actor_label())


def _backing_m5():
    smc = cube.static_mesh_component
    before = list(smc.get_editor_property("override_materials"))
    saved = mrq._set_backing_materials([cube], True)
    mid = smc.get_material(0).get_name()
    mrq._restore_backing_materials(saved)
    after = list(smc.get_editor_property("override_materials"))
    _ok(len(after) == len(before), (before, after))
    return {"before": len(before), "during_mat": mid, "after": len(after),
            "mat0_after": smc.get_material(0).get_name()}


step("M5 backing materials restore override_materials array", _backing_m5)
step("L4 _spawn_cloud_fill_lights(0) -> []", lambda: mrq._spawn_cloud_fill_lights(0.0))
def _h3():
    r = mrq._warn_if_spawnable_camera(cam)
    _ok(r is False, r)
    return r


step("H3 _warn_if_spawnable_camera(placed cam) -> False", _h3)
step("M2 _default_near_clip_cm()", mrq._default_near_clip_cm)


def _m4_clear_reuse():
    seq, full = mrq._create_temp_sequence(cam)
    ext = unreal.MovieSceneSequenceExtensions
    mrq._clear_sequence(seq)
    n = (len(ext.get_tracks(seq)), len(ext.get_bindings(seq)),
         len(ext.find_tracks_by_exact_type(seq, unreal.MovieSceneCameraCutTrack)))
    _ok(n == (0, 0, 0), n)
    deleted = mrq._delete_temp_sequence()
    _ok(deleted is True, "delete failed")
    return {"after_clear": n, "deleted": deleted}


step("M4 _clear_sequence empties temp seq + _delete_temp_sequence True", _m4_clear_reuse)


def _l1_offset():
    at = unreal.AssetToolsHelpers.get_asset_tools()
    p = "/Game/SmokeSeq"
    if unreal.EditorAssetLibrary.does_asset_exist(p + "/SEQ_Smoke2997"):
        unreal.EditorAssetLibrary.delete_asset(p + "/SEQ_Smoke2997")
    s2 = at.create_asset("SEQ_Smoke2997", p, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    ext = unreal.MovieSceneSequenceExtensions
    ext.set_display_rate(s2, unreal.FrameRate(30000, 1001))
    ext.set_playback_start(s2, 0)
    ext.set_playback_end(s2, 1000)
    tick = ext.get_tick_resolution(s2)
    seq, full = mrq._create_temp_sequence(cam, scene_sequence=s2, scene_frame=400)
    sec = ext.find_tracks_by_exact_type(seq, unreal.MovieSceneSubTrack)[0].get_sections()[0]
    off = sec.get_editor_property("parameters").get_editor_property("start_frame_offset").value
    exp = int(round(400 * tick.numerator * 1001 / float(tick.denominator * 30000)))
    mrq._delete_temp_sequence()
    unreal.EditorAssetLibrary.delete_asset(p + "/SEQ_Smoke2997")
    _ok(off == exp, (off, exp))
    return {"tick": "%d/%d" % (tick.numerator, tick.denominator), "offset": off, "expected": exp}


step("L1 start_frame_offset exact at 29.97fps frame 400", _l1_offset)


# ===================== ビューポートのルック（経路B: MRQ 近似） =====================
def _cvar_names(job):
    cv = job.get_configuration().find_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    return [str(e.get_editor_property("name")) for e in cv.get_editor_property("cvars")]


def _settings_names(job):
    return sorted(type(s).__name__ for s in job.get_configuration().get_all_settings())


def _vl_still_job():
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    out = {}
    hq = set(n for n, _v in mrq._HQ_CVARS)
    try:
        for vl in (False, True):
            fs = _FakeSub(real)
            mat = core.create_temp_matte_material()
            mrq._start_render(fs, cam, os.path.join(OUTD, "smoke_mrq_vl"), 640, 360,
                              False, "png", False, 1, 4, 8, "Smoke", None, None, 0.0,
                              False, None, matte_material=mat, viewport_look=vl)
            job = real.get_queue().get_jobs()[0]
            names = _cvar_names(job)
            sets = _settings_names(job)
            out[vl] = {"hq": sorted(hq & set(names)), "custom_depth": "r.CustomDepth" in names,
                       "motion_blur": "r.MotionBlurQuality" in names,
                       "game_override": "MoviePipelineGameOverrideSetting" in sets}
            mrq._KEEP.clear()
            for j in list(real.get_queue().get_jobs()):
                real.get_queue().delete_job(j)
            mrq._delete_temp_sequence()
            core.delete_temp_matte_material()
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
        mrq._delete_temp_sequence()
        core.delete_temp_matte_material()
    _ok(out[False]["hq"] and out[False]["game_override"], out)
    # viewport look の GameOverride は品質を上げない設定（中身は VC7）
    _ok(out[True]["hq"] == [] and out[True]["game_override"], out)
    _ok(out[True]["custom_depth"] and out[True]["motion_blur"], out)   # パスに必須の cvar は残す
    return out


step("VL1 render_beauty job: viewport_look drops HQ cvars, neutral GameOverride, keeps r.CustomDepth",
     _vl_still_job)


def _vl_seq_job():
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    hq = set(n for n, _v in mrq._HQ_CVARS)
    out = {}
    try:
        for vl in (False, True):
            fs = _FakeSub(real)
            mrq._start_sequence_render(fs, scene_seq, os.path.join(OUTD, "smoke_mrq_vls"), 640, 360,
                                       "Smoke", "001", True, False, 18, 4, 8, None, None,
                                       None, None, None, None, None,
                                       None, "Beauty", False, None, None, viewport_look=vl)
            job = real.get_queue().get_jobs()[0]
            out[vl] = {"hq": sorted(hq & set(_cvar_names(job))),
                       "game_override": "MoviePipelineGameOverrideSetting" in _settings_names(job)}
            mrq._KEEP.clear()
            for j in list(real.get_queue().get_jobs()):
                real.get_queue().delete_job(j)
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
    _ok(out[False]["hq"] and out[False]["game_override"], out)
    _ok(out[True]["hq"] == [] and out[True]["game_override"], out)
    return out


step("VL2 render_sequence job: viewport_look drops HQ cvars, neutral GameOverride", _vl_seq_job)


def _pp_w():
    return float(cam.camera_component.get_editor_property("post_process_blend_weight"))


def _vl_pp_still():
    cam.camera_component.set_editor_property("post_process_blend_weight", 1.0)
    seen = {}
    orig = mrq._start_render

    def fake(sub, cam_, *a, **k):
        seen["during"] = _pp_w()
        seen["viewport_look"] = k.get("viewport_look")
        raise RuntimeError("stop before MRQ")
    mrq._start_render = fake
    try:
        mrq.render_beauty(cam, os.path.join(OUTD, "smoke_mrq_vlpp"), 640, 360, viewport_look=True)
    except RuntimeError:
        pass
    finally:
        mrq._start_render = orig
    seen["after_fail"] = _pp_w()
    # 正常完了側の復元（_on_finished が呼ぶ _restore_saved_scene）
    saved = core.zero_camera_pp_weights([cam])
    seen["zeroed"] = _pp_w()
    mrq._restore_saved_scene({"ppw": saved})
    seen["after_ok"] = _pp_w()
    _ok(seen["during"] == 0.0 and seen["viewport_look"] is True, seen)
    _ok(seen["after_fail"] == 1.0 and seen["zeroed"] == 0.0 and seen["after_ok"] == 1.0, seen)
    _ok(mrq._KEEP == {}, mrq._KEEP)
    return seen


step("VL3 render_beauty viewport_look: camera PP weight 0 during, restored (fail + ok paths)",
     _vl_pp_still)


def _vl_pp_seq():
    cam.camera_component.set_editor_property("post_process_blend_weight", 0.7)
    seen = {}
    orig = mrq._start_sequence_render

    def fake(*a, **k):
        seen["during"] = round(_pp_w(), 3)
        seen["viewport_look"] = k.get("viewport_look")
        raise RuntimeError("stop before MRQ")
    mrq._start_sequence_render = fake
    try:
        mrq.render_sequence(scene_seq, os.path.join(OUTD, "smoke_mrq_vlpps"), 640, 360,
                            "Smoke", "001", viewport_look=True)
    except RuntimeError:
        pass
    finally:
        mrq._start_sequence_render = orig
    seen["after"] = round(_pp_w(), 3)
    seen["cut_cams"] = [a.get_actor_label() for a in
                        mrq._camera_cut_cameras_with_count(scene_seq)[0]]
    seen["cut_bindings"] = mrq._camera_cut_cameras_with_count(scene_seq)[1]
    cam.camera_component.set_editor_property("post_process_blend_weight", 1.0)
    _ok(seen["during"] == 0.0 and seen["after"] == 0.7 and seen["viewport_look"] is True, seen)
    _ok(seen["cut_cams"] == ["SmokeCam"] and seen["cut_bindings"] == 1, seen)
    return seen


step("VL4 render_sequence viewport_look: camera-cut camera PP weight 0 during, restored",
     _vl_pp_seq)


def _vl_levelvis():
    seq, full = mrq._create_temp_sequence(cam, level_visibility=True)
    ext = unreal.MovieSceneSequenceExtensions
    n_auto = len(ext.find_tracks_by_exact_type(seq, unreal.MovieSceneLevelVisibilityTrack))
    vis = mrq._editor_level_visibility()
    tr = mrq._add_level_visibility_track(seq, ["SubA", "SubB"], ["SubC"])
    secs = [(str(s.get_visibility()), [str(n) for n in s.get_level_names()])
            for s in tr.get_sections()]
    none = mrq._add_level_visibility_track(seq, [], [])
    mrq._delete_temp_sequence()
    # 捨てプロジェクトの Untitled にはサブレベルが無い → 自動トラックは作らない
    _ok(vis == ([], []) and n_auto == 0 and none is None, (vis, n_auto, none))
    _ok(len(secs) == 2 and secs[0][1] == ["SubA", "SubB"] and secs[1][1] == ["SubC"], secs)
    _ok("VISIBLE" in secs[0][0] and "HIDDEN" in secs[1][0], secs)
    return {"editor_levels": vis, "sections": secs}


step("VL5 level visibility track (names/visibility; none for a level without sublevels)",
     _vl_levelvis)


def _vl_grab_no_viewport():
    cam.camera_component.set_editor_property("post_process_blend_weight", 1.0)
    r = {"shows_pp": core.viewport_shows_camera_pp(cam)}
    try:
        core.begin_viewport_grab(cam, 64, 36, os.path.join(OUTD, "smoke_vp.png"))
        r["raised"] = None
    except Exception as e:
        r["raised"] = "%s: %s" % (type(e).__name__, e)
    r["pp_after"] = _pp_w()
    doc = getattr(getattr(unreal, "AutomationLibrary", None), "take_high_res_screenshot", None)
    r["thrs_doc"] = (doc.__doc__ or "").strip().splitlines()[0] if doc else None
    _ok(r["shows_pp"] is False and r["raised"] and r["pp_after"] == 1.0, r)
    return r


step("VL6 begin_viewport_grab without a viewport raises and restores camera PP", _vl_grab_no_viewport)


def _cvar_map(job):
    cv = job.get_configuration().find_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    return {str(e.get_editor_property("name")): e.get_editor_property("value")
            for e in cv.get_editor_property("cvars")}


def _vc_seq_job():
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    name = mrq._EXPOSURE_OFFSET_CVAR
    out = {}
    try:
        for key, ev in (("ev+1.75", 1.75), ("ev-2.5", -2.5), ("none", None), ("zero", 0.0)):
            fs = _FakeSub(real)
            mrq._start_sequence_render(fs, scene_seq, os.path.join(OUTD, "smoke_mrq_vc"), 640, 360,
                                       "Smoke", "001", True, False, 18, 4, 8, None, None,
                                       None, None, None, None, None,
                                       None, "Beauty", False, None, None, viewport_look=True,
                                       exposure_offset_ev=ev)
            out[key] = _cvar_map(real.get_queue().get_jobs()[0]).get(name)
            mrq._KEEP.clear()
            for j in list(real.get_queue().get_jobs()):
                real.get_queue().delete_job(j)
        # 静止画ジョブ（render_beauty）には露出合わせを入れない
        fs = _FakeSub(real)
        mrq._start_render(fs, cam, os.path.join(OUTD, "smoke_mrq_vc"), 640, 360,
                          False, "png", False, 1, 4, 8, "Smoke", None, None, 0.0,
                          False, None, viewport_look=True)
        out["still"] = _cvar_map(real.get_queue().get_jobs()[0]).get(name)
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
        mrq._delete_temp_sequence()
    _ok(name == "r.ExposureOffset", name)
    _ok(abs(out["ev+1.75"] - 1.75) < 1e-6 and abs(out["ev-2.5"] + 2.5) < 1e-6, out)
    _ok(out["none"] is None and out["zero"] is None and out["still"] is None, out)
    return out


step("VC2 render_sequence job: exposure_offset_ev -> r.ExposureOffset cvar only when given",
     _vc_seq_job)


def _vc_seq_passthrough():
    seen = {}
    orig = mrq._start_sequence_render

    def fake(*a, **k):
        seen["ev"] = k.get("exposure_offset_ev")
        raise RuntimeError("stop before MRQ")
    mrq._start_sequence_render = fake
    try:
        mrq.render_sequence(scene_seq, os.path.join(OUTD, "smoke_mrq_vcp"), 640, 360,
                            "Smoke", "001", viewport_look=True, exposure_offset_ev=-1.25)
    except RuntimeError:
        pass
    finally:
        mrq._start_sequence_render = orig
    _ok(seen.get("ev") == -1.25 and mrq._KEEP == {}, (seen, mrq._KEEP))
    return seen


step("VC3 render_sequence passes exposure_offset_ev through to the job builder", _vc_seq_passthrough)


def _go(job):
    go = job.get_configuration().find_setting_by_class(unreal.MoviePipelineGameOverrideSetting)
    if go is None:
        return None
    return {k: str(go.get_editor_property(k)) for k in
            ("cinematic_quality_settings", "texture_streaming", "use_lod_zero",
             "use_high_quality_shadows", "override_view_distance_scale")}


def _vc_game_override():
    # 置かないと MRQ は Cinematic スカラビリティの一時 GameOverride を足す → viewport look は明示的に OFF で置く
    real = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    out = {}
    try:
        for key, vp in (("seq_vp", True), ("seq_hq", False)):
            fs = _FakeSub(real)
            mrq._start_sequence_render(fs, scene_seq, os.path.join(OUTD, "smoke_mrq_vc"), 640, 360,
                                       "Smoke", "001", True, False, 18, 4, 8, None, None,
                                       None, None, None, None, None,
                                       None, "Beauty", False, None, None, viewport_look=vp)
            out[key] = _go(real.get_queue().get_jobs()[0])
            mrq._KEEP.clear()
            for j in list(real.get_queue().get_jobs()):
                real.get_queue().delete_job(j)
        fs = _FakeSub(real)
        mrq._start_render(fs, cam, os.path.join(OUTD, "smoke_mrq_vc"), 640, 360,
                          False, "png", False, 1, 4, 8, "Smoke", None, None, 0.0,
                          False, None, viewport_look=True)
        out["still_vp"] = _go(real.get_queue().get_jobs()[0])
    finally:
        mrq._KEEP.clear()
        for j in list(real.get_queue().get_jobs()):
            real.get_queue().delete_job(j)
        mrq._delete_temp_sequence()
    for k in ("seq_vp", "still_vp"):
        g = out[k]
        _ok(g and g["cinematic_quality_settings"] == "False" and "NONE" in g["texture_streaming"]
            and g["use_lod_zero"] == "False" and g["use_high_quality_shadows"] == "False"
            and g["override_view_distance_scale"] == "False", out)
    _ok(out["seq_hq"] and out["seq_hq"]["cinematic_quality_settings"] == "True", out)
    return out


step("VC7 viewport look jobs carry a GameOverride with Cinematic scalability OFF (HQ job keeps it ON)",
     _vc_game_override)


def _vc_analytic_exposure():
    ext = unreal.MovieSceneSequenceExtensions
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    r = {}
    # レベルのカメラ（近似側でウェイト 0＝ビューポートと同じ）→ 0
    ev, why = mrq.viewport_look_exposure(scene_seq, 0)
    r["level"] = (ev, why)
    _ok(ev == 0.0 and "level" in why, r)
    # スポーナブルのカメラ（ウェイトを 0 にできない）: テンプレートの露出補正 +1.5（w=0.8）、
    # unbound PPV の露出補正 +0.5 → -0.8×(1.5-0.5) = -0.8EV
    at = unreal.AssetToolsHelpers.get_asset_tools()
    p = "/Game/SmokeSeq"
    if unreal.EditorAssetLibrary.does_asset_exist(p + "/SEQ_SmokeSpawn"):
        unreal.EditorAssetLibrary.delete_asset(p + "/SEQ_SmokeSpawn")
    seq = at.create_asset("SEQ_SmokeSpawn", p, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    ppv = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0))
    try:
        ext.set_playback_start(seq, 0)
        ext.set_playback_end(seq, 24)
        b = ext.add_spawnable_from_class(seq, unreal.CineCameraActor)
        tpl = b.get_object_template()
        cc = tpl.camera_component
        s = cc.get_editor_property("post_process_settings")
        s.set_editor_property("override_auto_exposure_bias", True)
        s.set_editor_property("auto_exposure_bias", 1.5)
        cc.set_editor_property("post_process_settings", s)
        cc.set_editor_property("post_process_blend_weight", 0.8)
        cct = ext.add_track(seq, unreal.MovieSceneCameraCutTrack)
        sec = cct.add_section()
        sec.set_range(0, 24)
        sec.set_camera_binding_id(ext.get_binding_id(seq, b))
        ppv.set_editor_property("unbound", True)
        vs = ppv.get_editor_property("settings")
        vs.set_editor_property("override_auto_exposure_bias", True)
        vs.set_editor_property("auto_exposure_bias", 0.5)
        ppv.set_editor_property("settings", vs)
        ev, why = mrq.viewport_look_exposure(seq, 5)
        r["spawnable"] = (round(ev, 4), why)
        _ok(abs(ev + 0.8) < 1e-4 and "spawnable" in why, r)
        # PPV を消すと基準は PostProcessSettings の既定値
        eas.destroy_actor(ppv)
        ppv = None
        d = float(unreal.PostProcessSettings().get_editor_property("auto_exposure_bias"))
        ev, why = mrq.viewport_look_exposure(seq, 5)
        r["no_ppv"] = (round(ev, 4), d)
        _ok(abs(ev + 0.8 * (1.5 - d)) < 1e-4, r)
    finally:
        if ppv is not None:
            eas.destroy_actor(ppv)
        unreal.EditorAssetLibrary.delete_asset(p + "/SEQ_SmokeSpawn")
    return r


step("VC8 viewport_look_exposure: level camera -> 0 / spawnable template bias vs PPV -> -w*(cam-base)",
     _vc_analytic_exposure)



def _cleanup():
    for a in (cube, cam, sun):
        if a:
            eas.destroy_actor(a)
    if unreal.EditorAssetLibrary.does_asset_exist("/Game/SmokeSeq/SEQ_Smoke"):
        unreal.EditorAssetLibrary.delete_asset("/Game/SmokeSeq/SEQ_Smoke")
    left = []
    if unreal.EditorAssetLibrary.does_directory_exist("/Game/_UE5Capture_Tmp"):
        left = list(unreal.EditorAssetLibrary.list_assets("/Game/_UE5Capture_Tmp", True, False))
    return {"tmp_assets_left": left}


step("cleanup", _cleanup)

n_fail = sum(1 for r in RES if r[1] == "FAIL")
with open(os.path.join(OUTD, "smoke_mrq_result_%s.json" % VSHORT), "w", encoding="utf-8") as f:
    json.dump({"engine": ver, "results": RES}, f, indent=1, ensure_ascii=False)
P("SUMMARY engine=%s steps=%d fail=%d" % (ver, len(RES), n_fail))
