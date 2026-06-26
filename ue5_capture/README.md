# UE5.7 Scene Capture Tool

Unreal Engine **5.7** のエディタ上で動作する Python シーンキャプチャツール。
シーン内の既存カメラを指定し、**Color / Z-Depth / Matte Alpha** を PNG / EXR で書き出す。

プロジェクト更新でアセットが消える環境を想定し、**必要なオブジェクト
（SceneCaptureComponent2D / RenderTarget 等）はすべて Python 実行時にランタイム生成し、
キャプチャ後に破棄**する。`.uasset` の保存は行わない（すべて Transient）。

---

## ファイル構成

```
ue5_capture/
├── capture_tool.py   # エントリポイント（GUI 起動 / CUI フォールバック / CONFIG）
├── capture_core.py   # キャプチャロジック（Spawn・キャプチャ・後処理・破棄）
├── capture_ui.py     # tkinter GUI
└── README.md
```

プロジェクト外の任意フォルダ（例 `C:/tools/ue5_capture/`）に置いてよい。
`capture_tool.py` が自分のフォルダを `sys.path` に追加するので、3 ファイルが
同じフォルダにあれば動く。

---

## 必要な外部ライブラリ（重要）

UE の RenderTarget は**全画素を Python から高速に読み取る API が無い**ため、
一旦ファイルへ書き出して numpy / Pillow で後処理する設計。以下が必要：

| ライブラリ | 用途 | 必須度 |
|---|---|---|
| **numpy** | AA ダウンスケール / Depth 正規化 / Matte 閾値 | **必須** |
| **Pillow** | PNG / 16bit PNG の入出力 | **必須** |
| OpenEXR + Imath、または imageio | Depth を **16bit EXR** で出力する場合のみ | 任意 |

> numpy / Pillow が無いとキャプチャは実行されず、Output Log に警告が出る。
> EXR ライブラリが無い場合、Depth 16bit は **16bit PNG（Near/Far 正規化）** に
> 自動フォールバックする（警告ログあり）。

### UE の Python へインストール

UE 同梱 Python に入れる（エディタの Output Log で `py` 実行、または OS のコマンドラインから）：

```bat
:: UE 同梱 Python の実体（バージョンでパスは変わる）
"<UE_5.7>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install numpy pillow

:: EXR を使う場合（任意）
"<UE_5.7>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install OpenEXR Imath
:: もしくは
"<UE_5.7>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install imageio
```

エディタ内からまとめて入れる場合（Output Log → コンソールを `Python` に切替えて）：

```python
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "pillow"])
```

---

## 実行方法

### 1. GUI（推奨）

UE5 エディタの Output Log（モードを `Python` に切替）またはコンソールから：

```
py "C:/tools/ue5_capture/capture_tool.py"
```

tkinter が使える環境ならウィンドウが開く。カメラ・解像度・AA・出力先・パスを設定し
**Capture** を押す。

> GUI は tkinter を UE の Slate tick に非ブロッキング統合している（`mainloop` は使わない）。
> UE 同梱 Python に tkinter（tcl/tk）が含まれない環境では自動的に下記 CUI に切替わる。

### 2. CUI / CONFIG（tkinter が無い環境・バッチ用途）

`capture_tool.py` 冒頭の `LAUNCH_GUI = False` にするか、tkinter が無ければ自動で
CONFIG モードになる。`CONFIG` 辞書を編集して実行する：

```python
CONFIG = {
    "camera_label": "CameraActor_01",   # None なら先頭のカメラ
    "use_camera_resolution": False,
    "override_width": 3840,
    "override_height": 2160,
    "aa_factor": 2,                     # 1 / 2 / 4
    "output_dir": "C:/captures",       # 空ならプロジェクトの Saved/Captures
    "do_color": True,
    "do_depth": True,
    "do_matte": True,
    "depth_bit": "16bit",              # "8bit" or "16bit"
    "depth_near": 0.0,
    "depth_far": 10000.0,
    "matte_use_selection": True,        # 現在選択中のアクターを Matte 対象に
}
```

```
py "C:/tools/ue5_capture/capture_tool.py"
```

---

## 出力仕様

| パス | 形式 | 内容 |
|---|---|---|
| Color | RGBA PNG | `SCS_FinalColorLDR` を AA ダウンスケールして出力 |
| Z-Depth (8bit) | グレースケール PNG | `SCS_SceneDepth`(cm) を Near–Far で 0–255 正規化 |
| Z-Depth (16bit) | EXR (half float) | リニア距離値(cm)をそのまま。EXR 不可時 16bit PNG |
| Matte | グレースケール PNG | 選択アクター=白 / その他=黒（縁は AA） |

**ファイル名**: `{CameraName}_{YYYYMMDD_HHMMSS}_{passType}.{ext}`
例: `CameraActor_01_20260625_143022_color.png`

### アンチエイリアス（Spatial Supersample）
実 RenderTarget を指定解像度の N 倍（1x / 2x / 4x）で描画し、numpy のボックス
フィルタでダウンスケールして出力する。

### Matte の方式
仕様どおり選択アクターに `CustomDepthStencil`(=1) を一時付与し（キャプチャ後に復元）、
実マスクは **`show_only_actors` + SceneDepth のシルエット** で安定生成する
（PostProcessMaterial の動的オーサリングは UE5.7 Python では脆く、アセット不要要件にも
反するため、マテリアル不要のこの方式を採用）。

---

## クリーンアップ / 安全性

- Spawn した SceneCapture2D アクターは `try/finally` で**例外時も必ず破棄**し、
  最後に `collect_garbage()` を呼ぶ。
- RenderTarget は Transient（`KismetRenderingLibrary.create_render_target2d`）。
- Matte で変更した CustomDepth 設定は元の値に復元する。
- 一時ファイルは `<Project>/Saved/UE5CaptureTmp/` に書き出す。

---

## 既知の制約・確認事項

- **本ツールはエディタ内での実機確認が必要**。UE5.7 の Python API 名（`SceneCaptureSource`,
  `RenderTargetFormat`, `SceneCapturePrimitiveRenderMode` の列挙値、`capture_component2d`
  プロパティ名など）はバージョンで揺れることがあるため、最初の実行は Output Log を見ながら。
- Depth の float 取り出しは `export_render_target()` の `.hdr` 経由。`.hdr`(RGBE) は
  相対精度に限界があるため、厳密な距離値が必要なら EXR ライブラリの導入を推奨。
- `use_camera_resolution` はカメラのアスペクト比 × `base_height`(既定1080) で解像度を決める。
- CineCamera の被写界深度（PostProcess）は Color パスにコピーして反映する。
