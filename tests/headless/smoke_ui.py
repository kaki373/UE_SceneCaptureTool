# Headless smoke test of the capture_ui panel (tkinter inside a UE commandlet, throwaway project).
# Exercises: window build (shared target card outside the Notebook, seq-tab format/depth-bit
# widgets), settings round-trip, busy guard, impl early returns, frame-aware camera-cut lookup.
import os, sys, json, traceback
import unreal

SP = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(os.path.dirname(SP)), "ue5_capture")
OUTD = os.environ.get("UE_CAPTURE_SMOKE_OUT") or SP
os.makedirs(OUTD, exist_ok=True)
RES = []


def P(msg):
    print("[SMOKEUI] " + msg)


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
try:
    import numpy, PIL  # noqa
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
ui = step("import capture_ui", lambda: __import__("capture_ui"))
import tkinter as tk
from tkinter import ttk

eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def _spawn():
    cube = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(500, 0, 0))
    cube.static_mesh_component.set_static_mesh(
        unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube"))
    cube.set_actor_label("SmokeCube")
    cam = eas.spawn_actor_from_class(unreal.CineCameraActor, unreal.Vector(0, 0, 0))
    cam.set_actor_label("SmokeCam")
    cam2 = eas.spawn_actor_from_class(unreal.CineCameraActor, unreal.Vector(0, 100, 0))
    cam2.set_actor_label("SmokeCam2")
    return cube, cam, cam2


cube, cam, cam2 = step("spawn cube + 2 CineCameras", _spawn) or (None, None, None)

win = step("capture_ui.show() builds the window", ui.show)


def _pump(n=5):
    for _ in range(n):
        win.root.update()
    return True


step("root.update() x5", _pump)


def _ancestors(w):
    out = []
    while w is not None:
        out.append(type(w).__name__)
        w = w.master
    return out


def _shared_card():
    a1 = _ancestors(win.matte_pick["list"])
    a2 = _ancestors(win.objid_pick["list"])
    assert "Notebook" not in a1 and "Notebook" not in a2, (a1, a2)
    return {"matte_anc": a1[:6], "objid_anc": a2[:6]}


step("target pickers live outside the Notebook (shared card)", _shared_card)


def _seq_widgets():
    assert win.seq_beauty_fmt_var.get() == "PNG 8bit"
    assert win.seq_depth_bit_var.get() == "8bit PNG"
    assert win.depth_bit_var.get() == "16bit PNG"
    # the combobox values
    found = {"fmt": None, "bit": None}
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, ttk.Combobox):
                tv = str(c.cget("textvariable"))
                if tv == str(win.seq_beauty_fmt_var):
                    found["fmt"] = list(c.cget("values"))
                if tv == str(win.seq_depth_bit_var):
                    found["bit"] = list(c.cget("values"))
            walk(c)
    walk(win.root)
    assert found["fmt"] == ["PNG 8bit", "JPG 8bit", "EXR 16bit (float)"], found
    assert found["bit"] == ["8bit PNG", "16bit PNG", "EXR float"], found
    return found


step("seq tab has format + depth-bit comboboxes", _seq_widgets)


def _name_widgets():
    assert win.name_tpl_var.get() == core.DEFAULT_NAME_TEMPLATE == "{level}_{cut}_{camera}"
    assert win.seq_name_tpl_var.get() == core.DEFAULT_NAME_TEMPLATE
    assert win.overwrite_var.get() is False and win.seq_overwrite_var.get() is False
    # Untitled world, no sublevels -> cut is empty and collapses away
    win.cam_var.set("SmokeCam")
    body = win._resolve_name(win.name_tpl_var.get(), win._current_camera(), None)
    assert body == "Untitled_SmokeCam", body
    body2 = win._resolve_name("{seq}_{camera}", "Cam X", None)
    assert body2 == "Cam_X", body2
    win._update_name_preview()
    assert win.name_preview_var.get().startswith("→ Untitled_SmokeCam_"), win.name_preview_var.get()
    assert "素材名" in win.seq_name_preview_var.get()
    # the preview follows the template (trace)
    win.name_tpl_var.set("X_{camera}")
    win.root.update()
    assert win.name_preview_var.get().startswith("→ X_SmokeCam_"), win.name_preview_var.get()
    win.name_tpl_var.set(core.DEFAULT_NAME_TEMPLATE)
    s = win._collect_settings()
    assert s.name_body == "Untitled_SmokeCam", s.name_body
    assert core.out_basename(s, "Beauty", "001") == "Untitled_SmokeCam_Beauty_001"
    return body


step("name template widgets / _resolve_name / preview / out_basename", _name_widgets)


def _name_migration():
    p = win._settings_path()
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"name_usecustom": True, "name_custom": "MyShot", "name_usecam": True,
                   "seq_usecustom": True, "seq_custom": "SeqX"}, f)
    win._load_ui_state()
    assert win.name_tpl_var.get() == "MyShot_{camera}", win.name_tpl_var.get()
    assert win.seq_name_tpl_var.get() == "SeqX_{seq}", win.seq_name_tpl_var.get()
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"name_usecustom": False, "name_custom": "", "name_usecam": True}, f)
    win.name_tpl_var.set("Z")
    win._load_ui_state()
    assert win.name_tpl_var.get() == "Z"      # no custom name -> keep current/default
    win.name_tpl_var.set("{cut}_{camera}"); win.overwrite_var.set(True)
    win.seq_name_tpl_var.set("{seq}"); win.seq_overwrite_var.set(True)
    win._save_ui_state()
    win.name_tpl_var.set(""); win.overwrite_var.set(False)
    win.seq_name_tpl_var.set(""); win.seq_overwrite_var.set(False)
    win._load_ui_state()
    assert win.name_tpl_var.get() == "{cut}_{camera}" and win.overwrite_var.get() is True
    assert win.seq_name_tpl_var.get() == "{seq}" and win.seq_overwrite_var.get() is True
    win.name_tpl_var.set(core.DEFAULT_NAME_TEMPLATE); win.overwrite_var.set(False)
    win.seq_name_tpl_var.set(core.DEFAULT_NAME_TEMPLATE); win.seq_overwrite_var.set(False)
    os.remove(p)
    return True


step("name template settings migration + round-trip", _name_migration)


def _transfer():
    win.beauty_fmt_var.set("EXR 16bit (float)")
    win.depth_bit_var.set("EXR float")
    win.name_tpl_var.set("T_{camera}"); win.overwrite_var.set(True)
    win._transfer_from_image_tab()
    assert win.seq_beauty_fmt_var.get() == "EXR 16bit (float)"
    assert win.seq_depth_bit_var.get() == "EXR float"
    assert win.seq_name_tpl_var.get() == "T_{camera}" and win.seq_overwrite_var.get() is True
    win.name_tpl_var.set(core.DEFAULT_NAME_TEMPLATE); win.overwrite_var.set(False)
    win.seq_name_tpl_var.set(core.DEFAULT_NAME_TEMPLATE); win.seq_overwrite_var.set(False)
    return win.status_var.get()


step("_transfer_from_image_tab copies format/depth bit", _transfer)


def _roundtrip():
    win.seq_beauty_fmt_var.set("JPG 8bit")
    win.seq_depth_bit_var.set("16bit PNG")
    win._save_ui_state()
    p = win._settings_path()
    assert os.path.isfile(p) and not os.path.isfile(p + ".tmp")
    win.seq_beauty_fmt_var.set("PNG 8bit")
    win.seq_depth_bit_var.set("8bit PNG")
    win._load_ui_state()
    assert win.seq_beauty_fmt_var.get() == "JPG 8bit"
    assert win.seq_depth_bit_var.get() == "16bit PNG"
    # corrupt file must not break the panel
    with open(p, "w", encoding="utf-8") as f:
        f.write("[1,2,3]")
    win._load_ui_state()
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    win._load_ui_state()
    os.remove(p)
    return True


step("settings save/load round-trip (+corrupt file tolerated)", _roundtrip)


def _vp_exclusive():
    r = {}
    for tab, vp, fog, exl, fcb, ecb in (
            ("image", win.viewport_look_var, win.fog_off_var, win.exposure_lock_var,
             win.fog_off_cb, win.exposure_lock_cb),
            ("seq", win.seq_viewport_look_var, win.seq_fog_var, win.seq_exposure_lock_var,
             win.seq_fog_cb, win.seq_exposure_lock_cb)):
        assert vp.get() is False, tab
        fog.set(True); exl.set(True)
        vp.set(True); win.root.update()
        on = (fog.get(), exl.get(), "disabled" in fcb.state(), "disabled" in ecb.state())
        assert on == (False, False, True, True), (tab, on)
        vp.set(False); win.root.update()
        off = (fog.get(), exl.get(), "disabled" in fcb.state(), "disabled" in ecb.state())
        # OFF で有効に戻すが値は戻さない
        assert off == (False, False, False, False), (tab, off)
        r[tab] = {"on": on, "off": off}
    # 片方のタブの ON はもう片方に影響しない
    win.seq_fog_var.set(True)
    win.viewport_look_var.set(True)
    assert win.seq_fog_var.get() is True and "disabled" not in win.seq_fog_cb.state()
    win.viewport_look_var.set(False); win.seq_fog_var.set(False)
    return r


step("ビューポートのルック: exclusive with Fog/露出固定 on both tabs", _vp_exclusive)


def _vp_settings():
    p = win._settings_path()
    win.viewport_look_var.set(True); win.seq_viewport_look_var.set(True)
    win._save_ui_state()
    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["viewport_look"] is True and saved["seq_viewport_look"] is True, saved
    win.viewport_look_var.set(False); win.seq_viewport_look_var.set(False)
    win._load_ui_state()
    assert win.viewport_look_var.get() is True and win.seq_viewport_look_var.get() is True
    # 設定に Fog/露出固定とルックが同時 ON → ルックが勝つ
    saved.update({"fog_off": True, "exposure_lock": True, "seq_fog": True,
                  "seq_exposure_lock": True})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(saved, f)
    win.viewport_look_var.set(False); win.seq_viewport_look_var.set(False)
    win._load_ui_state()
    got = (win.viewport_look_var.get(), win.fog_off_var.get(), win.exposure_lock_var.get(),
           win.seq_viewport_look_var.get(), win.seq_fog_var.get(),
           win.seq_exposure_lock_var.get(), "disabled" in win.fog_off_cb.state())
    assert got == (True, False, False, True, False, False, True), got
    # 転送: 画像タブのルックを映像タブへ
    win.seq_viewport_look_var.set(False)
    win._transfer_from_image_tab()
    assert win.seq_viewport_look_var.get() is True
    win.viewport_look_var.set(False)
    win._transfer_from_image_tab()
    assert win.seq_viewport_look_var.get() is False and "disabled" not in win.seq_fog_cb.state()
    os.remove(p)
    return got


step("ビューポートのルック: settings round-trip / loaded Fog loses / transfer", _vp_settings)


def _vp_grab_fallback():
    # コマンドレットにはレベルビューポートが無い → 経路A は同期的に失敗して on_done(False)
    cam.camera_component.set_editor_property("post_process_blend_weight", 1.0)
    cube.set_is_temporarily_hidden_in_editor(False)
    got = []
    win._restore_hooks = []
    path = os.path.join(OUTD, "smoke_ui_vp.png")
    win._viewport_grab(cam, 64, 36, path, [cube], lambda ok, err: got.append((ok, str(err))))
    w = float(cam.camera_component.get_editor_property("post_process_blend_weight"))
    hidden = cube.is_temporarily_hidden_in_editor()
    assert got and got[0][0] is False, got
    assert w == 1.0 and hidden is False and not os.path.isfile(path), (w, hidden)
    return got[0]


step("ビューポートのルック: viewport grab without viewport -> on_done(False), state restored",
     _vp_grab_fallback)


def _out_dir_tokens():
    plain = os.path.join(OUTD, "plain")
    assert win._expand_out_dir(plain, "Cam", None) == plain
    got = win._expand_out_dir(os.path.join(OUTD, "{level}", "{camera}"), "Cam A", None)
    assert got == os.path.join(OUTD, "Untitled", "Cam_A"), got
    # the stored setting keeps the unexpanded template
    win.seq_out_var.set(os.path.join(OUTD, "{level}"))
    assert win._ui_state_dict()["seq_out"].endswith("{level}")
    win.seq_out_var.set(OUTD)
    return got


step("出力フォルダのトークン: 展開 / トークン無しは不変 / 設定は展開前", _out_dir_tokens)


def _topmost():
    p = win._settings_path()
    assert win.topmost_var.get() is False                       # default: unchanged behaviour
    assert int(win.root.attributes("-topmost")) == 0
    win.topmost_var.set(True)
    win._on_topmost_change()                                     # applies + saves
    assert int(win.root.attributes("-topmost")) == 1
    with open(p, "r", encoding="utf-8") as f:
        assert json.load(f)["topmost"] is True
    win.topmost_var.set(False)
    win._load_ui_state()                                         # restore re-applies
    got = (win.topmost_var.get(), int(win.root.attributes("-topmost")))
    assert got == (True, 1), got
    win.topmost_var.set(False)
    win._on_topmost_change()
    assert int(win.root.attributes("-topmost")) == 0
    # the worker's hidden panel ignores the setting
    win._headless = True
    win.topmost_var.set(True)
    win._apply_topmost()
    assert int(win.root.attributes("-topmost")) == 0
    win._headless = False
    win.topmost_var.set(False)
    os.remove(p)
    return got


step("⚙ 手前に表示: default off / apply / save / restore / headless ignores", _topmost)


def _bg_window():
    # queue window builds and follows the topmost setting; no jobs -> empty list
    win._bg_open_window()
    w = win._bg_win
    win._bg_refresh_window([{"id": "000001_x", "state": "queued", "name": "S Beauty",
                             "map": "/Game/M/L"}], 0.0)
    rows = [w["tree"].item(i, "values") for i in w["tree"].get_children()]
    assert rows and rows[0][0] == "待機中" and rows[0][2] == "L", rows
    win._bg_refresh_window([], 0.0)
    assert not w["tree"].get_children()
    w["top"].withdraw()
    return rows


step("バックグラウンド: 待ち行列ウィンドウ build / refresh", _bg_window)


def _confirm_overwrite():
    import tempfile
    d = tempfile.mkdtemp(prefix="smoke_ow_")
    asked = []
    orig = ui.messagebox.askokcancel

    def fake(title, msg, **kw):
        asked.append(msg)
        return fake.answer
    ui.messagebox.askokcancel = fake
    try:
        assert win._confirm_overwrite(d, "Shot", "001") is True and not asked   # nothing to delete: no dialog
        open(os.path.join(d, "Shot_Beauty_001.png"), "wb").close()
        fake.answer = False
        assert win._confirm_overwrite(d, "Shot", "001") is False
        assert len(asked) == 1 and "Shot_Beauty_001.png" in asked[0] and "1 件" in asked[0], asked
        assert os.path.exists(os.path.join(d, "Shot_Beauty_001.png"))            # asking deletes nothing
        fake.answer = True
        assert win._confirm_overwrite(d, "Shot", "001") is True and len(asked) == 2
        assert win._confirm_overwrite(d, "Shot", "002") is True and len(asked) == 2  # other take: no dialog
        win._headless = True                                                      # worker never asks
        assert win._confirm_overwrite(d, "Shot", "001") is True and len(asked) == 2
    finally:
        win._headless = False
        ui.messagebox.askokcancel = orig
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    return len(asked)


step("上書きの確認: 対象なしは聞かない / 対象ありは一覧つきで聞く / ワーカーは聞かない", _confirm_overwrite)


def _busy():
    win._set_busy(True)
    assert win._is_busy()
    assert "disabled" in win.capture_btn.state() and "disabled" in win.seq_btn.state()
    win._set_busy(False)
    assert not win._is_busy()
    assert "disabled" not in win.capture_btn.state()
    # guarded impl returning False -> busy cleared, hooks run
    calls = []
    def impl():
        win._restore_hooks.append(lambda: calls.append("hook"))
        return False
    win._run_guarded(impl)
    assert calls == ["hook"] and not win._is_busy()
    # guarded impl raising -> busy cleared, hooks run, exception propagates
    calls[:] = []
    def impl2():
        win._restore_hooks.append(lambda: calls.append("hook2"))
        raise RuntimeError("boom")
    try:
        win._run_guarded(impl2)
        raised = False
    except RuntimeError:
        raised = True
    assert raised and calls == ["hook2"] and not win._is_busy()
    # busy -> second click refused
    win._set_busy(True)
    win._run_guarded(lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    win._set_busy(False)
    return True


step("busy guard / restore hooks", _busy)


def _mrq_early():
    win.out_var.set("")
    r = win._on_mrq_impl()
    assert r is False, r
    return win.status_var.get()


step("_on_mrq_impl early return (no output dir)", _mrq_early)


def _seq_early():
    r = win._on_seq_render_impl()
    assert r is False, r
    return win.status_var.get()


step("_on_seq_render_impl early return (no sequencer open)", _seq_early)


def _make_scene_seq():
    at = unreal.AssetToolsHelpers.get_asset_tools()
    p = "/Game/SmokeSeq"
    if unreal.EditorAssetLibrary.does_asset_exist(p + "/SEQ_SmokeUI"):
        unreal.EditorAssetLibrary.delete_asset(p + "/SEQ_SmokeUI")
    seq = at.create_asset("SEQ_SmokeUI", p, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    ext = unreal.MovieSceneSequenceExtensions
    ext.set_display_rate(seq, unreal.FrameRate(24, 1))
    ext.set_playback_start(seq, 0)
    ext.set_playback_end(seq, 48)
    b1 = ext.add_possessable(seq, cam)
    b2 = ext.add_possessable(seq, cam2)
    cct = ext.add_track(seq, unreal.MovieSceneCameraCutTrack)
    s1 = cct.add_section(); s1.set_range(0, 24); s1.set_camera_binding_id(ext.get_binding_id(seq, b1))
    s2 = cct.add_section(); s2.set_range(24, 48); s2.set_camera_binding_id(ext.get_binding_id(seq, b2))
    return seq


seq = step("create 2-cut LevelSequence (cam@0-23, cam2@24-47)", _make_scene_seq)


def _cut_lookup():
    # get_bound_objects needs the Sequencer UI; headless it may return nothing -> None is acceptable,
    # but when it resolves it must pick the right camera per frame.
    a = ui.CaptureWindow._camera_cut_camera_for_frame(seq, 5)
    b = ui.CaptureWindow._camera_cut_camera_for_frame(seq, 30)
    res = {"f5": a.get_actor_label() if a else None, "f30": b.get_actor_label() if b else None}
    if a is not None:
        assert res["f5"] == "SmokeCam" and res["f30"] == "SmokeCam2", res
    # fallback path must still return a camera (never raise)
    c = win._sequence_camera_at(seq, 30)
    res["fallback_f30"] = c.get_actor_label() if c else None
    assert c is not None
    return res


step("_camera_cut_camera_for_frame / _sequence_camera_at", _cut_lookup)


def _vp_no_probe():
    # 露出合わせの試し撮り/参照撮影は廃止。静止画のビューポート撮影は「パイロット中」のときだけ
    assert not hasattr(win, "_seq_vp_calibrate") and not hasattr(ui, "_VPCAL_PROBE_DIR")
    import capture_core as core
    assert core.viewport_pilots(cam) is False          # コマンドレット: ビューポート無し
    assert not hasattr(core, "exposure_offset_ev") and not hasattr(core, "vpcal_cache_key")
    return {"pilots": False}


step("VC4 probe calibration removed; still viewport grab only while piloting (headless: not piloting)",
     _vp_no_probe)


def _cleanup():
    ui.close_all_windows()
    for a in (cube, cam, cam2):
        if a:
            eas.destroy_actor(a)
    if unreal.EditorAssetLibrary.does_asset_exist("/Game/SmokeSeq/SEQ_SmokeUI"):
        unreal.EditorAssetLibrary.delete_asset("/Game/SmokeSeq/SEQ_SmokeUI")
    return True


step("cleanup", _cleanup)

n_fail = sum(1 for r in RES if r[1] == "FAIL")
with open(os.path.join(OUTD, "smoke_ui_result_%s.json" % VSHORT), "w", encoding="utf-8") as f:
    json.dump({"engine": ver, "results": RES}, f, indent=1, ensure_ascii=False)
P("SUMMARY engine=%s steps=%d fail=%d" % (ver, len(RES), n_fail))
