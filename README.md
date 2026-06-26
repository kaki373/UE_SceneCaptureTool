# UE Scene Capture Tool

Unreal Engine **5.7** のエディタ上で動く Python 製シーンキャプチャツール。
シーン内の既存カメラを指定し、**Beauty / Z-Depth / Matte / Object ID** などを
PNG / EXR で書き出す。SceneCaptureComponent2D / RenderTarget はすべて実行時に
ランタイム生成し、キャプチャ後に破棄する（`.uasset` は保存しない / Transient）。

ツール本体は [`ue5_capture/`](ue5_capture/) 以下。エディタ内詳細仕様は
[`ue5_capture/README.md`](ue5_capture/README.md)、設計プロンプトは
[`ue5_capture_tool_prompt.md`](ue5_capture_tool_prompt.md) を参照。

---

## 主な機能

- **Beauty (MRQ)**: Movie Render Queue で対象カメラを通して高品質レンダ（ビューポート露出 + シーケンサ相当の影 / GI / TSR / ウォームアップ）。出力 `beauty.png`。
- **Z-Depth**: `SCS_SceneDepth`(cm) を Near/Far で正規化して 8bit / 16bit PNG、または EXR(float) で出力。
- **Matte (B/W)**: 指定オブジェクトの白黒シルエットマスク。`+ Beauty + Matte alpha PNG` で Beauty に α 合成した RGBA も出力。
- **Behind matte**: マットオブジェクトの**手前を除去して奥だけを描画**（MRQ Beauty 品質）。カメラ -> マット面距離の near-clip で手前を実除去し、**マットのシルエット形状でマスク合成**して `behindmatte.png` を出力。
- **No drop shadow / AO**: マットオブジェクトが他オブジェクトに落とす**影 (CastShadow) と AO (DistanceField / DynamicIndirect) を OFF** にするトグル。
- **Object ID**: アクターを色分けした 1 枚 + 色 -> 名前の対応 JSON。`+ Beauty + ObjectID mask` で α 合成 RGBA、`+ Hide-render` でクリーンプレートも。
- **クリーンプレート**: Matte ON のとき Beauty と Z-Depth からマット対象を自動除外。
- **AA**: Spatial Supersample (1x / 2x / 4x)。
- GUI (tkinter) は UE の Slate tick に非ブロッキング統合。tkinter が無い環境は CONFIG ベースの CUI に自動フォールバック。

---

## 依存関係 (Dependencies)

### ランタイム

| 要件 | 用途 | 必須度 |
|---|---|---|
| **Unreal Engine 5.7** | 実行環境（エディタ内 Python） | **必須** |
| **Movie Render Queue プラグイン** | Beauty / Behind matte の高品質レンダ | **必須**（Beauty 系を使う場合） |
| **Python Editor Script Plugin** | UE の Python 実行 | **必須** |

> Movie Render Queue は UE の組み込みプラグイン。プロジェクト設定の Plugins で
> "Movie Render Queue" を有効化してエディタ再起動が必要。

### Python ライブラリ（UE 同梱 Python に導入）

| ライブラリ | 用途 | 必須度 |
|---|---|---|
| **numpy** | AA ダウンスケール / Depth 正規化 / マスク合成 | **必須** |
| **Pillow (PIL)** | PNG / 16bit PNG の入出力、合成 | **必須** |
| imageio または (OpenEXR + Imath) または opencv-python | Z-Depth を **EXR (float)** で出力する場合のみ | 任意 |
| tkinter (tcl/tk) | GUI を使う場合。無ければ CUI へ自動フォールバック | 任意 |

UE 同梱 Python へのインストール例:

```bat
"<UE_5.7>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install numpy pillow
:: EXR を使う場合のみ（いずれか）
"<UE_5.7>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install imageio
```

エディタ内からまとめて入れる場合（Output Log のコンソールを `Python` に切替）:

```python
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "pillow"])
```

> numpy / Pillow が無いとキャプチャは実行されず Output Log に警告が出る。
> EXR ライブラリが無い場合、Z-Depth 16bit は 16bit PNG (Near/Far 正規化) に自動フォールバックする。

### プロジェクト設定（任意）

- **`r.AllowGlobalClipPlane=True`**（Project Settings -> Rendering -> "Support global clip plane for Planar Reflections"）。
  これは **SceneCapture 版の clip-plane behind**（`capture_core.capture_behind_matte`）を直接使う場合のみ必要。
  GUI の **Behind matte は MRQ の near-clip 方式**を使うため**この設定は不要**。有効化には DefaultEngine.ini への記載 + エディタ再起動が必要。

---

## 使い方

UE5.7 エディタの Output Log（モードを `Python` に切替）またはコンソールから:

```
py "<path>/ue5_capture/capture_tool.py"
```

GUI が開く。カメラ / 解像度 / AA / 出力先 / 各パスを設定し **Capture** を押す。
詳細（CUI / CONFIG モード、出力仕様、Matte 方式、安全性）は
[`ue5_capture/README.md`](ue5_capture/README.md) を参照。

---

## ファイル構成

```
UE_SceneCaptureTool/
├── README.md                  # 本ファイル
├── ue5_capture_tool_prompt.md # 設計プロンプト / 仕様
└── ue5_capture/
    ├── capture_tool.py        # エントリポイント（GUI / CUI / CONFIG）
    ├── capture_core.py        # キャプチャロジック（Spawn・キャプチャ・後処理・破棄・合成）
    ├── capture_mrq.py         # Movie Render Queue による Beauty / Behind レンダ
    ├── capture_ui.py          # tkinter GUI
    └── README.md              # エディタ内詳細仕様
```

3 つの `capture_*.py` は同じフォルダにあれば動く（`capture_tool.py` が自身のフォルダを
`sys.path` に追加する）。プロジェクト外の任意フォルダに置いてよい。
