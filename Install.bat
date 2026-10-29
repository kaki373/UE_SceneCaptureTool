@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  UE Scene Capture Tool - installer (single self-contained file)
rem  Supported engines: Unreal Engine 5.7 / 5.8
rem
rem  Usage:
rem    Install.bat                          interactive
rem    Install.bat "<dir>\Foo.uproject" ... configure these projects (drag and drop
rem                                         works); the engine matching the
rem                                         project's EngineAssociation is used
rem    Install.bat "<engine root>"          force an engine, e.g.
rem                                         "C:\Program Files\Epic Games\UE_5.8"
rem    Install.bat --yes [--no-pause]       unattended
rem    Install.bat --dry-run                show what would be done, write nothing
rem    Install.bat --no-libs                skip the pip step
rem    Install.bat --uninstall              remove the editor start-up hook
rem    Install.bat --help
rem
rem  Environment overrides:
rem    UE_SCENECAPTURE_PYTHON    interpreter used to run the payload
rem    UE_SCENECAPTURE_USER_DIR  replaces <Documents>\UnrealEngine\Python
rem                              (for tests: keeps the real start-up hook intact)
rem
rem  This batch only locates a Python interpreter; everything else lives in the
rem  Python payload below the marker line at the bottom of this file.
rem  Keep this file CRLF (see .gitattributes): cmd mis-resolves call labels in
rem  LF-only batch files.
rem
rem  Exit codes (which step failed):
rem     0  success
rem    10  tool body (ue5_capture) not found next to this file
rem    20  Unreal Engine could not be located
rem    30  required Python libraries could not be installed
rem    40  editor start-up script could not be written / removed
rem    50  a selected .uproject could not be updated
rem    90  unexpected error
rem   130  aborted (Ctrl-C)
rem ===========================================================================
title UE Scene Capture Tool Installer

rem shift also moves %0, so save our own path first
set "SELF=%~f0"
set "HERE=%~dp0"
set "VER=unknown"
if exist "%HERE%VERSION" set /p VER=<"%HERE%VERSION"
set "PY="
set "PYARG="

rem --- scan args for --no-pause / --help (shift does not consume %*) ---------
set "NOPAUSE="
:scan_args
if "%~1"=="" goto scan_done
if /i "%~1"=="--no-pause" set "NOPAUSE=1"
if /i "%~1"=="--help" goto usage
if /i "%~1"=="-h" goto usage
if "%~1"=="/?" goto usage
shift
goto scan_args
:scan_done

rem --- 1) explicit override -------------------------------------------------
if defined UE_SCENECAPTURE_PYTHON if exist "%UE_SCENECAPTURE_PYTHON%" set "PY=%UE_SCENECAPTURE_PYTHON%"

rem --- 2) registered Unreal Engine installs (supported first, then any) ------
rem     This only picks the interpreter that runs the payload; the payload
rem     chooses the target engine itself.
for %%V in (5.8 5.7) do (
  if not defined PY (
    for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\EpicGames\Unreal Engine\%%V" /v InstalledDirectory 2^>nul ^| findstr /i "InstalledDirectory"') do call :try_engine "%%B"
  )
)
if not defined PY (
  for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\EpicGames\Unreal Engine" /s /v InstalledDirectory 2^>nul ^| findstr /i "InstalledDirectory"') do call :try_engine "%%B"
)
if not defined PY (
  for /f "tokens=2*" %%A in ('reg query "HKCU\SOFTWARE\EpicGames\Unreal Engine" /s /v InstalledDirectory 2^>nul ^| findstr /i "InstalledDirectory"') do call :try_engine "%%B"
)

rem --- 3) common install locations -----------------------------------------
if not defined PY (
  for %%V in (5.8 5.7) do (
    for %%D in (C D E F G) do (
      call :try_engine "%%D:\Program Files\Epic Games\UE_%%V"
      call :try_engine "%%D:\Epic Games\UE_%%V"
      call :try_engine "%%D:\Unreal\UE_%%V"
      call :try_engine "%%D:\UE_%%V"
    )
  )
)

rem --- 4) any working Python 3 on PATH (the payload finds the engine itself) -
rem     "where python" can return the Windows Store stub, which opens the Store
rem     instead of running, so every candidate is smoke-tested before use.
if not defined PY (
  for /f "delims=" %%P in ('where python 2^>nul') do call :try_python "%%P"
)
if not defined PY (
  py -3 -c "import sys" >nul 2>&1 && (set "PY=py" & set "PYARG=-3")
)

if not defined PY (
  echo.
  echo [ERROR] Could not find a Python interpreter.
  echo         Unreal Engine 5.7 / 5.8 does not look installed in a standard location.
  echo         Re-run with the engine root, e.g.:
  echo             Install.bat "C:\Program Files\Epic Games\UE_5.8"
  echo.
  if not defined NOPAUSE pause
  exit /b 20
)

"%PY%" %PYARG% -c "import io,sys;p=sys.argv[1];t=io.open(p,encoding='utf-8').read();h,_,b=t.partition('#==='+'PYTHON===');exec(compile('\n'*h.count('\n')+b,p,'exec'))" "%SELF%" %*
set "RC=%ERRORLEVEL%"

if not defined NOPAUSE pause
exit /b %RC%

:usage
echo UE Scene Capture Tool installer  v%VER%   (Unreal Engine 5.7 / 5.8)
echo.
echo   Install.bat                         interactive
echo   Install.bat "path\Foo.uproject"     enable the plugins in this project
echo                                       (drag and drop a .uproject onto Install.bat)
echo   Install.bat "engine root"           force an engine, e.g. "D:\UE_5.8"
echo   Install.bat --yes [--no-pause]      unattended
echo   Install.bat --dry-run               show what would be done, write nothing
echo   Install.bat --no-libs               skip installing numpy / Pillow / imageio
echo   Install.bat --uninstall             remove the editor start-up hook
echo.
echo Exit codes: 0 ok, 10 tool missing, 20 engine, 30 libraries,
echo             40 start-up script, 50 .uproject, 90 unexpected, 130 aborted
exit /b 0

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
  1. Unreal Engine 5.7 / 5.8 を検出する（.uproject 指定時はその EngineAssociation に合わせる）
  2. 同梱 Python に numpy / Pillow / imageio / imageio-ffmpeg を入れる
     （起動フックは全エンジン共通なので、見つかった他の対応エンジンにも入れる）
  3. Documents/UnrealEngine/Python に起動スクリプトを置く（全プロジェクト共通）
  4. 選んだ .uproject で必要プラグインを有効化する

ツール本体はコピーせず、この Install.bat の隣の ue5_capture を絶対パスで参照する。
フォルダを移動・削除したら Install.bat を実行し直す（外すときは --uninstall）。
"""

import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys

try:
    import winreg
except ImportError:                                   # 非 Windows は対象外
    winreg = None

# --------------------------------------------------------------------- 定数
TOOL_TITLE = "UE Scene Capture Tool"
BOOTSTRAP_MODULE = "ue5_capture_bootstrap"
LIB_DIR_NAME = "ue5_capture_libs"
BEGIN_MARK = "# >>> UE Scene Capture Tool (Install.bat) >>>"
END_MARK = "# <<< UE Scene Capture Tool (Install.bat) <<<"
INIT_HEADER = ("# -*- coding: utf-8 -*-\n"
               "# UE が起動時に自動実行するユーザースクリプト。\n\n")

SUPPORTED = ((5, 7), (5, 8))
SUPPORTED_LABEL = " / ".join("%d.%d" % v for v in SUPPORTED)

# import 名 -> pip 指定 / 用途 / 必須か。requirements.txt と揃えておくこと。
PACKAGES = [
    ("numpy",          "numpy>=1.24",           "AA縮小・Depth正規化・マスク合成", True),
    ("PIL",            "Pillow>=10.0",          "PNG 入出力・合成",                True),
    ("imageio",        "imageio>=2.31",         "Z-Depth の EXR 出力（任意）",     False),
    ("imageio_ffmpeg", "imageio-ffmpeg>=0.4.9", "MP4 エンコード用 ffmpeg（任意）", False),
]

# .uproject で有効化するプラグイン（どちらも EnabledByDefault=false）。
# SequencerScripting / LevelSequenceEditor は MovieRenderPipeline の依存として
# 自動で有効になるので、.uproject への追記は行わない（差分を最小にする）。
PLUGINS = [
    ("PythonScriptPlugin", "エディタ内 Python（ツール本体の実行に必須）"),
    ("MovieRenderPipeline", "Movie Render Queue（レンダと Sequencer API に必須）"),
]

AUTO_YES = False
DRY_RUN = False

# 終了コード（どの工程で失敗したかを表す。バッチはこの値をそのまま返す）
EXIT_OK = 0
EXIT_NO_TOOL = 10          # ツール本体（ue5_capture）が Install.bat の隣に無い
EXIT_NO_ENGINE = 20        # 対応する UE を特定できなかった
EXIT_DEPS = 30             # 必須 Python ライブラリを入れられなかった
EXIT_BOOTSTRAP = 40        # 起動スクリプトを設置 / 削除できなかった
EXIT_PROJECT = 50          # 選んだプロジェクトの .uproject を更新できなかった
EXIT_UNEXPECTED = 90       # 想定外の例外
EXIT_ABORTED = 130         # Ctrl-C

EXIT_REASON = {
    EXIT_NO_TOOL: "ツール本体 (ue5_capture) が見つからない",
    EXIT_NO_ENGINE: "Unreal Engine を特定できない",
    EXIT_DEPS: "必須 Python ライブラリの導入に失敗",
    EXIT_BOOTSTRAP: "起動スクリプトの設置 / 削除に失敗",
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


def read_version(tool_root):
    try:
        with io.open(os.path.join(tool_root, "VERSION"), encoding="utf-8-sig") as f:
            return f.read().strip() or "unknown"
    except Exception:
        return "unknown"


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


def user_python_dir():
    """UE が全プロジェクトで sys.path に加える <Documents>/UnrealEngine/Python。

    UE_SCENECAPTURE_USER_DIR で差し替え可（テストで本物の起動フックを汚さないため）。"""
    override = os.environ.get("UE_SCENECAPTURE_USER_DIR", "").strip().strip('"')
    if override:
        return os.path.abspath(override)
    return os.path.join(documents_dir(), "UnrealEngine", "Python")


def writable(path):
    if DRY_RUN:                                        # 試し書きもしない
        return os.access(path, os.W_OK)
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


def vstr(v):
    return "%d.%d.%d" % tuple(v)


def is_supported(e):
    return tuple(e["version"][:2]) in SUPPORTED


def _reg_engine_roots():
    """[(root, guid)]。ランチャー版は HKLM\\SOFTWARE\\EpicGames\\Unreal Engine\\<ver>、
    ソースビルドは HKCU\\Software\\Epic Games\\Unreal Engine\\Builds（値名 GUID = パス）。"""
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
                                roots.append((winreg.QueryValueEx(
                                    sub, "InstalledDirectory")[0], ""))
                        except Exception:
                            pass
            except Exception:
                pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Epic Games\Unreal Engine\Builds") as k:
            i = 0
            while True:
                try:
                    guid, path, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                roots.append((path, guid))
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
    return [(e.get("InstallLocation", ""), "")
            for e in data.get("InstallationList", [])
            if str(e.get("AppName", "")).startswith("UE_")]


def _common_engine_roots():
    # ネットワークドライブは isdir で待たされることがあるのでローカルの定番だけ見る
    roots = []
    parents = []
    for d in "CDEFGH":
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
                    roots.append((os.path.join(parent, name), ""))
        except Exception:
            pass
    return roots


def find_engines():
    """{path, version, python, guid} のリスト（新しい順）を返す。"""
    by_key, found = {}, []
    for root, guid in (_reg_engine_roots() + _launcher_engine_roots()
                       + _common_engine_roots()):
        if not root:
            continue
        root = os.path.normpath(root)
        key = os.path.normcase(root)
        if key in by_key:
            if guid and not by_key[key]["guid"]:
                by_key[key]["guid"] = guid
            continue
        py = engine_python(root)
        if not py:
            continue
        e = {"path": root, "version": engine_version(root), "python": py,
             "guid": guid}
        by_key[key] = e
        found.append(e)
    found.sort(key=lambda e: e["version"], reverse=True)
    return found


def project_association(uproject):
    try:
        with io.open(uproject, encoding="utf-8-sig") as f:
            return str(json.load(f).get("EngineAssociation", "")).strip()
    except Exception:
        return ""


def engine_matches(e, assoc):
    if re.match(r"^\d+\.\d+$", assoc):
        return "%d.%d" % tuple(e["version"][:2]) == assoc
    return bool(e["guid"]) and e["guid"].strip("{}").lower() == assoc.strip("{}").lower()


def choose_one(cands, what):
    if len(cands) == 1:
        return cands[0]
    out("")
    out("%s が複数あります。番号を選んでください:" % what)
    for i, e in enumerate(cands, 1):
        out("  %d) %s  (UE %s)" % (i, e["path"], vstr(e["version"])))
    a = ask("番号 [1]: ", "1")
    try:
        return cands[int(a or 1) - 1]
    except Exception:
        return cands[0]


def pick_engine(forced_roots, project_args, engines=None):
    head("[1/4] Unreal Engine を探す（対応: %s）" % SUPPORTED_LABEL)

    for root in forced_roots:                          # 引数で指定されたもの優先
        root = os.path.normpath(root)
        py = engine_python(root)
        if py:
            e = {"path": root, "version": engine_version(root), "python": py,
                 "guid": ""}
            out("指定されたエンジンを使います: %s  (UE %s)" % (root, vstr(e["version"])))
            if not is_supported(e):
                out("[警告] 対応外のバージョンです（動作保証外）。")
            return e
        out("[警告] エンジンとして使えません（python.exe が無い）: %s" % root)

    if engines is None:
        engines = find_engines()
    if not engines:
        out("Unreal Engine が見つかりませんでした。")
        p = ask("エンジンのフォルダ（例 C:\\Program Files\\Epic Games\\UE_5.8）を入力: ")
        p = p.strip('"')
        if p and engine_python(os.path.normpath(p)):
            root = os.path.normpath(p)
            return {"path": root, "version": engine_version(root),
                    "python": engine_python(root), "guid": ""}
        return None

    for e in engines:
        out("  UE %-8s %s  %s" % (vstr(e["version"]),
                                  pad("対応" if is_supported(e) else "対象外", 6),
                                  e["path"]))

    # .uproject が指定されていれば、そのプロジェクトのエンジンを使う
    for p in project_args:
        assoc = project_association(p)
        if not assoc:
            continue
        hits = [e for e in engines if engine_matches(e, assoc)]
        if hits:
            e = choose_one(hits, "プロジェクトのエンジン (%s)" % assoc)
            out("")
            out("-> %s の EngineAssociation=%s に合わせて UE %s を使います: %s"
                % (os.path.basename(p), assoc, vstr(e["version"]), e["path"]))
            if not is_supported(e):
                out("[警告] このプロジェクトのエンジンは対応外です（動作保証外）。")
                if not ask_yes("それでも続行しますか？", False):
                    return None
            return e
        out("[警告] %s のエンジン (EngineAssociation=%s) が見つかりません。"
            "対応エンジンの最新版で続行します。" % (os.path.basename(p), assoc))
        break

    supported = [e for e in engines if is_supported(e)]
    if supported:
        newest = supported[0]["version"][:2]
        e = choose_one([x for x in supported if x["version"][:2] == newest],
                       "UE %d.%d" % tuple(newest))
        out("")
        out("-> UE %s を使います: %s" % (vstr(e["version"]), e["path"]))
        return e

    out("")
    out("[警告] 対応エンジン（%s）が見つかりません。" % SUPPORTED_LABEL)
    out("       最も新しい %s で続行することもできます（動作保証外）。"
        % engines[0]["path"])
    if ask_yes("それでも続行しますか？", False):
        return engines[0]
    return None


# --------------------------------------------------------- Python ライブラリ
def probe_modules(py, lib_root):
    """{import名: version|None, "__libdir__": この Python 用のユーザー領域} を返す。

    起動スクリプトと同じ規則（lib_root/py<major><minor>、旧形式の lib_root 直下）で
    ユーザー領域も見る。"""
    src = ("import json,os,sys\n"
           "root=%r\n"
           "d=os.path.join(root,'py%%d%%d'%%sys.version_info[:2])\n"
           "for p in (d,root):\n"
           "    if os.path.isdir(p): sys.path.append(p)\n"
           "r={'__libdir__':d}\n"
           "for n in %r:\n"
           "    try:\n"
           "        m=__import__(n)\n"
           "        r[n]=str(getattr(m,'__version__','?'))\n"
           "    except Exception:\n"
           "        r[n]=None\n"
           "print(json.dumps(r))\n" % (lib_root, [p[0] for p in PACKAGES]))
    try:
        r = subprocess.run([py, "-c", src], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        return json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception as ex:
        out("[警告] ライブラリの確認に失敗しました: %s" % ex)
        return {}


def ensure_pip(py):
    out("ensurepip で pip を用意します...")
    if DRY_RUN:
        return True
    return subprocess.call([py, "-m", "ensurepip", "--default-pip"]) == 0


def pip_install(py, pkgs, target, wheels):
    """target=None ならエンジンの site-packages、それ以外は --target で入れる。

    wheels（Install.bat の隣の wheels フォルダ）があれば先にオフラインで試す。
    無ければ PyPI に接続する＝ネットワーク必須。"""
    cmd = [py, "-m", "pip", "install", "--disable-pip-version-check",
           "--no-warn-script-location"]
    if target:
        cmd += ["--target", target, "--upgrade"]
    tries = []
    if wheels:
        tries.append(("オフライン wheels", ["--no-index", "--find-links", wheels]))
    tries.append(("オンライン PyPI", ["--find-links", wheels] if wheels else []))
    rc = 1
    for label, extra in tries:
        full = cmd + extra + pkgs
        out("")
        out("実行[%s]: %s" % (label, subprocess.list2cmdline(full)))
        if DRY_RUN:
            out("  (dry-run: 実行しません)")
            return 0
        rc = subprocess.call(full)
        if rc == 0:
            return 0
    return rc


def install_packages(engine, lib_root, wheels, primary=True):
    """戻り値: True=必須が揃った / False=必須が欠けた。"""
    head("[2/4] Python ライブラリ（UE %s 同梱 Python）%s"
         % (vstr(engine["version"]), "" if primary else "  ※追加エンジン"))
    py = engine["python"]
    site = os.path.join(os.path.dirname(py), "Lib", "site-packages")
    out("  %s" % py)

    have = probe_modules(py, lib_root)
    user_lib = have.get("__libdir__") or os.path.join(lib_root, "py311")
    missing = []
    for mod, pkg, why, required in PACKAGES:
        ver = have.get(mod)
        mark = "OK  " if ver else "無し"
        out("  %s %-22s %-10s %s" % (mark, pkg, ver or "-", why))
        if not ver:
            missing.append((mod, pkg, required))

    if not missing:
        out("")
        out("必要なライブラリはすべて入っています。")
        return True

    # エンジンに入れられるならそちらを優先。失敗しても必ずユーザー領域で再試行する
    # （Program Files 配下のエンジンなど、書き込み権限の出方は環境で変わるため）。
    pkgs = [pkg for _m, pkg, _r in missing]
    targets = ([None] if writable(site) else []) + [user_lib]
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
        rc = pip_install(py, pkgs, target, wheels)
        if rc == 0:
            break
        if i == 0:                                     # pip 自体が壊れている場合
            out("")
            out("[警告] pip の実行に失敗しました。")
            ensure_pip(py)
            rc = pip_install(py, pkgs, target, wheels)
            if rc == 0:
                break
        if i < len(targets) - 1:
            out("")
            out("[警告] 場所を変えて再試行します。")

    if DRY_RUN:
        return True

    have = probe_modules(py, lib_root)
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
            % (py, " ".join('"%s"' % p for _m, p, _r in still)))
        return False
    out("（任意ライブラリなので、この状態でもツールは起動します。"
        "EXR 出力や MP4 出力だけが使えません）")
    return True


# --------------------------------------------------- 起動スクリプト（共通）
BOOTSTRAP_TEMPLATE = u'''# -*- coding: utf-8 -*-
"""%(module)s  --  UE Scene Capture Tool の自動起動（Install.bat が生成）

このファイルは Install.bat が上書きします。手で編集しないでください。
UE は Documents/UnrealEngine/Python を必ず sys.path に加え、同フォルダの
init_unreal.py を全プロジェクト・全エンジンで実行するので、プロジェクト毎の
Startup Scripts 登録なしでメニュー／ツールバーが出る。
ツール本体は下の TOOL_DIR を直接読む。フォルダを移動したら移動先の
Install.bat を実行し直すこと（外すときは Install.bat --uninstall）。
"""

import os
import sys

TOOL_DIR = r"%(tool_dir)s"

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB_ROOT = os.path.join(_HERE, "%(lib_dir_name)s")
# --target で入れた拡張モジュールは Python の版に縛られるので版ごとのフォルダ。
# 直下は旧 Install.bat が入れた場所（3.11 用）。
for _d in (os.path.join(_LIB_ROOT, "py%%d%%d" %% sys.version_info[:2]), _LIB_ROOT):
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.append(_d)          # エンジン同梱の site-packages を優先

if os.path.isdir(TOOL_DIR) and TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

try:
    import unreal
except ImportError:                  # UE 外から import された場合は何もしない
    unreal = None


def _version():
    try:
        with open(os.path.join(os.path.dirname(TOOL_DIR), "VERSION")) as f:
            return f.read().strip()
    except Exception:
        return "?"


def _register(delta_time=None):
    """メニュー登録。ToolMenus は起動直後だと未準備なので次の tick で行う。"""
    try:
        import capture_menu
        capture_menu.register()
        if unreal is not None:
            unreal.log("[SceneCapture] v%%s  %%s" %% (_version(), TOOL_DIR))
    except Exception:
        import traceback
        traceback.print_exc()
        if unreal is not None:
            unreal.log_error("[SceneCapture] メニュー登録に失敗しました。"
                             "TOOL_DIR=%%s" %% TOOL_DIR)


if unreal is not None:
    if not os.path.isdir(TOOL_DIR):
        unreal.log_warning("[SceneCapture] ツール本体が見つかりません: %%s "
                           "（移動した場合は移動先の Install.bat を実行し直してください）"
                           %% TOOL_DIR)
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

INIT_BLOCK = (BEGIN_MARK + "\n"
              "try:\n"
              "    import " + BOOTSTRAP_MODULE + "  # noqa: F401\n"
              "except Exception:\n"
              "    import traceback\n"
              "    traceback.print_exc()\n"
              + END_MARK + "\n")


def _write(path, text, verb):
    out("%s: %s%s" % (verb, path, "  (dry-run: 書き込みません)" if DRY_RUN else ""))
    if DRY_RUN:
        return
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _remove(path):
    out("削除: %s%s" % (path, "  (dry-run)" if DRY_RUN else ""))
    if DRY_RUN:
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def install_bootstrap(tool_dir, user_dir):
    head("[3/4] エディタ起動スクリプト（全プロジェクト・全エンジン共通）")
    try:
        return _install_bootstrap(tool_dir, user_dir)
    except Exception as ex:
        out("[エラー] 起動スクリプトを設置できませんでした: %s" % ex)
        out("        書き込み先: %s" % user_dir)
        return False


def _install_bootstrap(tool_dir, user_dir):
    if not DRY_RUN:
        os.makedirs(user_dir, exist_ok=True)

    boot_path = os.path.join(user_dir, BOOTSTRAP_MODULE + ".py")
    body = BOOTSTRAP_TEMPLATE % {
        "module": BOOTSTRAP_MODULE,
        "tool_dir": tool_dir,
        "lib_dir_name": LIB_DIR_NAME,
    }
    _write(boot_path, body, "書き出し")

    init_path = os.path.join(user_dir, "init_unreal.py")
    if os.path.isfile(init_path):
        with io.open(init_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if BEGIN_MARK in text and END_MARK in text:
            pre, rest = text.split(BEGIN_MARK, 1)
            _old, post = rest.split(END_MARK, 1)
            text = pre + INIT_BLOCK + post.lstrip("\n")
            verb = "更新（既存の SceneCapture ブロックを差し替え）"
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n" + INIT_BLOCK
            verb = "追記（他ツールの記述はそのまま）"
    else:
        text = INIT_HEADER + INIT_BLOCK
        verb = "新規作成"
    _write(init_path, text, verb)
    return True


def uninstall(user_dir):
    """起動フックを外す。.uproject のプラグイン設定とエンジン側ライブラリは残す。"""
    head("アンインストール（起動スクリプトの削除）")
    ok = True
    init_path = os.path.join(user_dir, "init_unreal.py")
    try:
        if os.path.isfile(init_path):
            with io.open(init_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if BEGIN_MARK in text and END_MARK in text:
                pre, rest = text.split(BEGIN_MARK, 1)
                _old, post = rest.split(END_MARK, 1)
                pre = pre.rstrip("\n")
                text = (pre + "\n" if pre else "") + post.lstrip("\n")
                if text.strip() in ("", INIT_HEADER.strip()):
                    _remove(init_path)                 # 自分で作った中身だけ
                else:
                    _write(init_path, text, "ブロックを削除（他ツールの記述は残す）")
            else:
                out("init_unreal.py に SceneCapture ブロックはありません: %s" % init_path)
        else:
            out("init_unreal.py はありません: %s" % init_path)

        doomed = [os.path.join(user_dir, BOOTSTRAP_MODULE + ".py")]
        doomed += glob.glob(os.path.join(user_dir, "__pycache__",
                                         BOOTSTRAP_MODULE + ".*.pyc"))
        for p in doomed:
            if os.path.isfile(p):
                _remove(p)

        lib_root = os.path.join(user_dir, LIB_DIR_NAME)
        if os.path.isdir(lib_root) and ask_yes(
                "ユーザー領域のライブラリ %s も削除しますか？" % lib_root, True):
            _remove(lib_root)
    except Exception as ex:
        out("[エラー] 削除に失敗しました: %s" % ex)
        ok = False

    out("")
    out("残すもの:")
    out("  - .uproject のプラグイン設定（元のファイルは <名前>.uproject.bak）")
    out("  - エンジン同梱 Python の numpy / Pillow / imageio（他ツールと共用のため）")
    out("  - ツール本体のフォルダ（不要なら手で削除）")
    out("")
    out("終了コード: %d" % (EXIT_OK if ok else EXIT_BOOTSTRAP))
    return EXIT_OK if ok else EXIT_BOOTSTRAP


# ------------------------------------------------------- プロジェクト設定
def find_projects(engines, explicit):
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

    for v in sorted(SUPPORTED, reverse=True):         # 対応版の「最近使った」履歴
        ini = os.path.join(os.environ.get("LOCALAPPDATA", ""), "UnrealEngine",
                           "%d.%d" % v, "Saved", "Config",
                           "WindowsEditor", "EditorSettings.ini")
        try:
            with io.open(ini, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.search(r'RecentlyOpenedProjectFiles=\(ProjectName="([^"]+)"',
                                  line)
                    if m:
                        add(m.group(1), "UE %d.%d の履歴" % v)
        except Exception:
            pass

    for p in glob.glob(os.path.join(documents_dir(), "Unreal Projects", "*",
                                    "*.uproject")):
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
    if DRY_RUN:
        out("  有効化予定: %s  ->  %s  (dry-run: 書き込みません)"
            % (", ".join(todo), uproject))
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
        out("  [エラー] 書き込めません（読み取り専用 / バージョン管理のロック）: %s"
            % uproject)
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


def check_project_engine(uproject, ready_versions):
    """プロジェクトのエンジンにライブラリが入っていなければ知らせる。"""
    assoc = project_association(uproject)
    if ready_versions is None or not re.match(r"^\d+\.\d+$", assoc):
        return
    v = tuple(int(x) for x in assoc.split("."))
    if v not in SUPPORTED:
        out("  [注意] このプロジェクトは UE %s 用です（対応は %s）。"
            % (assoc, SUPPORTED_LABEL))
    elif v not in ready_versions:
        out("  [注意] このプロジェクトは UE %s 用ですが、そのエンジンにはライブラリを"
            "入れていません。Install.bat にこの .uproject をドロップして"
            "実行し直してください。" % assoc)


def setup_projects(engines, explicit, tool_dir, ready_versions):
    """"ok" / "skip" / "fail" を返す。"""
    head("[4/4] プロジェクト設定（プラグインの有効化）")
    for name, why in PLUGINS:
        out("  - %-20s %s" % (name, why))
    out("")

    projects = find_projects(engines, explicit)
    if not projects:
        out("プロジェクトが見つかりませんでした。")
        out("あとで Install.bat に .uproject をドラッグ＆ドロップすれば設定できます。")
        return "skip"

    if explicit:
        want = {os.path.normcase(os.path.normpath(x)) for x in explicit}
        targets = [p for p, _n in projects if os.path.normcase(p) in want]
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
            assoc = project_association(path) or "?"
            out("  %2d) [%s] %s  (%s / EngineAssociation=%s)" % (i, mark, path, note, assoc))
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
        check_project_engine(p, ready_versions)
    return "ok" if ok else "fail"


# ------------------------------------------------------------------- main
def parse_args(argv):
    opts = {"yes": False, "dry": False, "uninstall": False, "no_libs": False}
    engine_args, project_args, unknown = [], [], []
    for a in argv:
        # 末尾が \ のパスを "..." で渡すと \" が引用符のエスケープになり
        # 末尾に " が残る（"D:\UE_5.8\" -> D:\UE_5.8"）。判定の前に剥がす。
        a = a.strip().strip('"')
        low = a.lower()
        if low in ("-y", "--yes"):
            opts["yes"] = True
        elif low == "--dry-run":
            opts["dry"] = True
        elif low == "--uninstall":
            opts["uninstall"] = True
        elif low == "--no-libs":
            opts["no_libs"] = True
        elif low == "--no-pause":
            pass                                       # バッチ側で処理済み
        elif low.endswith(".uproject"):
            (project_args if os.path.isfile(a) else unknown).append(a)
        elif a and os.path.isdir(a):
            engine_args.append(a)
        elif a:
            unknown.append(a)
    return opts, engine_args, project_args, unknown


def main():
    global AUTO_YES, DRY_RUN

    argv = sys.argv[:]
    if argv and argv[0] == "-c":                       # Install.bat 経由
        self_path = argv[1]
        argv = argv[2:]
    else:
        self_path = os.path.abspath(__file__)
        argv = argv[1:]

    opts, engine_args, project_args, unknown = parse_args(argv)
    AUTO_YES = opts["yes"]
    DRY_RUN = opts["dry"]

    tool_root = os.path.dirname(os.path.abspath(self_path))
    tool_dir = os.path.join(tool_root, "ue5_capture")
    version = read_version(tool_root)
    user_dir = user_python_dir()
    lib_root = os.path.join(user_dir, LIB_DIR_NAME)
    wheels = os.path.join(tool_root, "wheels")
    wheels = wheels if os.path.isdir(wheels) else ""

    out("=" * 74)
    out(" %s  v%s  インストーラ（対応: UE %s）%s"
        % (TOOL_TITLE, version, SUPPORTED_LABEL, "  [dry-run]" if DRY_RUN else ""))
    out("=" * 74)
    out("ツール本体: %s" % tool_dir)
    out("起動スクリプトの置き場: %s" % user_dir)
    for a in unknown:
        out("[警告] 引数を解釈できません（存在しないパス / 未知のオプション）: %s" % a)

    if opts["uninstall"]:
        return uninstall(user_dir)

    steps = []                                         # (見出し, 状態, 補足)

    def report(code):
        """工程ごとの結果と終了コードを出して code を返す。"""
        head("結果")
        for label, status, note in steps:
            out("  %s%s%s" % (pad(label, 22), pad(status, 10), note))
        out("")
        if code:
            out("終了コード: %d  （%s）" % (code, EXIT_REASON.get(code, "?")))
        else:
            out("終了コード: 0  （成功）%s" % ("  [dry-run: 何も書き込んでいません]"
                                               if DRY_RUN else ""))
        return code

    if not os.path.isfile(os.path.join(tool_dir, "capture_menu.py")):
        out("")
        out("[エラー] ue5_capture フォルダが Install.bat と同じ場所にありません。")
        out("        配布 zip をまるごと展開してから実行してください。")
        steps.append(("[0] ツール本体", "失敗", tool_dir))
        return report(EXIT_NO_TOOL)
    steps.append(("[0] ツール本体", "OK", "v%s  %s" % (version, tool_dir)))

    engines = find_engines()
    engine = pick_engine(engine_args, project_args, engines)
    if engine is None:
        out("")
        out("[中止] 使用するエンジンが決まりませんでした。")
        steps.append(("[1] エンジン検出", "失敗", ""))
        return report(EXIT_NO_ENGINE)
    steps.append(("[1] エンジン検出", "OK",
                  "UE %s  %s" % (vstr(engine["version"]), engine["path"])))

    # 途中で失敗しても残りは続行し、最初に失敗した工程のコードを返す
    failed = 0

    ready_versions = None                              # None = 確認していない
    if opts["no_libs"]:
        head("[2/4] Python ライブラリ")
        out("--no-libs: スキップしました。")
        steps.append(("[2] ライブラリ", "スキップ", "--no-libs"))
    else:
        if wheels:
            out("")
            out("同梱 wheels を使います: %s" % wheels)
        deps_ok = install_packages(engine, lib_root, wheels)
        ready_versions = {tuple(engine["version"][:2])} if deps_ok else set()
        steps.append(("[2] ライブラリ", "OK" if deps_ok else "失敗",
                      "UE %s" % vstr(engine["version"])))
        if not deps_ok:
            failed = failed or EXIT_DEPS
        # 起動フックは全エンジン共通なので、他の対応エンジンにもライブラリが要る
        others = [e for e in engines if is_supported(e)
                  and os.path.normcase(e["python"]) != os.path.normcase(engine["python"])]
        if others:
            out("")
            out("他の対応エンジンでもツールが起動します（起動スクリプトは全エンジン共通）:")
            for e in others:
                out("  UE %s  %s" % (vstr(e["version"]), e["path"]))
            if ask_yes("これらにもライブラリを入れますか？", True):
                for e in others:
                    ok2 = install_packages(e, lib_root, wheels, primary=False)
                    if ok2:
                        ready_versions.add(tuple(e["version"][:2]))
                    steps.append(("    追加エンジン", "OK" if ok2 else "警告",
                                  "UE %s" % vstr(e["version"])))

    boot_ok = install_bootstrap(tool_dir, user_dir)
    steps.append(("[3] 起動スクリプト", "OK" if boot_ok else "失敗", user_dir))
    if not boot_ok:
        failed = failed or EXIT_BOOTSTRAP

    proj = setup_projects(engines or [engine], project_args, tool_dir, ready_versions)
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
    out("  - ツールは %s を直接読みます（コピーしません）。" % tool_dir)
    out("    このフォルダを移動・削除したら、移動先で Install.bat を実行し直してください。")
    out("  - プラグインを有効化したプロジェクトは、初回起動時に"
        "「モジュールのビルド」を求められることがあります。")
    out("  - アンインストール: Install.bat --uninstall")
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
