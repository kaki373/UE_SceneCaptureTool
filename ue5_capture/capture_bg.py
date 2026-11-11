# -*- coding: utf-8 -*-
"""
capture_bg.py  --  background render queue (one offscreen worker editor per project)

The panel enqueues jobs (a snapshot of its settings + map + sequence); a second editor started
with -RenderOffScreen (capture_bg_worker.py) drains them one at a time through the panel's own
handler, so every pass renders exactly as in the foreground.

Layout under <Project>/Saved/UE5Capture_bg/:
  jobs/<NNNNNN>_<id>.json   one job per file, ordered by NNNNNN; state queued/running/done/failed/cancelled
  jobs/<id>.cancel          cancel request (pending: skip it / running: worker cancels cooperatively)
  worker.lock               {pid, start_time, ...} of the live worker (start time defeats PID reuse)
  launch.json               the last launch {pid, start_time, acquired, jobs_taken}
  logs/                     worker editor log (-abslog) and one log per job

Writers: the panel creates job files and cancel flags; while a worker is alive only the worker
rewrites job files (no lost updates). With no live worker the panel recovers states itself.
Pure Python (no `unreal` import) so tests/headless/smoke_bg.py can exercise it anywhere.
"""

import json
import os
import subprocess
import sys
import time
import uuid

QUEUE_DIRNAME = "UE5Capture_bg"
WORKER_MARKER = "-UE5CaptureBgWorker"
STATES = ("queued", "running", "done", "failed", "cancelled")
FINISHED = ("done", "failed", "cancelled")
# Cooperative cancel: after this the panel hard-stops the worker (by verified PID).
CANCEL_ACK_TIMEOUT = 60.0
# A launched worker that has not taken the lock within this time is considered dead.
STARTUP_TIMEOUT = 900.0
# The worker's UE_MCP_Bridge (if the project has it) must not bind the user's bridge port.
_WORKER_BRIDGE_PORT = 9897


def queue_dir(saved_dir):
    return os.path.join(saved_dir, QUEUE_DIRNAME)


def _jobs_dir(q):
    return os.path.join(q, "jobs")


def logs_dir(q):
    return os.path.join(q, "logs")


def _write_json(path, data):
    """Atomic: temp file in the same folder + os.replace (readers never see half a file)."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ process helpers (Windows)
def _k32():
    import ctypes
    return ctypes.windll.kernel32


def proc_info(pid):
    """{"alive", "start_time", "image"} for pid, or None if it cannot be opened.
    start_time is the process creation FILETIME (100 ns ticks); image is the exe basename (lower)."""
    import ctypes
    from ctypes import wintypes
    if not pid or os.name != "nt":
        return None
    k = _k32()
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))        # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        alive = bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        ft = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h, *[ctypes.byref(x) for x in ft]):
            return None
        start = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        image = ""
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            image = os.path.basename(buf.value).lower()
        return {"alive": alive, "start_time": int(start), "image": image}
    finally:
        k.CloseHandle(h)


def proc_cmdline(pid):
    """Command line of pid via NtQueryInformationProcess(ProcessCommandLineInformation=60)."""
    import ctypes
    from ctypes import wintypes
    if not pid or os.name != "nt":
        return None
    k = _k32()
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None
    try:
        size = wintypes.ULONG(0)
        nt = ctypes.windll.ntdll
        nt.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if nt.NtQueryInformationProcess(h, 60, buf, size.value, ctypes.byref(size)) != 0:
            return None

        class _US(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]
        us = _US.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    finally:
        k.CloseHandle(h)


def _terminate(pid):
    from ctypes import wintypes
    k = _k32()
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x0001, False, int(pid))         # PROCESS_TERMINATE
    if not h:
        return False
    try:
        return bool(k.TerminateProcess(h, 1))
    finally:
        k.CloseHandle(h)


def is_alive(rec, image_prefix="unrealeditor"):
    """rec {"pid", "start_time"} names a live process: same PID, same creation time (PID reuse
    safe) and an UnrealEditor image."""
    if not rec or not rec.get("pid"):
        return False
    info = proc_info(rec["pid"])
    return bool(info and info["alive"] and info["start_time"] == rec.get("start_time")
                and info["image"].startswith(image_prefix))


# ------------------------------------------------------------------ jobs
def _job_path(q, job_id):
    return os.path.join(_jobs_dir(q), job_id + ".json")


def _cancel_path(q, job_id):
    return os.path.join(_jobs_dir(q), job_id + ".cancel")


def list_jobs(q):
    """All jobs, oldest first (file names start with the zero-padded sequence number)."""
    d = _jobs_dir(q)
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            j = _read_json(os.path.join(d, f))
            if j and j.get("id"):
                out.append(j)
    return out


def enqueue(q, job):
    """Adds job (dict) as queued and returns it with id/seq/state/created filled in."""
    d = _jobs_dir(q)
    os.makedirs(d, exist_ok=True)
    seqs = [int(f[:6]) for f in os.listdir(d) if f[:6].isdigit()]
    job = dict(job)
    job["seq"] = (max(seqs) + 1) if seqs else 1
    job["id"] = "%06d_%s" % (job["seq"], uuid.uuid4().hex[:8])
    job["state"] = "queued"
    job["created"] = time.time()
    _write_json(_job_path(q, job["id"]), job)
    return job


# Finished jobs kept in the queue list; older ones are removed at the next enqueue.
KEEP_FINISHED = 20
_KEEP_WORKER_LOGS = 5


def prune(q, keep=KEEP_FINISHED):
    """Removes the oldest finished jobs beyond `keep` (job file, cancel flag, per-job log and
    settings snapshot) and old worker logs, so the panel's poll stays cheap. Returns the count."""
    fin = [j for j in list_jobs(q) if j.get("state") in FINISHED]
    old = fin[:-keep] if keep else fin
    lg = logs_dir(q)
    paths = []
    for j in old:
        paths += [_job_path(q, j["id"]), _cancel_path(q, j["id"]),
                  os.path.join(lg, j["id"] + ".log"), os.path.join(lg, j["id"] + "_settings.json")]
    if os.path.isdir(lg):
        wl = sorted(f for f in os.listdir(lg) if f.startswith("worker_") and f.endswith(".log"))
        paths += [os.path.join(lg, f) for f in wl[:-_KEEP_WORKER_LOGS]]
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    return len(old)


def update_job(q, job_id, **fields):
    j = _read_json(_job_path(q, job_id)) or {"id": job_id}
    j.update(fields)
    _write_json(_job_path(q, job_id), j)
    return j


def cancel_requested(q, job_id):
    return os.path.isfile(_cancel_path(q, job_id))


def cancel_requested_at(q, job_id):
    try:
        return os.path.getmtime(_cancel_path(q, job_id))
    except OSError:
        return None


def _flag(q, job_id):
    p = _cancel_path(q, job_id)
    if not os.path.isfile(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(time.time()))


def request_cancel_running(q):
    """Flags the running job; the worker cancels it on its tick. Returns its id or None."""
    for j in list_jobs(q):
        if j.get("state") == "running":
            _flag(q, j["id"])
            return j["id"]
    return None


def cancel_pending(q, worker_alive):
    """Cancels every queued job. With a live worker only flags are written (the worker owns the
    job files and finalises them); without one the panel marks them cancelled directly."""
    n = 0
    for j in list_jobs(q):
        if j.get("state") == "queued":
            _flag(q, j["id"])
            if not worker_alive:
                update_job(q, j["id"], state="cancelled", finished=time.time(),
                           message="取り消し（待機中）")
            n += 1
    return n


def cancel_job(q, job_id, worker_alive):
    """Cancels one queued job (same ownership rule as cancel_pending). True if it was queued."""
    j = _read_json(_job_path(q, job_id))
    if not j or j.get("state") != "queued":
        return False
    _flag(q, job_id)
    if not worker_alive:
        update_job(q, job_id, state="cancelled", finished=time.time(), message="取り消し（待機中）")
    return True


def take_next(q, worker_pid):
    """Worker side: the oldest queued job becomes running (flagged ones become cancelled)."""
    for j in list_jobs(q):
        if j.get("state") != "queued":
            continue
        if cancel_requested(q, j["id"]):
            update_job(q, j["id"], state="cancelled", finished=time.time(),
                       message="取り消し（待機中）")
            continue
        return update_job(q, j["id"], state="running", started=time.time(), worker_pid=worker_pid)
    return None


# ------------------------------------------------------------------ lock / launch record
def _lock_path(q):
    return os.path.join(q, "worker.lock")


def _launch_path(q):
    return os.path.join(q, "launch.json")


def read_lock(q):
    return _read_json(_lock_path(q))


def read_launch(q):
    return _read_json(_launch_path(q))


def acquire_lock(q, pid, project, image_prefix="unrealeditor"):
    """Worker side. True when this process now owns the queue. A stale lock (dead or recycled
    PID) is removed first; a live one makes this return False."""
    os.makedirs(q, exist_ok=True)
    cur = read_lock(q)
    if cur is not None:
        if is_alive(cur, image_prefix):
            return False
        try:
            os.remove(_lock_path(q))
        except OSError:
            pass
    info = proc_info(pid) or {}
    rec = {"pid": int(pid), "start_time": info.get("start_time"), "image": info.get("image"),
           "project": project, "since": time.time()}
    try:
        fd = os.open(_lock_path(q), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    return True


def release_lock(q, pid):
    cur = read_lock(q)
    if cur and int(cur.get("pid") or 0) == int(pid):
        try:
            os.remove(_lock_path(q))
        except OSError:
            pass


def note_launch(q, **fields):
    rec = read_launch(q) or {}
    rec.update(fields)
    _write_json(_launch_path(q), rec)


def live_worker(q, image_prefix="unrealeditor", now=None):
    """("running", lock) / ("starting", launch record) / None."""
    lock = read_lock(q)
    if lock and is_alive(lock, image_prefix):
        return ("running", lock)
    rec = read_launch(q)
    now = time.time() if now is None else now
    if (rec and not rec.get("acquired") and now - float(rec.get("launched_at") or 0) < STARTUP_TIMEOUT
            and is_alive(rec, image_prefix)):
        return ("starting", rec)
    return None


def recover(q, image_prefix="unrealeditor"):
    """With no live worker: drop a stale lock and close jobs left running by a dead worker
    (failed, or cancelled when a cancel was requested). Never retried. Returns their ids."""
    if live_worker(q, image_prefix) is not None:
        return []
    if read_lock(q) is not None:
        try:
            os.remove(_lock_path(q))
        except OSError:
            pass
    done = []
    for j in list_jobs(q):
        if j.get("state") == "running":
            if cancel_requested(q, j["id"]):
                update_job(q, j["id"], state="cancelled", finished=time.time(),
                           message="中断（ワーカー停止）")
            else:
                update_job(q, j["id"], state="failed", finished=time.time(),
                           message="ワーカーがジョブ途中で終了しました（ログ参照）")
            done.append(j["id"])
    return done


def should_autostart(q, image_prefix="unrealeditor"):
    """Queued jobs, no live worker, and the last worker made progress (took >= 1 job).
    A worker that died before taking any job is not relaunched automatically (no relaunch loop);
    the next explicit enqueue starts one."""
    if live_worker(q, image_prefix) is not None:
        return False
    if not any(j.get("state") == "queued" and not cancel_requested(q, j["id"])
               for j in list_jobs(q)):
        return False
    rec = read_launch(q)
    return rec is None or int(rec.get("jobs_taken") or 0) > 0


def worker_command(engine_exe, uproject, worker_script, log_path):
    """Offscreen editor that runs worker_script. -nowrite: never save the user's ini files.
    -ExecCmds, not -ExecutePythonScript (5.8 quits right after the latter)."""
    script = worker_script.replace("\\", "/")
    return ('"%s" "%s" -RenderOffScreen -Unattended -NoSplash -nowrite -SCCProvider=None '
            '-MCPBridgePort=%d -abslog="%s" %s -ExecCmds="py %s"'
            % (engine_exe, uproject, _WORKER_BRIDGE_PORT, log_path, WORKER_MARKER, script))


def launch_worker(q, engine_exe, uproject, worker_script):
    """Starts a worker detached, minimised and not activated (it renders offscreen anyway).
    Returns its PID."""
    os.makedirs(logs_dir(q), exist_ok=True)
    log = os.path.join(logs_dir(q), "worker_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
    cmd = worker_command(engine_exe, uproject, worker_script, log)
    pid = _create_outside_parent(cmd, os.path.dirname(uproject))
    if not pid:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                               # SW_SHOWMINNOACTIVE
        pid = subprocess.Popen(cmd, startupinfo=si, close_fds=True,
                               creationflags=0x00000008 | 0x00000200).pid  # DETACHED | NEW_GROUP
    info = proc_info(pid) or {}
    _write_json(_launch_path(q), {"pid": pid, "start_time": info.get("start_time"),
                                  "launched_at": time.time(), "acquired": False,
                                  "jobs_taken": 0, "log": log})
    return pid


def _create_outside_parent(cmd, cwd):
    """Starts cmd via WMI Win32_Process.Create (minimised, not activated) and returns its PID,
    or None. Windows lets a child of the foreground process take the foreground; the worker's
    PIE start then stole focus from the user's editor (measured on 5.8). A WMI-created process
    is not our child, so it cannot."""
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
          "-Property @{ShowWindow=[uint16]7}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          "@{CommandLine=$env:UE5CAP_BG_CMD; CurrentDirectory=$env:UE5CAP_BG_CWD; "
          "ProcessStartupInformation=$si}; "
          "Write-Output ('{0} {1}' -f $r.ReturnValue, $r.ProcessId)")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           env=dict(os.environ, UE5CAP_BG_CMD=cmd, UE5CAP_BG_CWD=cwd),
                           capture_output=True, text=True, timeout=60,
                           creationflags=0x08000000)     # CREATE_NO_WINDOW
        rc, pid = r.stdout.split()[:2]
        return int(pid) if int(rc) == 0 and int(pid) > 0 else None
    except Exception:
        return None


def hard_stop(q, image_prefix="unrealeditor", marker=WORKER_MARKER):
    """Terminates the worker only after re-verifying PID + start time + image + the worker
    marker on its command line. Never touches any other process. True when it was stopped."""
    for rec in (read_lock(q), read_launch(q)):
        if not rec or not is_alive(rec, image_prefix):
            continue
        cl = proc_cmdline(rec["pid"]) or ""
        if marker.lower() not in cl.lower():
            continue
        if _terminate(rec["pid"]):
            for _ in range(50):
                if not is_alive(rec, image_prefix):
                    break
                time.sleep(0.1)
            recover(q, image_prefix)
            return True
    return False


def poll(q, image_prefix="unrealeditor", now=None):
    """Panel tick: recover after a dead worker, hard-stop a worker that ignored a cancel for
    CANCEL_ACK_TIMEOUT, and summarise. Returns
    {"worker", "running", "waiting", "finished", "needs_start", "start_blocked", "hard_stopped"}."""
    now = time.time() if now is None else now
    hard = False
    lw = live_worker(q, image_prefix, now)
    jobs = list_jobs(q)
    running = next((j for j in jobs if j.get("state") == "running"), None)
    if lw is None:
        if recover(q, image_prefix):
            jobs = list_jobs(q)
            running = None
    elif running is not None:
        t = cancel_requested_at(q, running["id"])
        if t is not None and now - t > CANCEL_ACK_TIMEOUT:
            hard = hard_stop(q, image_prefix)
            if hard:
                jobs = list_jobs(q)
                running = None
                lw = None
    waiting = [j for j in jobs if j.get("state") == "queued" and not cancel_requested(q, j["id"])]
    finished = [j for j in jobs if j.get("state") in FINISHED]
    needs = bool(waiting) and lw is None and should_autostart(q, image_prefix)
    blocked = bool(waiting) and lw is None and not needs
    return {"worker": lw[0] if lw else None, "running": running, "waiting": len(waiting),
            "finished": finished, "needs_start": needs, "start_blocked": blocked,
            "hard_stopped": hard}


def default_engine_exe(engine_dir):
    return os.path.normpath(os.path.join(engine_dir, "Binaries", "Win64", "UnrealEditor.exe"))


def worker_script_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_bg_worker.py")


if __name__ == "__main__":       # pragma: no cover - manual status check
    for j in list_jobs(sys.argv[1]):
        print(j.get("id"), j.get("state"), j.get("name"), j.get("message", ""))
