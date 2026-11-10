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
OUT = os.path.join(OUTD, "smoke_core_out_%s" % VSHORT)
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
        mrq._start_render(fs, cam, os.path.join(OUTD, "smoke_core_mrq"), 640, 360,
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
        mrq._start_sequence_render(fs, scene_seq, os.path.join(OUTD, "smoke_core_mrq_seq"), 640, 360,
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



# ======================= capture_core fix checks (smoke_core) =======================
import numpy as _np2
from PIL import Image as _Im2


def _exr_rt():
    import imageio.v2 as iio
    p = os.path.join(OUT, "exr_rt.exr")
    a = _np2.array([[123.456, 50000, 70000, 1e8]], _np2.float32)
    ok = core._write_exr_gray(p, a)
    b = iio.imread(p)
    b = b if b.ndim == 2 else b[..., 0]
    assert ok and b.dtype == _np2.float32 and _np2.array_equal(b, a), (ok, b.dtype, b.tolist())
    return (str(b.dtype), b.ravel().tolist())


step("C1 _write_exr_gray float32 round-trip", _exr_rt)


def _ff():
    p = core.find_ffmpeg()
    assert p and os.path.isfile(p) and p.lower().endswith(".exe"), p
    return p


step("H4 find_ffmpeg() -> imageio_ffmpeg exe", _ff)
def _nc():
    ini = os.path.join(unreal.Paths.project_config_dir(), "DefaultEngine.ini")
    orig = open(ini, encoding="utf-8").read() if os.path.isfile(ini) else None
    v0 = core.default_near_clip_cm()
    try:
        with open(ini, "a", encoding="utf-8") as f:
            f.write(chr(10) + "[/Script/Engine.Engine]" + chr(10) + "NearClipPlane=5.5" + chr(10))
        v_cached = core.default_near_clip_cm()
        core._NEAR_CLIP_CACHE = None
        v1 = core.default_near_clip_cm()
        assert v_cached == 10.0, v_cached
        core._NEAR_CLIP_CACHE = None
    finally:
        if orig is None:
            os.remove(ini)
        else:
            open(ini, "w", encoding="utf-8").write(orig)
    assert v0 == 10.0 and v1 == 5.5, (v0, v1)
    return (v0, v1)


step("M5 default_near_clip_cm() (ini absent=10 / NearClipPlane=5.5)", _nc)
step("API presence (release_render_target2d / get_material_property_input_node)",
     lambda: (hasattr(unreal.RenderingLibrary, "release_render_target2d"),
              hasattr(unreal.MaterialEditingLibrary, "get_material_property_input_node")))


def _far_cube():
    fc = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(80000, 20000, 0))
    fc.static_mesh_component.set_static_mesh(
        unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube"))
    fc.set_actor_scale3d(unreal.Vector(100, 100, 100))
    fc.set_actor_label("SmokeFarCube")
    return fc


far_cube = step("spawn far cube (800m, 100m)", _far_cube)


def _n_scenecap():
    return len(unreal.GameplayStatics.get_all_actors_of_class(
        core._get_editor_world(), unreal.SceneCapture2D))


def _depth_seq(mode):
    d = os.path.join(OUT, "dseq_" + mode)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    import time
    t = time.time()
    n, ext = core.render_depth_sequence(d, "Smoke", "001", 0, 2, 384, 216,
                                        lambda f: cam, 100.0, 100000.0, mode, aa=1)
    dt = time.time() - t
    files = sorted(os.listdir(d))
    r = {"n": n, "ext": ext, "files": files, "sec": round(dt, 2),
         "scenecap_left": _n_scenecap()}
    if ext == "png":
        im = _Im2.open(os.path.join(d, files[0]))
        a = _np2.asarray(im)
        r.update(mode=im.mode, dtype=str(a.dtype), min=int(a.min()), max=int(a.max()),
                 n_mid=int(((a > 0) & (a < 65535)).sum()))
        assert im.mode in ("I;16", "I") and a.dtype == _np2.uint16
    else:
        import imageio.v2 as iio
        a = iio.imread(os.path.join(d, files[0]))
        a = a if a.ndim == 2 else a[..., 0]
        r.update(dtype=str(a.dtype), min=float(a.min()), max=float(a.max()),
                 n_far_geo=int(((a > 65504) & (a < 1e7)).sum()),
                 n_sky=int((a >= 1e7).sum()), n_inf=int(_np2.isinf(a).sum()))
        assert a.dtype == _np2.float32 and r["n_far_geo"] > 0 and r["n_inf"] == 0
    assert n == 2 and r["scenecap_left"] == 0
    return r


step("NEW render_depth_sequence 16bit (2 frames)", lambda: _depth_seq("16bit"))
step("NEW render_depth_sequence exr (2 frames)", lambda: _depth_seq("exr"))


def _depth_hd():
    import time
    d = os.path.join(OUT, "dseq_hd")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    t = time.time()
    r = core.render_depth_sequence(d, "Smoke", "001", 0, 3, 1920, 1080,
                                   lambda f: cam, 100.0, 100000.0, "16bit", aa=1)
    return {"ret": r, "sec_per_frame": round((time.time() - t) / 3, 2)}


step("render_depth_sequence 1920x1080 16bit timing (3 frames)", _depth_hd)


def _trim_exr():
    d = os.path.join(OUT, "dseq_exr")
    n = core.trim_sequence_frames(d, "Smoke", "001", ["Depth"], 0, 1)
    left = sorted(os.listdir(d))
    assert n == 1 and left == ["Smoke_Depth_001.0000.exr"], (n, left)
    m = core.move_pass_frames_to_subdirs(d, "Smoke", "001", ["Depth"])
    return (n, m, sorted(os.listdir(d)))


step("NEW trim/move handle .exr", _trim_exr)


def _far_matte():
    s = core.CaptureSettings()
    s.camera_actor = cam
    s.output_dir = OUT
    s.base_height = 216
    s.aa_factor = 1
    s.do_color = False
    s.do_depth = False
    s.do_matte = True
    s.do_object_id = True
    s.matte_actors = [far_cube]
    s.objid_actors = [cube, far_cube]
    s.take_suffix = "002"
    outs = [os.path.basename(o) for o in core.run_capture(s)]
    mp = [o for o in outs if "_Matte_" in o][0]
    m = _np2.asarray(_Im2.open(os.path.join(OUT, mp)))
    oid = [o for o in outs if "_ObjectID_" in o][0]
    import json as _j
    man = _j.load(open(os.path.join(OUT, oid[:-4] + ".json"), encoding="utf-8"))
    img = _np2.asarray(_Im2.open(os.path.join(OUT, oid)).convert("RGB"))
    cols = {"#%02X%02X%02X" % tuple(int(v) for v in c) for c in img.reshape(-1, 3)}
    hit = {k: (k in cols) for k in man}
    r = {"outs": outs, "matte_cov_px": int((m > 127).sum()), "manifest": man,
         "manifest_key_in_png": hit, "scenecap_left": _n_scenecap()}
    assert r["matte_cov_px"] > 0 and len(man) == 2 and all(hit.values()) and r["scenecap_left"] == 0
    return r


step("H2/M2 matte+objid of 800m cube (32F threshold, manifest key == pixel)", _far_matte)


def _mb():
    m1 = core.get_or_create_matteboard_material()
    c1 = core._emissive_connected(m1)
    m2 = core.get_or_create_matteboard_material()
    return {"emissive_connected": c1, "same_obj": m1 == m2,
            "n_expr": unreal.MaterialEditingLibrary.get_num_material_expressions(m2)}


step("M10 matteboard reuse (no rebuild when connected)", _mb)


def _unlit_restore():
    n1 = core.set_matte_unlit([cube])
    tags1 = [str(t) for t in cube.tags]
    # 壊れたパスのタグを1本足して復元 → タグが残ること
    cube.set_editor_property("tags", list(cube.tags) + [unreal.Name(
        "ue5cap_origmat|StaticMeshComponent0|0|/Game/NoSuch/M_Missing.M_Missing")])
    n2 = core.restore_matte_materials([cube])
    tags2 = [str(t) for t in cube.tags]
    mat0 = cube.static_mesh_component.get_material(0)
    r = {"unlit": n1, "restored": n2, "tags_after": tags2,
         "slot0": mat0.get_path_name() if mat0 else None}
    assert len(tags2) == 1 and "M_Missing" in tags2[0], r
    cube.set_editor_property("tags", [])
    return r


step("H1 restore keeps tag when original material is missing", _unlit_restore)


def _mp4_odd():
    d = os.path.join(OUT, "mp4odd")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    for i in range(3):
        _Im2.fromarray(_np2.full((51, 101, 3), 40 * i, _np2.uint8)).save(
            os.path.join(d, "Smoke_Beauty_001.%04d.png" % i))
    import subprocess
    cmd, dst = core.encode_mp4_cmd(core.find_ffmpeg(), d, "Smoke", "Beauty", "001", 24, 1, 18, 0)
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    assert r.returncode == 0 and os.path.isfile(dst), r.stderr[-300:]
    return (r.returncode, os.path.getsize(dst))


step("M4 encode_mp4_cmd with odd 101x51", _mp4_odd)


def _rest():
    eas.destroy_actor(far_cube)
    return _n_scenecap()


step("destroy far cube / SceneCapture2D left (expect 0)", _rest)
# ===================================================================================


def _naming():
    rn = core.resolve_name_template
    assert rn("{level}_{cut}_{camera}", level="LV_047_MASTER", cut="047", camera="CamA") == "LV_047_MASTER_047_CamA"
    assert rn("{level}_{cut}_{camera}", level="Untitled", cut="", camera="Cam A") == "Untitled_Cam_A"
    assert rn("{nope}", level="x") == "capture"
    assert rn("", level="L", cut="1", camera="C") == "L_1_C"
    assert rn("{seq}-{camera}", seq="CIN_01A_047", camera="Cam") == "CIN_01A_047-Cam"
    d = os.path.join(OUT, "naming")
    os.makedirs(d, exist_ok=True)
    for f in ["Shot_Beauty_001.png", "Shot_Depth_002.exr", "Other_Beauty_005.png"]:
        open(os.path.join(d, f), "wb").close()
    os.makedirs(os.path.join(d, "Shot_003"), exist_ok=True)
    os.makedirs(os.path.join(d, "Beauty_002"), exist_ok=True)
    open(os.path.join(d, "Beauty_002", "Shot_Beauty_002.0001.png"), "wb").close()
    assert core.latest_take_number(d, "Shot") == 3
    assert core.latest_take_number(d, "Other") == 5
    assert core.latest_take_number(d, "None") == 0
    n = core.delete_take_outputs(d, "Shot", "002")
    assert n == 2, n
    assert not os.path.exists(os.path.join(d, "Beauty_002"))
    assert not os.path.exists(os.path.join(d, "Shot_Depth_002.exr"))
    assert os.path.exists(os.path.join(d, "Other_Beauty_005.png")) and os.path.isdir(os.path.join(d, "Shot_003"))
    n = core.delete_take_outputs(d, "Shot", "003")
    assert n == 1 and not os.path.isdir(os.path.join(d, "Shot_003"))
    # prefix collision: "A" must not see or delete "A_B" outputs; empty foreign folders survive
    for f in ["A_Beauty_001.png", "A_B_Beauty_004.png", "A_B_Depth_001.exr"]:
        open(os.path.join(d, f), "wb").close()
    os.makedirs(os.path.join(d, "Depth_001"), exist_ok=True)      # empty, not ours
    os.makedirs(os.path.join(d, "Normal_001"), exist_ok=True)
    open(os.path.join(d, "Normal_001", "A_B_Normal_001.0001.png"), "wb").close()
    assert core.latest_take_number(d, "A") == 1
    assert core.latest_take_number(d, "A_B") == 4
    n = core.delete_take_outputs(d, "A", "001")
    assert n == 1, n
    assert not os.path.exists(os.path.join(d, "A_Beauty_001.png"))
    assert os.path.exists(os.path.join(d, "A_B_Depth_001.exr"))
    assert os.path.isdir(os.path.join(d, "Depth_001")) and os.path.isdir(os.path.join(d, "Normal_001"))
    assert os.path.exists(os.path.join(d, "Normal_001", "A_B_Normal_001.0001.png"))
    n = core.delete_take_outputs(d, "A_B", "001")
    assert n == 2 and not os.path.isdir(os.path.join(d, "Normal_001")), n
    assert os.path.exists(os.path.join(d, "A_B_Beauty_004.png"))
    # broken templates never raise
    assert rn("{level.x}", level="L") == "level.x"
    assert rn("{0}_{camera}", camera="C") == "0_camera"
    assert rn("{level", level="L") == "level"
    return {"cut_untitled": core.detect_cut_number(), "level": core.level_name()}


step("N1 resolve_name_template / latest_take_number / delete_take_outputs", _naming)


def _cut_detect():
    # 人間のレベル構成を模す: /Game/SmokeCut/01_047_Forest/{LV_047_Environment, LV_047_Lighting}
    # を永続レベルのサブレベル（LevelStreamingDynamic）として足し、親フォルダ名とサブレベル名に
    # 共通する番号 047 が返ることを見る。
    world = core._get_editor_world()
    made = []
    for nm in ("LV_047_Environment", "LV_047_Lighting"):
        pkg = "/Game/SmokeCut/01_047_Forest/" + nm
        if unreal.EditorAssetLibrary.does_asset_exist(pkg):
            unreal.EditorAssetLibrary.delete_asset(pkg)
        sl = unreal.EditorLevelUtils.create_new_streaming_level(
            unreal.LevelStreamingDynamic, pkg, False)
        assert sl is not None, "create_new_streaming_level returned None for " + pkg
        made.append(pkg)
    cut = core.detect_cut_number(world)
    res = {"cut": cut, "levels": [core._level_package(l) for l in unreal.EditorLevelUtils.get_levels(world)]}
    for pkg in made:
        try:
            lv = None
            for l in unreal.EditorLevelUtils.get_levels(world):
                if core._level_package(l) == pkg:
                    lv = l
            if lv is not None:
                unreal.EditorLevelUtils.remove_level_from_world(lv)
        except Exception as e:
            P("  remove_level_from_world: %s" % e)
        if unreal.EditorAssetLibrary.does_asset_exist(pkg):
            unreal.EditorAssetLibrary.delete_asset(pkg)
    assert cut == "047", res
    return res


step("N2 detect_cut_number from 01_047_Forest/LV_047_* sublevels", _cut_detect)


def _vp_exposure_math():
    r = {}
    # PPV の露出補正の合成: priority の低い順に lerp、上書きしていない物は無視
    b = core.blend_exposure_bias(1.0, [(0, 1.0, True, 2.0)])
    assert abs(b - 2.0) < 1e-9, b
    b = core.blend_exposure_bias(1.0, [(5, 0.5, True, 3.0), (0, 1.0, True, 2.0), (9, 1.0, False, -4.0)])
    assert abs(b - 2.5) < 1e-9, b                 # 2.0 → lerp(2.0, 3.0, 0.5)
    assert core.blend_exposure_bias(1.0, []) == 1.0
    r["blend"] = b
    f = core.viewport_look_exposure_offset
    # レベルのカメラ（近似でウェイト 0）: レンダにもビューポートにも乗らない → 0
    assert f(2.0, True, 3.5, 1.0, False, False) == 0.0
    # パイロット中: 両方に乗る → 0
    assert f(2.0, True, 3.5, 1.0, True, True) == 0.0
    # スポーナブル: レンダにだけ乗る → -w×(cam-base)
    assert abs(f(2.0, True, 3.5, 0.8, True, False) + 1.2) < 1e-9
    # カメラが露出補正を上書きしていない → 0
    assert f(2.0, False, 3.5, 1.0, True, False) == 0.0
    # ウェイトは 0..1 にクランプ
    assert abs(f(0.0, True, 1.0, 2.0, True, False) + 1.0) < 1e-9
    r["spawnable"] = f(2.0, True, 3.5, 0.8, True, False)
    return r


step("VC6 viewport look exposure math: PPV bias blend / -w*(cam-base) only when PP is in render but not viewport",
     _vp_exposure_math)


step("cleanup", _cleanup)

n_fail = sum(1 for r in RES if r[1] == "FAIL")
with open(os.path.join(OUTD, "smoke_core_result_%s.json" % VSHORT), "w", encoding="utf-8") as f:
    json.dump({"engine": ver, "results": RES}, f, indent=1, ensure_ascii=False)
P("SUMMARY engine=%s steps=%d fail=%d" % (ver, len(RES), n_fail))
