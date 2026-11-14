# UE Scene Capture Tool

Unreal Engine **5.7 / 5.8** のエディタ上で動く Python 製シーンキャプチャツール。
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

- **Beauty (MRQ)**: Movie Render Queue で対象カメラを通して高品質レンダ（ビューポート露出 + シーケンサ相当の影 / GI / TSR / ウォームアップ）。出力形式は **PNG 8bit / JPG 8bit / EXR 16bit (float)**。
- **Z-Depth**: `SCS_SceneDepth`(cm) を Near/Far で正規化して 8bit / 16bit PNG、または EXR(float) で出力。**手前=白 / 奥=黒 固定**。Normal と同じ共有の可視雲αジョブが自動レンダされ、`depth' = depth × (1−雲α)` で雲領域を黒（=奥）にする（8bit/16bit PNG 対応・**EXR float は対象外**＝生cmは 0=カメラ位置になり意味が壊れるため選択時は注記してスキップ。映像タブの Depth 連番にも同様に適用）。Depth のみを選ぶ場合は雲抜きジョブを伴わない高速なデータパスのみで完結する。
  - **Near/Far 横の「自動」チェック**：可視ジオメトリの最近/最遠距離を低解像度 SceneCapture で実測し、Near/Far 欄へ書き戻す（空・ファープレーンは除外、ロバスト百分位＋余白でクリップを防止）。映像タブでは先頭/中央/終端の3フレームを実測して全編を包むレンジに統合する。
  - 16bit PNG / EXR の深度取得は **RGBA32F** で行う（従来の RGBA16F は 65504cm≈655m で飽和し、far がそれを超えると空がグレーで残るバグがあった。32F が定数値を返す環境では自動で 16F にフォールバック）。
- **Normal**: 法線を RGB に出力（XYZ の -1..1 を `*0.5+0.5` で 0..1 に詰める法線マップ規約）。空間は **カメラ（既定。正対面が青 = V-Ray SamplerInfo 等の規約）/ ワールド** を選択。非描画領域（空=ファープレーン）は黒。Beauty と同一 MRQ ジョブの追加 PP パスなので WPO/風で揺れる前景も Beauty と画素整合。**VDB雲の領域は自動で黒**（雲は法線を持たず奥のジオメトリが透けるため、可視雲モード（**黒バッキング輝度方式**）の**深度順序どおりの雲α**を別ジョブでレンダして乗算。雲を貫くオブジェクトの法線は消えずに残る）。PNG/MP4 は表示用 sRGB エンコードのため、データとして使う場合はリニア化してから -1..1 に戻す。
- **Matteの前（Beauty+Matte）**: クリーンな Beauty にオクルージョン考慮のマットをアルファとして焼いた RGBA(MatteBeauty)。生の白黒マットは内部素材（自動削除）。
- **Matteの奥**: マットオブジェクトの**手前を除去して奥だけを描画**（MRQ Beauty 品質）。near-clip で手前を実除去し、マットのシルエット形状でマスク合成して出力。near-clip は視線に垂直な1枚なので、板より手前に丸ごとあるメッシュアクター（LevelInstance の中身を含む）はアクター単位で隠す。板を跨ぐアクターだけは手前側が黒になる。映像タブで**「空マット」を同時に選ぶと、奥プレートも空黒（fog/PPマテリアル/ブルーム OFF）で撮り、Matteの奥の空が純黒になる**（出力名は Behind のまま）。
- **ObjectID**: 対象アクターを色分け（他は黒）した 1 枚 + 色 -> 名前の対応 JSON。
- **Raw Lighting Full(Sun+GI+Sky)** / **Raw Lighting Direct**（画像タブ）: MRQ の LightingOnly パスによる「アルベド無視のライティングのみ」素材（落ち影 + シェーディング）。Full は太陽 + GI + スカイライト込み（Beauty と同一ジョブ）、Direct は GI / スカイライト / AO を ShowFlag で切った直射のみ（影は完全な黒。専用ジョブを 1 本追加）。出力形式は Beauty と同じ選択に従う。
- **雲マット**: レベル内の全 VDB雲（HeterogeneousVolume）に加え、UDS の雲レイヤー（VolumetricCloud）や雲内フォグ板の雲まで拾う白黒マスク（**雲=黒 / 背景=白**・ソフトエッジ・**深度順序どおり**＝可視雲モード。雲を貫くオブジェクトはマットに乗らず、手前の雲は物体を覆う）。**ベール2本差分方式（4レンダ）**で合成する：①白T+GeoMask（板より手前の透過率・ジオメトリ有無）、②VeilAll（大気・雲ON の黒バッキング素照明＝雲ベール輝度）、③VeilNone/CloudVeil0（大気・雲だけ OFF にした同条件の黒バッキング）、④雲なしBG（半解像度の Beauty）。V=VeilAll−VeilNone の差分で UDS 雲レイヤーやフォグ板の雲まで拾いつつ、空のグラデーションや大気ヘイズを正確に相殺する。飽和カーブで内部を不透明化し（既定 `_VIS_DENSE_GAMMA`=1.5）、出力ファイル名に濃度タグ **`_H5d1.5`** を付与する（濃度設定の識別用）。I2I の雲領域マスク用。両タブ対応（映像は PNG連番/MP4、雲マット/空マットのみ選択ならメイン Beauty ジョブを省略）。静止画の合成ソース（4レンダの EXR/PNG）は合成成功時に削除、合成に失敗した場合のみ診断用に `_cloudsrc/` へ退避して残す。
- **空マット（SkyMatte）**: 両タブにチェックボックスとして追加。空だけ純黒（UDS の Sky_Sphere を黒ドームへ差替え）・PPマテリアル（スタイライズ等）とブルームは強制 OFF・fog も強制 OFF にしつつ、大気光と雲は維持したまま追加の1ジョブでレンダする。シーン/アセットは変更せずレンダ後に自動復元。
- **露出固定**: 両タブのチェック。自動露出（Eye Adaptation）をレンダ中だけ切り（MRQ ジョブ内 cvar `r.EyeAdaptationQuality 0`、レンダ後にエンジンが復元）、フレーム間の明るさの揺れを止める。PPV/カメラは変更しない。固定される明るさは適応が収束した値にほぼ一致する（AV024 実測 ±2 L*）。チェーンの全ジョブ（Behind / 空マット / 雲ヘルパー）に効く。スクリプトからは `unreal._ue5capture_extra_cvars` に cvar を置けば同じ経路で全ジョブに乗る。
- **ビューポートのルック**: 両タブのチェック。エディタのビューポートで見えている見た目（フォグ/露出/ポスプロ）で Beauty を撮る。画像タブは、ビューポートがそのカメラを**すでにパイロットしている**ときだけゲームビューのビューポートを指定解像度で撮影（`take_high_res_screenshot`。撮影中はエディタを前面に置く。背景だとビューポートが間引かれて撮影が遅れる）し、他の素材は従来どおり MRQ。その場でパイロットして撮るとボリュメトリックフォグ/Lumen の履歴が育たず灰色で光の筋の無い画になるため、パイロットしていないとき・EXR 形式・ビューポート無し・タイムアウトは **MRQ 近似**に切り替える。MRQ 近似は Beauty ジョブだけ高品質 cvar を使わず、Game Override を「品質を上げない」設定で置いてエディタのスカラビリティのまま描く（置かないと MRQ が Cinematic スカラビリティの一時設定を自動で足す）。静止画は一時シーケンスのレベル可視性トラックでサブレベル表示もエディタに合わせる。映像タブは MRQ 近似のみ（完全一致ではない）。どちらもパイロット中でないカメラのポスプロウェイトを撮影中だけ 0 にし（ビューポートにはカメラ自身の露出補正・ビネットが乗っていないため）、0 にできないスポーナブルのカメラはテンプレートの露出補正をレベルの PPV と合成した値から打ち消す（`r.ExposureOffset`・ジョブ内 cvar・自動復元。試し撮りなし）。Fog/グローなし・露出固定とは排他（ON の間は無効化）。
- **PPマテリアル OFF（pp_materials_off）**: `render_beauty` / `render_sequence` の内部引数として、レベルのスタイライズ PP マテリアルを `ShowFlag.PostProcessMaterial` で切る。Normal / Depth 等の追加 PP パス（AdditionalPostProcessMaterials 経由）はこの ShowFlag の影響を受けず素通しされる。空マット・雲マット系の内部ジョブが利用する。
- **Overscan**: 元フレームを中央に保ったまま周囲に余白を追加。`%`（一律）/ `px`（X,Y 別）。カメラ filmback を一時拡大して実現。
- **クリーンプレート**: Matte ON のとき Beauty と Z-Depth からマット対象を自動除外（隠せば影/AO も自動で消える）。
- **解像度**: カメラ設定（アスペクト表示）/ 指定（アスペクト維持トグルで幅⇄高さ自動）。出力名は **ファイル名テンプレート**（既定 `{level}_{cut}_{camera}`。`{level}`=永続レベル名、`{cut}`=表示中サブレベルの親フォルダ名とサブレベル名に共通する番号、`{camera}`=カメラ名（映像はカメラカット先頭のカメラ）、`{seq}`=シーケンス名）に `_素材名_NNN` を付けたもの。両タブとも同じ欄で、解決後の名前がその場に表示される。**上書き**トグル ON でテイク番号を進めず、同名の最新テイクの既存出力を消して同じ番号で出し直す。
- **anti-aliasing**: Spatial Supersample (1x / 2x / 4x)。Beauty は MRQ の TSR/Temporal。
- **シーケンスレンダ（映像タブ）**: Sequencer で開いている LevelSequence をカメラカットに従って MRQ レンダ。**素材（Beauty / Z-Depth / Normal / Beauty+Matte(Matteの前) / Matteの奥 / 雲マット / 空マット / ObjectID / Raw Lighting Full(Sun+GI+Sky) / Raw Lighting Direct）それぞれに PNG 連番と H.264 MP4 を個別指定**できる（Raw Lighting Direct は GI/スカイライト/AO を切った直射のみの専用ジョブ。Direct 以外と併用時は2本目ジョブとして自動チェーン。雲マット/空マットのみを選ぶとメインの Beauty ジョブを省略して専用ジョブチェーンから直接開始する）。MRQ は PNG 連番（マスター）を出力し、MP4 は **ffmpeg でシーケンスの Display Rate どおりに**エンコード（CRF プリセット 17/20/24/28 + 数値直接入力・音声なし・余剰フレームは自動トリム）。Matte 系の対象と ObjectID の対象は、タブの外（パネル下部）の「対象」カードにある **Matte targets / Object ID targets** を両タブで共用する（色↔名前の JSON マニフェスト付き。レンダ中のみ r.CustomDepth=3 に切替えて復元）。**連番の形式（PNG 8bit / JPG 8bit / EXR 16bit float。Beauty / Raw Lighting に適用、他は常に PNG）と Z-Depth のビット深度（8bit PNG = Beauty と同一ジョブの PP パス / 16bit PNG・EXR float = フレーム毎に SceneCapture で実測した厳密リニア。EXR は生 cm で MP4 と雲抜きなし）を画像タブと同じ欄で選べる**。フレーム範囲はシーケンス設定または指定。テイク毎サブフォルダ出力のトグルあり。詳細設定の「画像タブの設定をコピー」ボタンで画像タブの解像度 / 出力先 / 形式 / 品質 / Depth 設定を一括コピー。中間ファイル（内部素材・EXR・トリム前連番）はテイクフォルダ内の `_work_<take>` に集約し、成功時はまとめて削除される。雲マットのヘルパージョブはウォームアップを最大8に抑え、雲なしBG は半解像度でレンダしてコストを削る。
- **バックグラウンドでキュー（映像タブ）**: チェックを入れてシーケンスレンダを押すと、画面に出ない 2 台目のエディタ（`-RenderOffScreen`）が同じ設定・同じ処理でレンダする。手元のエディタはその間も使え（別レベルを開いてもよい）、手元での通常キャプチャとも同時に動く。押すたびにキュー（`Saved/UE5Capture_bg/`）へ積まれ、1 台のワーカーが順番に撮り、空になると自分で終了する。**キューリスト**（積むと自動で開く・ボタンで再表示）で状態を見て、実行中の中断・選んだジョブ/待機中全ての取り消し・出力フォルダを開ける。撮るのは保存済みのレベル/シーケンス（未保存の変更があれば確認する）。2 台目のエディタぶんの VRAM/メモリが要る。**裏のエディタが動いている間は、そのジョブのレベル・シーケンス・参照アセットを手元で保存できない**（裏のエディタがファイルを開いているため。保存は失敗し「Unable to Check Out」ダイアログが出る。キューが空になって裏のエディタが終了すれば保存できる）。
- **設定の保持**: 両タブの入力は `Saved/UE5Capture_ui_settings.json` に保存され、次回起動時に復元される。
- GUI (tkinter) は UE の Slate tick に非ブロッキング統合。tkinter が無い環境は CONFIG ベースの CUI に自動フォールバック。

---

## 依存関係 (Dependencies)

### ランタイム

| 要件 | 用途 | 必須度 |
|---|---|---|
| **Unreal Engine 5.7 / 5.8** | 実行環境（エディタ内 Python 3.11） | **必須** |
| **Movie Render Queue プラグイン** (`MovieRenderPipeline`) | Beauty / Behind matte の高品質レンダ、Sequencer API | **必須** |
| **Python Editor Script Plugin** (`PythonScriptPlugin`) | UE の Python 実行 | **必須** |

> どちらのプラグインも既定では無効。`Install.bat` が `.uproject` に追記する
> （手で行う場合は Edit > Plugins で有効化してエディタ再起動）。

### Python ライブラリ（UE 同梱 Python に導入）

| ライブラリ | 用途 | 必須度 |
|---|---|---|
| **numpy** | AA ダウンスケール / Depth 正規化 / マスク合成 | **必須** |
| **Pillow (PIL)** | PNG / 16bit PNG の入出力、合成 | **必須** |
| imageio | Z-Depth を **EXR (float)** で出力する場合のみ（OpenEXR + Imath / opencv-python でも可） | 任意 |
| imageio-ffmpeg | 映像タブの MP4 エンコードに使う ffmpeg 本体（PATH 上の ffmpeg でも可。手持ちの ffmpeg.exe を使うなら、パネル右上の ⚙ →「設定ファイルを開く」で `ffmpeg_path` にパスを書く） | 任意 |
| tkinter (tcl/tk) | GUI。エンジン同梱 Python に入っている。無ければ CUI へ自動フォールバック | 任意 |

版の下限は [`requirements.txt`](requirements.txt)。`Install.bat` が自動で入れる
（[インストール](#インストール)）。手で入れる場合は **OS のシェルから**エンジン同梱の
python.exe を使う:

```bat
"<UE_5.x>\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m pip install -r requirements.txt
```

> エディタ内（Output Log の Python / `execute_python`）から `subprocess` で pip を回さないこと。
> エディタ内の `sys.executable` は `UnrealEditor.exe` なので、2 つ目のエディタが起動して固まる。
> numpy / Pillow が無いとキャプチャは実行されず Output Log に警告が出る。
> EXR ライブラリが無い場合、Z-Depth 16bit は 16bit PNG (Near/Far 正規化) に自動フォールバックする。

### プロジェクト設定（任意）

- **`r.AllowGlobalClipPlane=True`**（Project Settings -> Rendering -> "Support global clip plane for Planar Reflections"）。
  これは **SceneCapture 版の clip-plane behind**（`capture_core.capture_behind_matte`）を直接使う場合のみ必要。
  GUI の **Behind matte は MRQ の near-clip 方式**を使うため**この設定は不要**。有効化には DefaultEngine.ini への記載 + エディタ再起動が必要。

---

## インストール

配布 zip（またはこのリポジトリ）を**消さない場所**に展開し、**`Install.bat` を実行する**。
ツール本体はコピーされず、展開した場所の `ue5_capture\` が直接読み込まれる
（ローカルでも共有ドライブでも可。日本語やスペースを含むパスも可）。

```
Install.bat                                # 対話（プロジェクトは一覧から選ぶ）
Install.bat "<dir>\Foo.uproject"           # .uproject をドラッグ＆ドロップしてもよい
Install.bat --yes --no-pause               # 無人実行（.uproject 指定が無ければプロジェクト設定はスキップ）
Install.bat "C:\Program Files\Epic Games\UE_5.8"   # エンジンを明示指定
Install.bat --dry-run                      # 何をするかだけ表示（何も書き込まない）
Install.bat --no-libs                      # ライブラリ導入（pip）を飛ばす
Install.bat --uninstall                    # 起動スクリプトを外す
Install.bat --help
```

Install.bat が行うこと:

1. **エンジンを決める**（対応: UE 5.7 / 5.8）— レジストリ（ランチャー版とソースビルド）/
   Epic ランチャーの `LauncherInstalled.dat` / よくあるインストール先を探す。
   `.uproject` を渡すとその `EngineAssociation` のエンジン、無ければ対応版の最新を使う
2. **同梱 Python にライブラリを導入** — numpy / Pillow（必須）、imageio、imageio-ffmpeg（任意）。
   すでに入っているものは飛ばす。エンジンフォルダに書き込めない場合は
   `Documents\UnrealEngine\Python\ue5_capture_libs\py311` に入れる（起動スクリプトが
   `sys.path` に追加）。起動スクリプトは全エンジン共通なので、**見つかった他の対応エンジンにも入れる**
   （確認あり）。pip は PyPI に接続するため**インターネット接続が必要**。
   zip の隣に `wheels\` フォルダがあれば先にそこからオフラインで入れる（[配布](#配布)）
3. **起動スクリプトを設置** — `Documents\UnrealEngine\Python\` に
   `ue5_capture_bootstrap.py` を置き、`init_unreal.py` に import 行を追記する。
   このフォルダは UE が全プロジェクト・全エンジンで自動実行するので、**プロジェクト毎の
   Startup Scripts 登録は不要**（既存の `init_unreal.py` はマーカーで囲んだ自分のブロック
   だけを差し替えるので、他ツールの記述は残る）
4. **プロジェクトのプラグインを有効化** — `.uproject` に
   `PythonScriptPlugin` と `MovieRenderPipeline` を追記する（SequencerScripting /
   LevelSequenceEditor は MovieRenderPipeline の依存として自動で有効になるため追記しない）。
   書き換え前に `<名前>.uproject.bak` を作る

何度実行してもよい（済んでいる工程は飛ばし、起動スクリプトは差し替える）。
途中の工程が失敗しても残りは続行し、最後に工程ごとの結果表と終了コードを表示する。
**終了コードがどの工程で止まったかを表す**（バッチはこの値をそのまま返す）:

| コード | 意味 |
|---|---|
| 0 | 成功 |
| 10 | `ue5_capture` フォルダが Install.bat の隣に無い（展開ミス） |
| 20 | Unreal Engine を特定できない（引数でエンジンのパスを渡す） |
| 30 | 必須 Python ライブラリの導入に失敗（ネットワーク / プロキシ） |
| 40 | 起動スクリプトを書けない / 消せない（Documents への書き込み権限） |
| 50 | 選んだ `.uproject` を更新できない（読み取り専用 / バージョン管理のロック） |
| 90 | 想定外のエラー |
| 130 | Ctrl-C で中断 |

実行後、**エディタを再起動**すればメニューバーの **SceneCapture** と
ツールバー右端の **SceneCapture** ボタンが出る（Output Log に `[SceneCapture] v<版>` が出る）。

- **フォルダを移動・削除した場合**: 起動スクリプトは展開先の絶対パスを指しているので、
  移動先の `Install.bat` を実行し直す（実行しないと起動時に「ツール本体が見つかりません」の警告）。
- **更新**: 新しい zip を同じ場所に上書き展開する（git なら `git pull`）。再インストールは不要。
- **アンインストール**: `Install.bat --uninstall`。起動スクリプトと `ue5_capture_libs` を消す。
  `.uproject` のプラグイン設定とエンジン側のライブラリは残す。

---

## 使い方

### メニュー / ツールバーから起動（推奨）

`Install.bat` を実行済みならエディタ再起動だけでメニューバーに **SceneCapture**
メニューと、ツールバー右端に **SceneCapture** ボタンが出る。

インストーラを使わず手動で入れる場合（プロジェクト毎に1回）:

1. `Project Settings > Plugins > Python > Startup Scripts` の「+Add」に
   `<install dir>/ue5_capture/capture_menu.py` を追加
2. エディタを再起動

メニューは `Panel...`（ツールを開く）/ `Open Output Folder` / `Settings...` の3項目。
起動時にモジュールを reload するので、コード修正後もメニュー/ボタンから開くだけで
最新版が立ち上がる。再起動せずに即席で登録する場合は
`py "<install dir>/ue5_capture/capture_menu.py"` を一度実行すればよい（再実行しても重複しない）。

### コンソールから起動

UE エディタの Output Log（モードを `Python` に切替）またはコンソールから:

```
py "<install dir>/ue5_capture/capture_tool.py"
```

GUI が開く。画像タブでカメラ / 解像度 / AA / 出力先 / 各パスを設定し **Capture**、
映像タブでシーケンスと形式を設定し **シーケンスレンダ** を押す。Matte / ObjectID の対象は
パネル下部の「対象」カード（両タブ共通）で指定する。レンダ中は両タブのボタンが無効になる。
詳細（CUI / CONFIG モード、出力仕様、Matte 方式、安全性）は
[`ue5_capture/README.md`](ue5_capture/README.md)、シーケンスレンダの仕様は
[`ue5_capture_sequence_spec.md`](ue5_capture_sequence_spec.md) を参照。

---

## 配布

```
powershell -ExecutionPolicy Bypass -File build_dist.ps1            # dist\UE_SceneCaptureTool_v<VERSION>.zip
powershell -ExecutionPolicy Bypass -File build_dist.ps1 -Wheels    # オフライン用 wheels\ 同梱（約 50MB 増）
powershell -ExecutionPolicy Bypass -File build_dist.ps1 -OutDir <フォルダ>
```

版は `VERSION`（x.y.z）が正本で、zip 名と Install.bat の表示に使われる。上げたら
`CHANGELOG.md` に追記する。zip はリポジトリ直下と同じ並びで、`docs/`（内部の作業メモ）と
`__pycache__` は含めない。受け取った人向けの手順は zip 直下の `INSTALL.txt`。

---

## ファイル構成

```
UE_SceneCaptureTool/
├── Install.bat                  # インストーラ（エンジン検出・pip・起動スクリプト・プラグイン有効化・--uninstall）
├── INSTALL.txt                  # 配布 zip 受け取り側の最小手順
├── README.md                    # 本ファイル
├── VERSION                      # 版（x.y.z）
├── CHANGELOG.md                 # 変更履歴
├── requirements.txt             # Python ライブラリと版の下限（Install.bat と同じ内容）
├── build_dist.ps1               # 配布 zip の作成（zip には含めない）
├── ue5_capture_tool_prompt.md   # 設計プロンプト / 仕様（画像キャプチャ）
├── ue5_capture_sequence_spec.md # シーケンスレンダ（映像タブ）仕様
├── docs/                        # 開発中の検証メモ（zip には含めない）
└── ue5_capture/
    ├── capture_tool.py          # エントリポイント（GUI / CUI / CONFIG）
    ├── capture_core.py          # キャプチャロジック（Spawn・キャプチャ・後処理・破棄・合成）
    ├── capture_mrq.py           # Movie Render Queue による Beauty / Behind / シーケンスレンダ
    ├── capture_ui.py            # tkinter GUI（画像 / 映像タブ）
    ├── capture_bg.py            # バックグラウンドの待ち行列・ワーカーの起動/生存確認/停止（unreal 非依存）
    ├── capture_bg_worker.py     # 裏のエディタ内で待ち行列を順に処理するワーカー
    ├── capture_menu.py          # メニューバー登録（起動スクリプト / 手動の Startup Script から呼ばれる）
    ├── startup_menu.py          # ツールバーボタン登録（capture_menu から呼ばれる）
    └── README.md                # エディタ内詳細仕様
```

`capture_*.py` は同じフォルダにあれば動く（`capture_tool.py` が自身のフォルダを
`sys.path` に追加する）。プロジェクト外の任意フォルダに置いてよい。
