# Smoke test of capture_bg (background render queue core). Pure Python: runs inside a UE
# commandlet (run_all_smokes.ps1) or with a plain Python (python smoke_bg.py).
# Exercises: queue ordering, atomic writes, cancel flags, lock liveness (stale lock, recycled
# PID = start-time mismatch, wrong image), crash recovery, no relaunch loop, hard stop only with
# the worker marker, worker command line.
import ast, json, os, shutil, subprocess, sys, tempfile, time, traceback

SP = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(os.path.dirname(SP)), "ue5_capture")
OUTD = os.environ.get("UE_CAPTURE_SMOKE_OUT") or SP
os.makedirs(OUTD, exist_ok=True)
RES = []
try:
    import unreal
    ver = unreal.SystemLibrary.get_engine_version()
except ImportError:
    unreal = None
    ver = "python-" + sys.version.split()[0]
VSHORT = ver.split("-")[0]


def P(msg):
    if unreal:
        unreal.log("[SMOKEBG] " + msg)
    print("[SMOKEBG] " + msg)


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


if TOOL not in sys.path:
    sys.path.insert(0, TOOL)
bg = step("import capture_bg", lambda: __import__("capture_bg"))
Q = tempfile.mkdtemp(prefix="smoke_bg_")
MY = os.getpid()
MY_IMAGE = (bg.proc_info(MY) or {}).get("image", "")
MY_PREFIX = MY_IMAGE[:6]           # matches this process (python / unrealeditor-cmd)


def _no_unreal_import():
    src = open(os.path.join(TOOL, "capture_bg.py"), encoding="utf-8").read()
    names = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            names.add(n.module)
    assert "unreal" not in names, names
    return sorted(names)


step("capture_bg imports no unreal (pure core)", _no_unreal_import)


def _order():
    a = bg.enqueue(Q, {"name": "A"})
    b = bg.enqueue(Q, {"name": "B"})
    c = bg.enqueue(Q, {"name": "C"})
    names = [j["name"] for j in bg.list_jobs(Q)]
    assert names == ["A", "B", "C"], names
    assert a["seq"] < b["seq"] < c["seq"] and a["state"] == "queued"
    assert not [f for f in os.listdir(os.path.join(Q, "jobs")) if f.endswith(".tmp")]
    j = bg.take_next(Q, 123)
    assert j["name"] == "A" and j["state"] == "running" and j["worker_pid"] == 123
    return names


step("enqueue order / take_next oldest / atomic (no .tmp left)", _order)


def _cancel():
    # B pending is flagged -> take_next skips it (cancelled) and returns C
    ids = {j["name"]: j["id"] for j in bg.list_jobs(Q)}
    with open(os.path.join(Q, "jobs", ids["B"] + ".cancel"), "w") as f:
        f.write("x")
    j = bg.take_next(Q, 123)
    st = {x["name"]: x["state"] for x in bg.list_jobs(Q)}
    assert j["name"] == "C" and st["B"] == "cancelled", (j, st)
    # running jobs: request_cancel_running flags the first running one (A)
    rid = bg.request_cancel_running(Q)
    assert rid == ids["A"] and bg.cancel_requested(Q, ids["A"])
    # cancel_pending without a worker marks queued ones cancelled directly
    d = bg.enqueue(Q, {"name": "D"})
    n = bg.cancel_pending(Q, worker_alive=False)
    assert n == 1 and {x["name"]: x["state"] for x in bg.list_jobs(Q)}["D"] == "cancelled"
    # with a worker only the flag is written (worker owns the files)
    e = bg.enqueue(Q, {"name": "E"})
    bg.cancel_pending(Q, worker_alive=True)
    ej = [x for x in bg.list_jobs(Q) if x["name"] == "E"][0]
    assert ej["state"] == "queued" and bg.cancel_requested(Q, e["id"])
    return st


step("cancel flags: pending skipped / running flagged / pending with and without worker", _cancel)


def _dead_pid():
    p = subprocess.Popen([sys.executable if unreal is None else _python(), "-c", "pass"],
                         creationflags=0x08000000)
    p.wait()
    return p.pid


def _python():
    for c in (os.path.join(sys.base_prefix, "python.exe"), os.path.join(sys.prefix, "python.exe"),
              sys.executable):
        if c and os.path.isfile(c) and os.path.basename(c).lower().startswith("python"):
            return c
    return sys.executable


def _lock():
    q = tempfile.mkdtemp(prefix="smoke_bg_lock_")
    info = bg.proc_info(MY)
    assert info and info["alive"] and info["start_time"] > 0, info
    assert bg.acquire_lock(q, MY, "P", image_prefix=MY_PREFIX)
    assert not bg.acquire_lock(q, MY, "P", image_prefix=MY_PREFIX)        # live owner
    assert bg.live_worker(q, MY_PREFIX)[0] == "running"
    # wrong image prefix -> not a worker
    assert bg.live_worker(q, "unrealeditor-nope") is None
    # recycled PID: same pid, different start time -> not alive
    rec = bg.read_lock(q)
    rec2 = dict(rec, start_time=rec["start_time"] - 1)
    assert not bg.is_alive(rec2, MY_PREFIX)
    # stale lock: dead pid -> acquire succeeds after removing it
    bg.release_lock(q, MY)
    assert bg.read_lock(q) is None
    bg._write_json(os.path.join(q, "worker.lock"), {"pid": _dead_pid(), "start_time": 1})
    assert bg.live_worker(q, MY_PREFIX) is None
    assert bg.acquire_lock(q, MY, "P", image_prefix=MY_PREFIX)
    bg.release_lock(q, MY)
    shutil.rmtree(q, ignore_errors=True)
    return {"image": MY_IMAGE}


step("lock liveness: live / wrong image / recycled pid / stale lock", _lock)


def _recover():
    q = tempfile.mkdtemp(prefix="smoke_bg_rec_")
    a = bg.enqueue(q, {"name": "A"}); b = bg.enqueue(q, {"name": "B"}); c = bg.enqueue(q, {"name": "C"})
    bg.take_next(q, 1)                                 # A running
    dead = _dead_pid()
    bg._write_json(os.path.join(q, "worker.lock"), {"pid": dead, "start_time": 5})
    bg.note_launch(q, pid=dead, start_time=5, launched_at=time.time(), acquired=True, jobs_taken=1)
    s = bg.poll(q, image_prefix=MY_PREFIX)             # dead worker -> recover
    st = {j["name"]: j["state"] for j in bg.list_jobs(q)}
    assert st == {"A": "failed", "B": "queued", "C": "queued"}, st
    assert bg.read_lock(q) is None and s["needs_start"] and s["waiting"] == 2, s
    # a job cancelled-and-running when the worker died ends as cancelled, not failed
    bg.take_next(q, 1)
    bg.request_cancel_running(q)
    bg.recover(q, MY_PREFIX)
    st = {j["name"]: j["state"] for j in bg.list_jobs(q)}
    assert st["B"] == "cancelled", st
    # a worker that died before taking any job is not relaunched (no relaunch loop)
    bg.note_launch(q, pid=_dead_pid(), start_time=5, launched_at=time.time(), acquired=False,
                   jobs_taken=0)
    s = bg.poll(q, image_prefix=MY_PREFIX)
    assert not s["needs_start"] and s["start_blocked"], s
    shutil.rmtree(q, ignore_errors=True)
    return st


step("crash recovery: running->failed, cancel->cancelled, no relaunch after startup death", _recover)


def _hard_stop():
    q = tempfile.mkdtemp(prefix="smoke_bg_kill_")
    # our own process must never be stopped (no worker marker on its command line)
    info = bg.proc_info(MY)
    bg._write_json(os.path.join(q, "worker.lock"), {"pid": MY, "start_time": info["start_time"]})
    assert bg.hard_stop(q, image_prefix=MY_PREFIX) is False
    assert bg.proc_info(MY)["alive"]
    os.remove(os.path.join(q, "worker.lock"))
    # a child carrying the marker is stopped, and its running job is closed
    py = _python()
    p = subprocess.Popen([py, "-c", "import time; time.sleep(60)", bg.WORKER_MARKER],
                         creationflags=0x08000000)
    time.sleep(0.5)
    ci = bg.proc_info(p.pid)
    j = bg.enqueue(q, {"name": "K"}); bg.take_next(q, p.pid)
    bg._write_json(os.path.join(q, "worker.lock"), {"pid": p.pid, "start_time": ci["start_time"]})
    prefix = os.path.basename(py).lower()[:6]
    assert bg.hard_stop(q, image_prefix=prefix) is True
    rc = p.wait(timeout=10)
    st = bg.list_jobs(q)[0]["state"]
    assert st == "failed", st
    shutil.rmtree(q, ignore_errors=True)
    return {"rc": rc, "job": st, "cmdline_ok": True}


step("hard stop: refuses without marker / stops marked child / closes its job", _hard_stop)


def _cmd():
    c = bg.worker_command(r"C:\UE\UnrealEditor.exe", r"C:\P\P.uproject", r"C:\x\w.py", r"C:\l.log")
    for t in ("-RenderOffScreen", "-Unattended", "-nowrite", bg.WORKER_MARKER, '-ExecCmds="py C:/x/w.py"'):
        assert t in c, (t, c)
    assert "-ExecutePythonScript" not in c
    return c


step("worker command line", _cmd)


def _prune():
    q = tempfile.mkdtemp(prefix="smoke_bg_prune_")
    ids = []
    for i in range(6):
        j = bg.enqueue(q, {"name": "P%d" % i})
        ids.append(j["id"])
        if i < 5:
            bg.update_job(q, j["id"], state="done", finished=time.time())
    os.makedirs(bg.logs_dir(q), exist_ok=True)
    open(os.path.join(bg.logs_dir(q), ids[0] + ".log"), "w").close()
    n = bg.prune(q, keep=2)
    left = [j["id"] for j in bg.list_jobs(q)]
    assert n == 3 and left == ids[3:], (n, left)            # 2 finished + the queued one stay
    assert not os.path.exists(os.path.join(bg.logs_dir(q), ids[0] + ".log"))
    assert bg.enqueue(q, {"name": "next"})["seq"] == 7      # numbering keeps increasing
    shutil.rmtree(q, ignore_errors=True)
    return left


step("prune: oldest finished removed, queued kept, numbering monotonic", _prune)

shutil.rmtree(Q, ignore_errors=True)
n_fail = sum(1 for r in RES if r[1] == "FAIL")
with open(os.path.join(OUTD, "smoke_bg_result_%s.json" % VSHORT), "w", encoding="utf-8") as f:
    json.dump({"engine": ver, "results": RES}, f, indent=1, ensure_ascii=False)
P("SUMMARY engine=%s steps=%d fail=%d" % (ver, len(RES), n_fail))
