# UE Scene Capture Tool

Unreal Engine **5.7** のエディタ上で動く Python 製シーンキャプチャツール。
GUI は **画像キャプチャ** / **映像キャプチャ** の 2 タブ構成。
画像タブはシーン内の既存カメラを指定して **Beauty / Z-Depth / Matte / Object ID** などを
PNG / EXR で書き出す。映像タブは Sequencer で開いている LevelSequence を
**PNG 連番 / MP4** でレンダする。SceneCaptureComponent2D / RenderTarget はすべて実行時に
ランタイム生成し、キャプチャ後に破棄する（`.uasset` は保存しない / Transient）。

ツール本体は [`ue5_capture/`](ue5_capture/) 以下。エディタ内詳細仕様は
[`ue5_capture/README.md`](ue5_capture/README.md)、設計プロンプトは
[`ue5_capture_tool_prompt.md`](ue5_capture_tool_prompt.md) を参照。

---

## 主な機能

- **Beauty (MRQ)**: Movie Render Queue で対象カメラを通して高品質レンダ（ビューポート露出 + シーケンサ相当の影 / GI / TSR / ウォームアップ）。
- **Z-Depth**: `SCS_SceneDepth`(cm) を Near/Far で正規化して 8bit / 16bit PNG、または EXR(float) で出力。`手前=白/奥=黒` 反転可。
- **Matte (B/W)**: 指定オブジェクトのオクルージョン考慮シルエット。`+ Beauty + Matte alpha` で Beauty に α 合成した RGBA(MatteBeauty) も出力。
- **Behind matte**: マットオブジェクトの**手前を除去して奥だけを描画**（MRQ Beauty 品質）。near-clip で手前を実除去し、マットのシルエット形状でマスク合成して出力。
- **Object ID**: アクターを色分けした 1 枚 + 色 -> 名前の対応 JSON。`+ Beauty with alpha`(ObjectIDBeauty) で α 合成 RGBA、`+ Hide-render`(ObjectIDClean) でクリーンプレートも。
- **Overscan**: 元フレームを中央に保ったまま周囲に余白を追加。`%`（一律）/ `px`（X,Y 別）。カメラ filmback を一時拡大して実現。
- **クリーンプレート**: Matte ON のとき Beauty と Z-Depth からマット対象を自動除外（隠せば影/AO も自動で消える）。
- **解像度**: Use Camera Setting（アスペクト表示）/ Override（アスペクト維持トグルで幅⇄高さ自動）。出力名は `[任意名]_[カメラ名]_素材名_NNN`。
- **anti-aliasing**: Spatial Supersample (1x / 2x / 4x)。Beauty は MRQ の TSR/Temporal。
- **シーケンスレンダ（映像タブ）**: Sequencer で開いている LevelSequence をカメラカットに従って MRQ レンダ。PNG 連番と **H.264 MP4**（UE 内蔵エンコーダ・CRF プリセット 17/20/24/28・音声なし）を同時出力可。Z-Depth AOV（Near/Far 正規化・表示用エンコード）もパス毎の連番 / MP4 で出力できる。フレーム範囲はシーケンス設定または指定、fps はシーケンスの Display Rate。テイク毎サブフォルダ出力のトグルあり。「画像キャプチャの設定を転送」ボタンで画像タブの解像度 / 出力先 / 品質 / Depth 設定を一括コピー。
- **設定の保持**: 両タブの入力は `Saved/UE5Capture_ui_settings.json` に保存され、次回起動時に復元される。
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

### ツールバーから起動（推奨）

`Documents/UnrealEngine/Python/init_unreal.py` が `startup_menu.register()` を呼ぶことで、
レベルエディタのツールバー右端に **SceneCapture** ボタンが常設される（全プロジェクト共通・
プロジェクトファイルには手を入れない）。ボタンはモジュールを reload してから GUI を
開くので、コード修正後もボタンを押すだけで最新版が立ち上がる。

### コンソールから起動

UE5.7 エディタの Output Log（モードを `Python` に切替）またはコンソールから:

```
py "<path>/ue5_capture/capture_tool.py"
```

GUI が開く。画像タブでカメラ / 解像度 / AA / 出力先 / 各パスを設定し **Capture**、
映像タブでシーケンスと形式を設定し **Sequence Render** を押す。
詳細（CUI / CONFIG モード、出力仕様、Matte 方式、安全性）は
[`ue5_capture/README.md`](ue5_capture/README.md)、シーケンスレンダの仕様は
[`ue5_capture_sequence_spec.md`](ue5_capture_sequence_spec.md) を参照。

---

## ファイル構成

```
UE_SceneCaptureTool/
├── README.md                    # 本ファイル
├── ue5_capture_tool_prompt.md   # 設計プロンプト / 仕様（画像キャプチャ）
├── ue5_capture_sequence_spec.md # シーケンスレンダ（映像タブ）仕様
└── ue5_capture/
    ├── capture_tool.py          # エントリポイント（GUI / CUI / CONFIG）
    ├── capture_core.py          # キャプチャロジック（Spawn・キャプチャ・後処理・破棄・合成）
    ├── capture_mrq.py           # Movie Render Queue による Beauty / Behind / シーケンスレンダ
    ├── capture_ui.py            # tkinter GUI（画像 / 映像タブ）
    ├── startup_menu.py          # ツールバー起動ボタン登録（init_unreal.py から呼ぶ）
    └── README.md                # エディタ内詳細仕様
```

3 つの `capture_*.py` は同じフォルダにあれば動く（`capture_tool.py` が自身のフォルダを
`sys.path` に追加する）。プロジェクト外の任意フォルダに置いてよい。
