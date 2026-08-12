@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  UE Scene Capture Tool - installer (single self-contained file)
rem
rem  Usage:
rem    Install.bat                       ... interactive
rem    Install.bat "X:\Proj\Foo.uproject" [more.uproject ...]
rem    Install.bat "D:\Unreal\UE_5.7"    ... force a specific engine root
rem    Install.bat --yes [--no-pause]    ... unattended
rem
rem  This batch only locates a Python interpreter; everything else lives in the
rem  Python payload below the marker line at the bottom of this file.
rem
rem  Exit codes (which step failed):
rem     0  success
rem    10  tool body (ue5_capture) not found next to this file
rem    20  Unreal Engine could not be located
rem    30  required Python libraries could not be installed
rem    40  editor start-up script could not be written
rem    50  a selected .uproject could not be updated
rem    90  unexpected error
rem   130  aborted (Ctrl-C)
rem ===========================================================================
title UE Scene Capture Tool Installer

rem shift は %0 もずらすので、自分自身のパスは先に退避しておく
set "SELF=%~f0"
set "PY="

rem --- 1) explicit override -------------------------------------------------
if defined UE_SCENECAPTURE_PYTHON if exist "%UE_SCENECAPTURE_PYTHON%" set "PY=%UE_SCENECAPTURE_PYTHON%"

rem --- 2) registered Unreal Engine installs (5.7 first, then any) -----------
if not defined PY (
  for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\EpicGames\Unreal Engine\5.7" /v InstalledDirectory 2^>nul ^| findstr /i "InstalledDirectory"') do call :try_engine "%%B"
)
if not defined PY (
  for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\EpicGames\Unreal Engine" /s /v InstalledDirectory 2^>nul ^| findstr /i "InstalledDirectory"') do call :try_engine "%%B"
)
if not defined PY (
  for /f "tokens=2*" %%A in ('reg query "HKCU\SOFTWARE\EpicGames\Unreal Engine" /s /v InstalledDirectory 2^>nul ^| findstr /i "InstalledDirectory"') do call :try_engine "%%B"
)

rem --- 3) common install locations -----------------------------------------
if not defined PY (
  for %%D in (C D E F G X) do (
    call :try_engine "%%D:\Program Files\Epic Games\UE_5.7"
    call :try_engine "%%D:\Epic Games\UE_5.7"
    call :try_engine "%%D:\Unreal\UE_5.7"
    call :try_engine "%%D:\UE_5.7"
  )
)

rem --- 4) any working Python 3 on PATH (the payload finds the engine itself) -
rem     "where python" can return the Windows Store stub, which opens the Store
rem     instead of running, so every candidate is smoke-tested before use.
set "PYARG="
if not defined PY (
  for /f "delims=" %%P in ('where python 2^>nul') do call :try_python "%%P"
)
if not defined PY (
  py -3 -c "import sys" >nul 2>&1 && (set "PY=py" & set "PYARG=-3")
)

if not defined PY (
  echo.
  echo [ERROR] Could not find a Python interpreter.
  echo         Unreal Engine 5.7 does not look installed in a standard location.
  echo         Re-run with the engine root, e.g.:
  echo             Install.bat "D:\Unreal\UE_5.7"
  echo.
  pause
  exit /b 20
)

rem --- scan args for --no-pause (shift does not consume %*) ------------------
set "NOPAUSE="
:scan_args
if "%~1"=="" goto scan_done
if /i "%~1"=="--no-pause" set "NOPAUSE=1"
shift
goto scan_args
:scan_done

"%PY%" %PYARG% -c "import io,sys;p=sys.argv[1];t=io.open(p,encoding='utf-8').read();h,_,b=t.partition('#==='+'PYTHON===');exec(compile('\n'*h.count('\n')+b,p,'exec'))" "%SELF%" %*
set "RC=%ERRORLEVEL%"

if not defined NOPAUSE pause
exit /b %RC%

:try_engine
if defined PY goto :eof
set "_CAND=%~1\Engine\Binaries\ThirdParty\Python3\Win64\python.exe"
if exist "%_CAND%" set "PY=%_CAND%"
goto :eof

:try_python
if defined PY goto :eof
"%~1" -c "import sys" >nul 2>&1 || goto :eof
set "PY=%~1"
goto :eof

#===PYTHON===
# -*- coding: utf-8 -*-
"""UE Scene Capture Tool インストーラ本体（Install.bat に埋め込まれている）。

やること:
  1. Unreal Engine 5.7 を検出する
  2. 同梱 Python に numpy / Pillow / imageio / imageio-ffmpeg を入れる
  3. Documents/UnrealEngine/Python に起動スクリプトを置く（全プロジェクト共通）
  4. 選んだ .uproject で必要プラグインを有効化する
"""

import glob
import io
import json
import os
import re
import subprocess
import sys

try:
    import winreg
except ImportError:                                   # 非 Windows は対象外
    winreg = None

# --------------------------------------------------------------------- 定数
TOOL_TITLE = "UE Scene Capture Tool"
BOOTSTRAP_MODULE = "ue5_capture_bootstrap"
BEGIN_MARK = "# >>> UE Scene Capture Tool (Install.bat) >>>"
END_MARK = "# <<< UE Scene Capture Tool (Install.bat) <<<"

# import 名 -> pip パッケージ名 / 用途 / 必須か
PACKAGES = [
    ("numpy",          "numpy",          "AA縮小・Depth正規化・マスク合成", True),
    ("PIL",            "Pillow",         "PNG 入出力・合成",                True),
    ("imageio",        "imageio",        "Z-Depth の EXR 出力（任意）",     False),
    ("imageio_ffmpeg", "imageio-ffmpeg", "MP4 エンコード用 ffmpeg（任意）", False),
]

# .uproject で有効化するプラグイン（どちらも EnabledByDefault=false）。
# SequencerScripting / LevelSequenceEditor は MovieRenderPipeline の依存として
# 自動で有効になるので、.uproject への追記は行わない（差分を最小にする）。
PLUGINS = [
    ("PythonScriptPlugin", "エディタ内 Python（ツール本体の実行に必須）"),
    ("MovieRenderPipeline", "Movie Render Queue（レンダと Sequencer API に必須）"),
]

AUTO_YES = False

# 終了コード（どの工程で失敗したかを表す。バッチはこの値をそのまま返す）
EXIT_OK = 0
EXIT_NO_TOOL = 10          # ツール本体（ue5_capture）が Install.bat の隣に無い
EXIT_NO_ENGINE = 20        # UE 5.7 を特定できなかった
EXIT_DEPS = 30             # 必須 Python ライブラリを入れられなかった
EXIT_BOOTSTRAP = 40        # 起動スクリプトを設置できなかった
EXIT_PROJECT = 50          # 選んだプロジェクトの .uproject を更新できなかった
EXIT_UNEXPECTED = 90       # 想定外の例外
EXIT_ABORTED = 130         # Ctrl-C

EXIT_REASON = {
    EXIT_NO_TOOL: "ツール本体 (ue5_capture) が見つからない",
    EXIT_NO_ENGINE: "Unreal Engine を特定できない",
    EXIT_DEPS: "必須 Python ライブラリの導入に失敗",
    EXIT_BOOTSTRAP: "起動スクリプトの設置に失敗",
    EXIT_PROJECT: "プロジェクト (.uproject) の更新に失敗",
    EXIT_UNEXPECTED: "想定外のエラー",
    EXIT_ABORTED: "中断された",
}


# ----------------------------------------------------------------- 小道具
def out(msg=""):
    try:
        print(msg)
    except UnicodeEncodeError:                        # cp932 に無い文字よけ
        print(msg.encode("ascii", "replace").decode("ascii"))
    sys.stdout.flush()


def pad(text, width):
    """全角を2桁として右詰めスペースを足す（結果表の桁を揃えるため）。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(1, width - w)


def head(title):
    out("")
    out("-" * 74)
    out(" " + title)
    out("-" * 74)


def ask(prompt, default=""):
    if AUTO_YES:
        out(prompt + "  -> [--yes] %s" % (default or "(既定)"))
        return default
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    a = ask("%s [%s]: " % (prompt, d), "y" if default else "n").lower()
    if not a:
        return default
    return a.startswith("y")


def documents_dir():
    """UE の FPlatformProcess::UserDir() と同じ「ドキュメント」フォルダ。

    OneDrive リダイレクト等があるのでレジストリの User Shell Folders を見る。"""
    if winreg is not None:
        try:
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                val = winreg.QueryValueEx(k, "Personal")[0]
            val = os.path.expandvars(val)
            if os.path.isdir(val):
                return val
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".ue5capture_write_test")
        with io.open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


# ------------------------------------------------------------ エンジン検出
def engine_python(root):
    p = os.path.join(root, "Engine", "Binaries", "ThirdParty",
                     "Python3", "Win64", "python.exe")
    return p if os.path.isfile(p) else None


def engine_version(root):
    try:
        with io.open(os.path.join(root, "Engine", "Build", "Build.version"),
                     encoding="utf-8-sig") as f:
            d = json.load(f)
        return (int(d.get("MajorVersion", 0)), int(d.get("MinorVersion", 0)),
                int(d.get("PatchVersion", 0)))
    except Exception:
        m = re.search(r"UE[_-](\d+)\.(\d+)", root)
        if m:
            return (int(m.group(1)), int(m.group(2)), 0)
        return (0, 0, 0)


def _reg_engine_roots():
    roots = []
    if winreg is None:
        return roots
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\EpicGames\Unreal Engine",
                                    0, winreg.KEY_READ | view) as k:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(k, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(k, name) as sub:
                                roots.append(winreg.QueryValueEx(
                                    sub, "InstalledDirectory")[0])
                        except Exception:
                            pass
            except Exception:
                pass
    return roots


def _launcher_engine_roots():
    dat = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                       "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat")
    try:
        with io.open(dat, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return []
    return [e.get("InstallLocation", "") for e in data.get("InstallationList", [])
            if str(e.get("AppName", "")).startswith("UE_")]


def _common_engine_roots():
    roots = []
    parents = []
    for d in "CDEFGHXYZ":
        drive = "%s:\\" % d
        if not os.path.isdir(drive):
            continue
        parents += [os.path.join(drive, "Program Files", "Epic Games"),
                    os.path.join(drive, "Epic Games"),
                    os.path.join(drive, "Unreal"),
                    drive]
    for parent in parents:
        try:
            for name in os.listdir(parent):
                if name.upper().startswith("UE_5"):
                    roots.append(os.path.join(parent, name))
        except Exception:
            pass
    return roots


def find_engines():
    """{path, version, python} のリスト（新しい順）を返す。"""
    seen, found = set(), []
    for root in (_reg_engine_roots() + _launcher_engine_roots()
                 + _common_engine_roots()):
        if not root:
            continue
        root = os.path.normpath(root)
        key = os.path.normcase(root)
        if key in seen:
            continue
        seen.add(key)
        py = engine_python(root)
        if py:
            found.append({"path": root, "version": engine_version(root),
                          "python": py})
    found.sort(key=lambda e: e["version"], reverse=True)
    return found


def pick_engine(forced_roots):
    head("[1/4] Unreal Engine 5.7 を探す")

    for root in forced_roots:                          # 引数で指定されたもの優先
        root = os.path.normpath(root)
        py = engine_python(root)
        if py:
            v = engine_version(root)
            out("指定されたエンジンを使います: %s  (UE %d.%d.%d)"
                % (root, v[0], v[1], v[2]))
            return {"path": root, "version": v, "python": py}
        out("[警告] エンジンとして使えません（python.exe が無い）: %s" % root)

    engines = find_engines()
    if not engines:
        out("Unreal Engine が見つかりませんでした。")
        p = ask("エンジンのフォルダ（例 D:\\Unreal\\UE_5.7）を入力: ").strip('"')
        if p and engine_python(os.path.normpath(p)):
            root = os.path.normpath(p)
            return {"path": root, "version": engine_version(root),
                    "python": engine_python(root)}
        return None

    for e in engines:
        v = e["version"]
        out("  UE %d.%d.%d   %s" % (v[0], v[1], v[2], e["path"]))

    exact = [e for e in engines if e["version"][:2] == (5, 7)]
    if len(exact) == 1:
        out("")
        out("-> UE 5.7 を使います: %s" % exact[0]["path"])
        return exact[0]
    if len(exact) > 1:
        out("")
        out("UE 5.7 が複数あります。番号を選んでください:")
        for i, e in enumerate(exact, 1):
            out("  %d) %s" % (i, e["path"]))
        a = ask("番号 [1]: ", "1")
        try:
            return exact[int(a or 1) - 1]
        except Exception:
            return exact[0]

    out("")
    out("[警告] UE 5.7 が見つかりません。このツールは 5.7 専用です。")
    out("       最も新しい %s で続行することもできます（動作保証外）。"
        % engines[0]["path"])
    if ask_yes("それでも続行しますか？", False):
        return engines[0]
    return None


# --------------------------------------------------------- Python ライブラリ
def probe_modules(py, extra_path):
    src = ("import json\n"
           "r={}\n"
           "for n in %r:\n"
           "    try:\n"
           "        m=__import__(n)\n"
           "        r[n]=str(getattr(m,'__version__','?'))\n"
           "    except Exception:\n"
           "        r[n]=None\n"
           "print(json.dumps(r))\n" % [p[0] for p in PACKAGES])
    env = os.environ.copy()
    if extra_path:
        env["PYTHONPATH"] = extra_path + os.pathsep + env.get("PYTHONPATH", "")
    try:
        r = subprocess.run([py, "-c", src], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env)
        return json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception as ex:
        out("[警告] ライブラリの確認に失敗しました: %s" % ex)
        return {}


def ensure_pip(py):
    out("ensurepip で pip を用意します...")
    return subprocess.call([py, "-m", "ensurepip", "--default-pip"]) == 0


def pip_install(py, pkgs, target):
    """target=None ならエンジンの site-packages、それ以外は --target で入れる。"""
    cmd = [py, "-m", "pip", "install", "--disable-pip-version-check",
           "--no-warn-script-location"]
    if target:
        cmd += ["--target", target]
    cmd += pkgs
    out("")
    out("実行: " + " ".join(cmd))
    return subprocess.call(cmd)


def install_packages(engine, user_lib_dir):
    head("[2/4] Python ライブラリ（エンジン同梱 Python 3.x 用）")
    py = engine["python"]
    site = os.path.join(os.path.dirname(py), "Lib", "site-packages")

    have = probe_modules(py, user_lib_dir if os.path.isdir(user_lib_dir) else "")
    missing = []
    for mod, pkg, why, required in PACKAGES:
        ver = have.get(mod)
        mark = "OK  " if ver else "無し"
        out("  %s %-14s %-16s %s" % (mark, pkg, ver or "-", why))
        if not ver:
            missing.append((mod, pkg, required))

    if not missing:
        out("")
        out("必要なライブラリはすべて入っています。")
        return True

    # エンジンに入れられるならそちらを優先。失敗しても必ずユーザー領域で再試行する
    # （Program Files 配下のエンジンなど、書き込み権限の出方は環境で変わるため）。
    pkgs = [pkg for _m, pkg, _r in missing]
    targets = ([None] if writable(site) else []) + [user_lib_dir]
    rc = 1
    for i, target in enumerate(targets):
        out("")
        if target is None:
            out("インストール先: エンジンの site-packages")
            out("  %s" % site)
        else:
            out("インストール先: ユーザー領域"
                "（起動スクリプトがこのフォルダを sys.path に追加します）")
            out("  %s" % target)
        rc = pip_install(py, pkgs, target)
        if rc == 0:
            break
        if i == 0:                                     # pip 自体が壊れている場合
            out("")
            out("[警告] pip の実行に失敗しました。")
            ensure_pip(py)
            rc = pip_install(py, pkgs, target)
            if rc == 0:
                break
        if i < len(targets) - 1:
            out("")
            out("[警告] 場所を変えて再試行します。")

    have = probe_modules(py, user_lib_dir if os.path.isdir(user_lib_dir) else "")
    still = [(m, p, req) for m, p, req in missing if not have.get(m)]
    if not still:
        out("")
        out("ライブラリのインストール完了。")
        return True

    out("")
    for mod, pkg, required in still:
        out("[%s] %s を入れられませんでした（pip 終了コード %d）"
            % ("エラー" if required else "警告", pkg, rc))
    if any(req for _m, _p, req in still):
        out("ネットワーク／プロキシを確認して、次を手で実行してください:")
        out('  "%s" -m pip install %s'
            % (py, " ".join(p for _m, p, _r in still)))
        return False
    out("（任意ライブラリなので、この状態でもツールは起動します。"
        "EXR 出力や MP4 出力だけが使えません）")
    return True


# --------------------------------------------------- 起動スクリプト（共通）
BOOTSTRAP_TEMPLATE = u'''# -*- coding: utf-8 -*-
"""%(module)s  --  UE Scene Capture Tool の自動起動（Install.bat が生成）

このファイルは Install.bat が上書きします。手で編集しないでください。
UE は Documents/UnrealEngine/Python を必ず sys.path に加え、同フォルダの
init_unreal.py を全プロジェクトで実行するので、プロジェクト毎の
Startup Scripts 登録なしでメニュー／ツールバーが出る。
"""

import os
import sys

TOOL_DIR = r"%(tool_dir)s"
LIB_DIRS = %(lib_dirs)r

if os.path.isdir(TOOL_DIR) and TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)
for _d in LIB_DIRS:
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.append(_d)          # エンジン同梱の site-packages を優先

try:
    import unreal
except ImportError:                  # UE 外から import された場合は何もしない
    unreal = None


def _register(delta_time=None):
    """メニュー登録。ToolMenus は起動直後だと未準備なので次の tick で行う。"""
    try:
        import capture_menu
        capture_menu.register()
    except Exception:
        import traceback
        traceback.print_exc()
        if unreal is not None:
            unreal.log_error("[SceneCapture] メニュー登録に失敗しました。"
                             "TOOL_DIR=%%s" %% TOOL_DIR)


if unreal is not None:
    if not os.path.isdir(TOOL_DIR):
        unreal.log_warning("[SceneCapture] ツール本体が見つかりません: %%s "
                           "（Install.bat を実行し直してください）" %% TOOL_DIR)
    else:
        _handle = {}

        def _once(delta_time):
            h = _handle.pop("h", None)
            if h is not None:
                try:
                    unreal.unregister_slate_post_tick_callback(h)
                except Exception:
                    pass
            _register()

        try:
            _handle["h"] = unreal.register_slate_post_tick_callback(_once)
        except Exception:            # コマンドレット等 Slate が無い環境
            _register()
'''


def install_bootstrap(tool_dir, user_python_dir, lib_dirs):
    head("[3/4] エディタ起動スクリプト（全プロジェクト共通）")
    try:
        return _install_bootstrap(tool_dir, user_python_dir, lib_dirs)
    except Exception as ex:
        out("[エラー] 起動スクリプトを設置できませんでした: %s" % ex)
        out("        書き込み先: %s" % user_python_dir)
        return False


def _install_bootstrap(tool_dir, user_python_dir, lib_dirs):
    os.makedirs(user_python_dir, exist_ok=True)

    boot_path = os.path.join(user_python_dir, BOOTSTRAP_MODULE + ".py")
    body = BOOTSTRAP_TEMPLATE % {
        "module": BOOTSTRAP_MODULE,
        "tool_dir": tool_dir,
        "lib_dirs": [d for d in lib_dirs if d],
    }
    with io.open(boot_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    out("書き出し: %s" % boot_path)

    init_path = os.path.join(user_python_dir, "init_unreal.py")
    block = (BEGIN_MARK + "\n"
             "try:\n"
             "    import " + BOOTSTRAP_MODULE + "  # noqa: F401\n"
             "except Exception:\n"
             "    import traceback\n"
             "    traceback.print_exc()\n"
             + END_MARK + "\n")

    if os.path.isfile(init_path):
        with io.open(init_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if BEGIN_MARK in text and END_MARK in text:
            pre, rest = text.split(BEGIN_MARK, 1)
            _old, post = rest.split(END_MARK, 1)
            text = pre + block + post.lstrip("\n")
            out("更新: %s（既存の SceneCapture ブロックを差し替え）" % init_path)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n" + block
            out("追記: %s（他ツールの記述はそのまま）" % init_path)
    else:
        text = ("# -*- coding: utf-8 -*-\n"
                "# UE が起動時に自動実行するユーザースクリプト。\n\n" + block)
        out("新規作成: %s" % init_path)

    with io.open(init_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


# ------------------------------------------------------- プロジェクト設定
def find_projects(engine, explicit):
    seen, found = set(), []

    def add(path, note):
        if not path:
            return
        path = os.path.normpath(path)
        key = os.path.normcase(path)
        if key in seen or not os.path.isfile(path):
            return
        seen.add(key)
        found.append((path, note))

    for p in explicit:
        add(p, "指定")

    v = engine["version"]
    ini = os.path.join(os.environ.get("LOCALAPPDATA", ""), "UnrealEngine",
                       "%d.%d" % (v[0], v[1]), "Saved", "Config",
                       "WindowsEditor", "EditorSettings.ini")
    try:
        with io.open(ini, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r'RecentlyOpenedProjectFiles=\(ProjectName="([^"]+)"', line)
                if m:
                    add(m.group(1), "最近開いた")
    except Exception:
        pass

    for pat in (os.path.join(documents_dir(), "Unreal Projects", "*", "*.uproject"),):
        for p in glob.glob(pat):
            add(p, "Unreal Projects")
    return found


def project_plugin_state(uproject):
    """{plugin: True/False/None} を返す。None = 記述なし。"""
    try:
        with io.open(uproject, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return None, None
    state = {}
    entries = data.get("Plugins") or []
    for name, _why in PLUGINS:
        state[name] = None
        for e in entries:
            if str(e.get("Name", "")).lower() == name.lower():
                state[name] = bool(e.get("Enabled", False))
                break
    return data, state


def enable_plugins(uproject):
    data, state = project_plugin_state(uproject)
    if data is None:
        out("  [エラー] .uproject を読めませんでした: %s" % uproject)
        return False
    todo = [n for n, _w in PLUGINS if state.get(n) is not True]
    if not todo:
        out("  すでに設定済み: %s" % uproject)
        return True

    entries = data.setdefault("Plugins", [])
    for name in todo:
        hit = None
        for e in entries:
            if str(e.get("Name", "")).lower() == name.lower():
                hit = e
                break
        if hit is None:
            entries.append({"Name": name, "Enabled": True})
        else:
            hit["Enabled"] = True

    backup = uproject + ".bak"
    made_backup = False
    try:
        if not os.path.exists(backup):
            with io.open(uproject, "rb") as src:
                original = src.read()
            with io.open(backup, "wb") as dst:
                dst.write(original)
            made_backup = True
        with io.open(uproject, "w", encoding="utf-8", newline="\r\n") as f:
            json.dump(data, f, indent="\t", ensure_ascii=False)
            f.write("\n")
    except PermissionError:
        out("  [エラー] 書き込めません（読み取り専用 / SVN ロック）: %s" % uproject)
        return False
    except Exception as ex:
        out("  [エラー] 書き込みに失敗: %s (%s)" % (uproject, ex))
        return False

    out("  有効化: %s  ->  %s" % (", ".join(todo), uproject))
    if made_backup:
        # 全体を書き直すので（エディタの Plugins UI と同じ挙動）差分は広く出る
        out("    元のファイル: %s" % backup)
    return True


def check_stale_startup_script(uproject, tool_dir):
    """旧方式（Startup Scripts 登録）が残っていたら知らせる。

    init_unreal.py は Startup Scripts より先に走るため、両方あるとこちらが
    先に capture_menu を import する = 登録したパスの方が実際に使われる。
    別コピーを指したまま残っていると「更新したのに反映されない」に化ける。"""
    ini = os.path.join(os.path.dirname(uproject), "Config", "DefaultEngine.ini")
    try:
        with io.open(ini, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "StartupScripts" in line and "capture_menu" in line:
                    p = line.split("=", 1)[-1].strip().strip('"')
                    if not os.path.isfile(p):
                        out("  [注意] DefaultEngine.ini に、存在しないパスの "
                            "Startup Script が残っています: %s" % p)
                    elif os.path.normcase(os.path.dirname(os.path.abspath(p))) \
                            == os.path.normcase(tool_dir):
                        out("  [情報] 同じツールの Startup Script 登録が"
                            "残っています（動作は同じ。消して構いません）: %s" % p)
                    else:
                        out("  [注意] 別コピーを指す Startup Script が"
                            "残っています。実際に使われるのは今インストールした "
                            "%s の方です。混乱を避けるため Project Settings > "
                            "Plugins > Python > Startup Scripts から削除して"
                            "ください: %s" % (tool_dir, p))
    except Exception:
        pass


def setup_projects(engine, explicit, tool_dir):
    """"ok" / "skip" / "fail" を返す。"""
    head("[4/4] プロジェクト設定（プラグインの有効化）")
    for name, why in PLUGINS:
        out("  - %-20s %s" % (name, why))
    out("")

    projects = find_projects(engine, explicit)
    if not projects:
        out("プロジェクトが見つかりませんでした。")
        out("あとで Install.bat に .uproject をドラッグ＆ドロップすれば設定できます。")
        return "skip"

    if explicit:
        targets = [p for p, _n in projects if os.path.normcase(p) in
                   {os.path.normcase(os.path.normpath(x)) for x in explicit}]
    else:
        out("設定するプロジェクトを選んでください（プラグインが無効だと"
            "ツールは動きません）:")
        rows = []
        for i, (path, note) in enumerate(projects, 1):
            _d, state = project_plugin_state(path)
            if state is None:
                mark = "読めません"
            elif all(state.get(n) is True for n, _w in PLUGINS):
                mark = "設定済み"
            else:
                mark = "要設定"
            rows.append(path)
            out("  %2d) [%s] %s  (%s)" % (i, mark, path, note))
        out("")
        out("  番号をカンマ区切りで指定 / a=すべて / 空 Enter=スキップ")
        a = ask("選択 [空=スキップ]: ", "").lower()
        if not a:
            out("スキップしました。")
            return "skip"
        if a == "a":
            targets = rows
        else:
            targets = []
            for tok in re.split(r"[,\s]+", a):
                try:
                    targets.append(rows[int(tok) - 1])
                except Exception:
                    pass

    if not targets:
        out("対象なし。")
        return "skip"

    out("")
    ok = True
    for p in targets:
        ok = enable_plugins(p) and ok
        check_stale_startup_script(p, tool_dir)
    return "ok" if ok else "fail"


# ------------------------------------------------------------------- main
def main():
    global AUTO_YES

    argv = sys.argv[:]
    if argv and argv[0] == "-c":                       # Install.bat 経由
        argv = argv[2:]
    else:
        argv = argv[1:]

    engine_args, project_args, unknown = [], [], []
    for a in argv:
        low = a.lower()
        if low in ("-y", "--yes"):
            AUTO_YES = True
        elif low in ("--no-pause",):
            pass                                       # バッチ側で処理済み
        elif low.endswith(".uproject"):
            p = a.strip('"')
            (project_args if os.path.isfile(p) else unknown).append(p)
        elif os.path.isdir(a):
            engine_args.append(a.strip('"'))
        else:
            unknown.append(a)

    tool_root = os.path.dirname(os.path.abspath(
        sys.argv[1] if len(sys.argv) > 1 and sys.argv[0] == "-c" else __file__))
    tool_dir = os.path.join(tool_root, "ue5_capture")

    out("=" * 74)
    out(" %s  インストーラ" % TOOL_TITLE)
    out("=" * 74)
    out("ツール本体: %s" % tool_dir)
    for a in unknown:
        out("[警告] 引数を解釈できません（存在しないパス / 未知のオプション）: %s" % a)

    steps = []                                         # (見出し, 状態, 補足)

    def report(code):
        """工程ごとの結果と終了コードを出して code を返す。"""
        head("結果" if code else "完了")
        for label, status, note in steps:
            out("  %s%s%s" % (pad(label, 22), pad(status, 10), note))
        out("")
        if code:
            out("終了コード: %d  （%s）" % (code, EXIT_REASON.get(code, "?")))
        else:
            out("終了コード: 0  （成功）")
        return code

    if not os.path.isfile(os.path.join(tool_dir, "capture_menu.py")):
        out("")
        out("[エラー] ue5_capture フォルダが Install.bat と同じ場所にありません。")
        out("        リポジトリをまるごと展開してから実行してください。")
        steps.append(("[0] ツール本体", "失敗", tool_dir))
        return report(EXIT_NO_TOOL)
    steps.append(("[0] ツール本体", "OK", tool_dir))

    engine = pick_engine(engine_args)
    if engine is None:
        out("")
        out("[中止] 使用するエンジンが決まりませんでした。")
        steps.append(("[1] エンジン検出", "失敗", ""))
        return report(EXIT_NO_ENGINE)
    steps.append(("[1] エンジン検出", "OK", engine["path"]))

    user_python_dir = os.path.join(documents_dir(), "UnrealEngine", "Python")
    user_lib_dir = os.path.join(user_python_dir, "ue5_capture_libs")

    # 途中で失敗しても残りは続行し、最初に失敗した工程のコードを返す
    failed = 0

    deps_ok = install_packages(engine, user_lib_dir)
    steps.append(("[2] ライブラリ", "OK" if deps_ok else "失敗", ""))
    if not deps_ok:
        failed = failed or EXIT_DEPS

    boot_ok = install_bootstrap(tool_dir, user_python_dir,
                                [user_lib_dir] if os.path.isdir(user_lib_dir) else [])
    steps.append(("[3] 起動スクリプト", "OK" if boot_ok else "失敗", user_python_dir))
    if not boot_ok:
        failed = failed or EXIT_BOOTSTRAP

    proj = setup_projects(engine, project_args, tool_dir)
    steps.append(("[4] プロジェクト設定",
                  {"ok": "OK", "skip": "スキップ"}.get(proj, "失敗"), ""))
    if proj == "fail":
        failed = failed or EXIT_PROJECT

    head("完了" if not failed else "完了（一部失敗）")
    out("次の手順:")
    out("  1. Unreal Editor を（開いていれば）再起動する")
    out("  2. メニューバーの [SceneCapture] か、ツールバー右端の"
        " [SceneCapture] ボタンから開く")
    out("")
    out("メモ:")
    out("  - ツールは %s を直接読みます。" % tool_dir)
    out("    git pull で更新すれば、そのまま最新版が使われます。")
    out("  - プラグインを有効化したプロジェクトは、初回起動時に"
        "「モジュールのビルド」を求められることがあります。")
    out("  - アンインストールは %s\\init_unreal.py の SceneCapture ブロックと"
        % user_python_dir)
    out("    %s.py を消すだけです。" % BOOTSTRAP_MODULE)
    return report(failed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        out("")
        out("中断しました。（終了コード %d）" % EXIT_ABORTED)
        sys.exit(EXIT_ABORTED)
    except Exception:
        import traceback
        traceback.print_exc()
        out("")
        out("[エラー] 想定外のエラーで中断しました。（終了コード %d）"
            % EXIT_UNEXPECTED)
        sys.exit(EXIT_UNEXPECTED)
