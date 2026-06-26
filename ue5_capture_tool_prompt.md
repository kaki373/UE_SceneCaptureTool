# UE5 Scene Capture Tool — Claude Code Prompt

## 概要

Unreal Engine 5.7 のエディタ上で動作するシーンキャプチャツールを Python で作成してください。シーン内の既存カメラアクターを指定し、Color / Z-Depth / Matte Alpha をPNGまたはEXRで任意のフォルダに書き出します。

プロジェクト更新で追加アセットが消される可能性がある環境のため、**Pythonスクリプトの実行だけで必要なオブジェクト（SceneCaptureComponent、RenderTarget、PostProcessMaterial等）をすべてランタイムで生成・設定し、キャプチャ後に破棄する**設計にしてください。アセットの保存（uasset）は行わず、すべてTransientパッケージ上に作成してください。

## 技術方式

- **SceneCaptureComponent2D + TextureRenderTarget2D** をPythonからSpawnして使用する
- 指定されたカメラアクターの Transform / FOV / その他カメラ設定を SceneCaptureComponent に転写する
- キャプチャ後、Spawn したアクター・コンポーネント・RenderTarget はすべて破棄する
- スクリプトファイルはプロジェクト外の任意のパスに配置可能とする（`py "C:/tools/capture.py"` 等で実行）

## 機能要件

### 1. カメラ指定
- シーン内の CameraActor / CineCameraActor をドロップダウンで一覧表示し、1つ選択する
- 選択したカメラの位置・回転・FOV・被写界深度設定等を取得して SceneCaptureComponent2D に反映する

### 2. 解像度・アスペクト比
- **「カメラの現在設定を維持」モード**: カメラのアスペクト比とプロジェクトのデフォルト解像度を使用
- **「オーバーライド」モード**: 幅 × 高さをピクセルで直接指定（例: 3840×2160, 7680×4320 等）

### 3. Color出力
- RGBA PNG で書き出す
- 出力先フォルダは GUI 上でパスを指定（デフォルトはプロジェクトの Saved/Captures/）

### 4. Z-Depth出力（オプション）
- GUI のチェックボックスで有効/無効を切り替え
- **8bit モード**: Near/Far 距離（cm単位）を指定し、その範囲を 0–255 に正規化して PNG 出力
- **16bit モード**: EXR (float16) でリニア距離値をそのまま書き出す
- Near/Far 距離は GUI 上で入力可能にする（デフォルト: Near=0, Far=10000）

### 5. Matte Alpha出力（オプション）
- GUI のチェックボックスで有効/無効を切り替え
- **マット対象の指定方法**: エディタ上で現在選択中のアクター群をマット対象とする（`unreal.EditorLevelLibrary.get_selected_level_actors()` を使用）
- 選択アクターに CustomDepthStencil を一時的に有効化（Stencil Value = 1）
- SceneCaptureComponent 側で CustomDepth/Stencil を利用し、マット対象 = 白（255）、その他 = 黒（0）のマスク画像をPNG出力
- キャプチャ後、CustomDepthStencil 設定を元の状態に復元する

### 6. アンチエイリアス
- SceneCaptureComponent2D のスーパーサンプリング的なAA対応
- 方法: 実際のRenderTarget解像度を指定解像度の N 倍（2x or 4x）で描画し、ダウンスケールして出力する（Spatial Supersample）
- GUI でAAの倍率を選択可能にする（1x / 2x / 4x、デフォルト: 2x）

### 7. ファイル命名
- 命名規則: `{CameraName}_{Timestamp}_{PassType}.{ext}`
  - PassType: `color`, `depth`, `matte`
  - Timestamp: `YYYYMMDD_HHMMSS`
  - 例: `CameraActor_01_20260625_143022_color.png`

## GUI

**Editor Utility Widget (EUW)** として実装する。ただし EUW の .uasset が消される可能性があるため、Python スクリプト実行時に EUW が存在しなければ Python 側から動的に Slate ウィンドウ（`unreal.SWindow` 等）を生成してGUIを構成する方針とする。もし UE5.7 の Python API で Slate ウィンドウの動的生成が困難であれば、EUW を Python から動的に生成・登録する方法、または コンソールコマンドで引数指定する CUI フォールバックも許容する。

### GUI レイアウト案

```
┌─ Scene Capture Tool ─────────────────────────┐
│                                               │
│  Camera:     [▼ CameraActor_01           ]    │
│                                               │
│  Resolution: (○) Use Camera Setting            │
│              (●) Override: [3840] x [2160]     │
│                                               │
│  AA:         [▼ 2x                       ]    │
│                                               │
│  Output Dir: [C:/captures/               ][…] │
│                                               │
│  ─── Passes ──────────────────────────────    │
│  ☑ Color (PNG)                                │
│  ☑ Z-Depth                                    │
│      Bit Depth: [▼ 16bit EXR]                 │
│      Near (cm): [0     ]  Far (cm): [10000]   │
│  ☑ Matte Alpha (use selected actors)          │
│                                               │
│            [ ▶ Capture ]                      │
└───────────────────────────────────────────────┘
```

## 技術的な注意事項

- UE5.7 の `unreal` Python モジュールを使用すること
- `unreal.EditorLevelLibrary`, `unreal.GameplayStatics`, `unreal.SceneCaptureComponent2D`, `unreal.TextureRenderTarget2D` 等のクラスを活用する
- RenderTarget からピクセルデータを読み取る際は `unreal.RenderTargetLibrary` または `export_render_target()` を使用する
- EXR 書き出しが UE Python API で直接困難な場合、RenderTarget のピクセルを numpy 等で受け取り OpenEXR ライブラリ経由で書き出す方法も許容する（ただし外部ライブラリの依存は最小限にする）
- PostProcessMaterial を Transient で動的生成する場合、`unreal.MaterialInstanceDynamic` を活用する
- **エラーハンドリング**: カメラ未選択、出力先パス不在、RenderTarget生成失敗等に対して `unreal.log_warning()` で通知する
- **クリーンアップ**: キャプチャ完了後、例外発生時も含めて try/finally で生成したオブジェクトをすべて確実に破棄する

## 外部依存ライブラリ

UE の RenderTarget は全画素を Python から高速に読み取る API が無いため、一旦ファイルへ書き出してから画像処理ライブラリで後処理する。以下を UE 同梱 Python に `pip install` すること。

| ライブラリ | 用途 | 必須度 |
|---|---|---|
| **numpy** | AA ダウンスケール / Z-Depth 正規化 / Matte 閾値処理 | **必須** |
| **Pillow** | PNG / 16bit PNG の入出力 | **必須** |
| OpenEXR + Imath、または imageio | Z-Depth を 16bit EXR で出力する場合のみ | 任意 |

- numpy / Pillow が無い場合、キャプチャは実行せず `unreal.log_warning()` で通知する。
- EXR ライブラリが無い場合、Z-Depth 16bit は 16bit PNG（Near/Far 正規化）に自動フォールバックする。

インストール例:
```bat
"<UE_5.7>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install numpy pillow
"<UE_5.7>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install OpenEXR Imath
```

## ディレクトリ構成

```
C:/tools/ue5_capture/           （プロジェクト外）
├── capture_tool.py             # メインスクリプト（エントリポイント）
├── capture_core.py             # キャプチャロジック
├── capture_ui.py               # GUI構築
└── README.md                   # 使い方
```

## 実行方法

UE5 エディタの Output Log または Python コンソールから:

```
py "C:/tools/ue5_capture/capture_tool.py"
```

これで GUI が起動し、設定後「Capture」ボタンでキャプチャが実行される。
