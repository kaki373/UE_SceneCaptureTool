# UE5.7 Scene Capture Tool

Unreal Engine **5.7** のエディタ上で動く Python シーンキャプチャツール。
シーン内の既存 **CineCamera** を指定し、同一フレーム・同一解像度で

- **Beauty**（＝ビューポート/シーケンサ相当の高品質カラー）
- **Z-Depth** / **Matte** / **Object ID** / **Behind**（マットの向こう側）

を PNG/EXR で書き出す。データ系パスは SceneCapture をランタイム生成して撮り、
Beauty は **Movie Render Queue (MRQ)** で対象カメラを通してレンダリングする。
`.uasset` は基本作らない（一時 LevelSequence は作って即削除）。

---

## ファイル構成
```
ue5_capture/
├── capture_tool.py   # エントリポイント（GUI 起動 / CUI フォールバック / CONFIG）
├── capture_core.py   # SceneCapture 系（Depth/Matte/ObjectID）・合成・命名・後処理
├── capture_mrq.py    # MRQ 経由の Beauty 高品質レンダ（PIE・非同期）
├── capture_ui.py     # tkinter GUI（UE の Slate tick に非ブロッキング統合）
└── README.md
```

## 必要ライブラリ
| ライブラリ | 用途 | 必須度 |
|---|---|---|
| **numpy** | 画素後処理（AA/正規化/合成/マスク） | **必須** |
| **Pillow** | PNG 入出力 | **必須** |
| OpenEXR+Imath / imageio | Depth を EXR(float) で出す場合のみ | 任意 |

UE 同梱 Python に入れる（OS 側シェルから。`<UE>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe`）。
> ⚠️ `execute_python` から `subprocess` で pip を回さないこと。UE の `sys.executable` は
> `UnrealEditor.exe` で、2つ目のエディタが pip モードで起動しエディタが固まる。OS 側で入れる。
> UE が import する site-packages は `<Project>/Intermediate/PipInstall/Lib/site-packages`。

---

## 実行（GUI）
UE の Output Log を Python に切替えて：
```
py "D:/webui/ClaudeCode/UE_capture/ue5_capture/capture_tool.py"
```
ウィンドウ「**Scene Capture Tool (UE5.7) ★Beauty版★**」が開く。設定後、唯一のボタン
**▶ Capture (Beauty + Depth/Matte/ObjectID)** を押すと、データ系を出力 → MRQ Beauty（PIE に入る）→
Beauty 合成、の順で全パスが揃う。

> 出力先が無効になる「残留ウィンドウ」を避けるため、`show()` は登録簿（`unreal._ue5capture_windows`）
> 経由で既存ウィンドウを閉じてから1枚だけ開く。コード変更後にウィンドウが増えたら
> `for o in gc.get_objects(): type(o).__name__=='CaptureWindow' and o._on_close()` で一掃できる。

---

## 出力パスと素材名
ファイル名は **`[任意名]_[カメラ名]_素材名_NNN.ext`**（任意名/カメラ名は GUI のチェックで含める/外す、
NNN は出力フォルダ内の通し番号）。素材名（クリーン名）：

| 素材名 | 形式 | 内容 | エンジン |
|---|---|---|---|
| **Beauty** | PNG/EXR | カメラ実露出+PPV+影/GI/TSR の高品質（シーケンサ相当） | MRQ |
| **Depth** | 16bit/8bit PNG or EXR | カメラからの距離(**cm**)。Near/Far 正規化、`手前=白/奥=黒`反転可。EXR は生cm | SceneCapture |
| **Normal** | RGB PNG | GBuffer 法線（XYZ の -1..1 → `*0.5+0.5`）。空間はカメラ（既定・正対=青）/ワールド選択。Beauty と同一ジョブの PP パス＝画素整合。sRGB エンコード注意 | MRQ |
| **Matte** | 白黒 PNG | 対象アクターのオクルージョン考慮シルエット | SceneCapture |
| **MatteBeauty** | RGBA PNG | Beauty に Matte をαとして合成 | MRQ+合成 |
| **ObjectID** | RGB PNG + `.json` | 対象を色分け（黄金角で分離）+ 色→名 対応表 | SceneCapture |
| **ObjectIDBeauty** | RGBA PNG | Beauty に ObjectID カバレッジをαとして合成 | MRQ+合成 |
| **ObjectIDClean** | RGBA PNG | ObjectID 対象を隠した Beauty クリーンプレート（2回目 MRQ） | MRQ |
| **Behind** | RGBA PNG | マット対象の向こう側だけ（対象を隠した Beauty を near-clip + マットシルエットで切抜き） | MRQ+合成 |

## 主な設定
- **露出**：MRQ が実カメラの物理露出+PostProcessVolume で描くのでビューポート/ゲームと一致。
  （SceneCapture 単発は eye-adaptation が収束せず暗くなるため Color パスは廃止。露出 UI も無し。）
- **Resolution**：`Use Camera Setting`（カメラのアスペクトを表示・幅から高さ算出）/ `Override`（W×H、
  `アスペクト維持` で幅⇄高さ自動）。
- **Overscan**：ON で元フレームを中央に保ったまま周囲に余白を追加。`%`（一律）/ `px`（**X,Y 別**）。
  実装はカメラ **filmback の sensor 幅/高さを一時拡大→FOV を縦横独立に広げ→レンダ後復元**、解像度も ×(1+f)。
- **anti-aliasing**：SceneCapture 系の Spatial Supersample 倍率（1x/2x/4x）。Beauty は MRQ の TSR+Temporal。
- **Matte**：ON のとき Beauty から対象を**常に隠す**（クリーンプレート）。隠せば対象の影/AO も自動で消える。
- **Fog OFF**：Beauty レンダ時に `r.Fog 0` / `r.VolumetricFog 0`。
- **MRQ 品質**：Warmup（Lumen/影の収束。**32 以上推奨**。低いと暗くなる）、Temporal サンプル数、EXR。

## VDB雲（HeterogeneousVolume）のマット / ObjectID
HeterogeneousVolumeComponent（SparseVolumeTexture=VDB の雲等）は CustomDepth/SceneDepth を
書かないため、従来のステンシル/深度比較のマスクには一切写らない。ツールは以下で扱う。
動作は両タブ共通の「**VDB雲: Auto / ON / OFF**」セレクタで切替（既定 Auto=レベルに HV が
あれば自動で有効。ON=検出に関わらず強制（板の手前の半透明メッシュ等にも効く）。
OFF=雲を完全無視した従来の書き出し＝雲対象は注記を出して無視）。

- **雲マット＝バッキング方式（板対象があるとき・既定）**: マット板を**発光100の
  アンリット白材で1回レンダ**（EXR・シーンリニア(トーンカーブ無効)・TS=1・露出固定・
  何も隠さない）。線形色 W ≈ T×素板レベル（手前の内容の発光は 1/100 で無視できる）から
  「板より手前にある内容の透過率 T」を取得し、板マスクへ `mask' = 255−(255−mask)×T` で
  統合する。体積レンダは背景放射に対して線形なので T は数学的に正確。
  **板の手前の雲・半透明は他のオブジェクトと同じ遮蔽挙動**になり、板の後ろの雲は
  板に遮られて写らないため自動的に無効（雲を Matte 対象に指定する必要はない）。
  雲は Beauty から隠さず写ったまま。EXR の読みは同梱 ffmpeg の
  `format=gbrpf32le,extractplanes=g` → PFM(float32) → PIL（ビット一致・クリップなし）。
- **雲のみが Matte 対象（板なし）のとき**: 従来の**分離モード CloudMatte ジョブ**
  （雲以外の非ライトアクターを隠して α レンダ・遮蔽なし全投影・完了メッセージに注記）。
- **雲 ObjectID**（静止画のみ）: 対象の雲1つずつ分離モードジョブを回し、α≥0.5 の画素を
  色分けして ObjectID PNG / JSON マニフェストへ合成する。映像の ObjectID は雲非対応。
- ⚠️ 遮蔽考慮の**ホールドアウト方式は UE5.7 では使えない**（実測 2026-07-24）:
  `r.Deferred.SupportPrimitiveAlphaHoldout=True` を単独有効化すると HV 描画で
  RWHoldoutTexture 未束縛の Fatal クラッシュ、`r.PostProcessing.PropagateAlpha=True` と
  ペアならクラッシュしないが holdout αがほぼ 0 のまま（出力が壊れている）。
  コード上は `_set_cloud_matte_mode(use_holdout=...)` に経路を残してある（エンジン修正待ち）。
- **静止画（時間凍結）の TS>1 は同一ジョブの Matte/Depth PP パスを 50% に希釈する**
  （TS=2 でマスク黒が sRGB(0.5)=187 になる実測。映像レンダでは起きない）。凍結静止画の
  サブフレームは同一内容で TS>1 に意味が無いため、PP パスがあるとき自動で TS=1 になる。

---

## UE5.7 API の要点（ハマりどころ）
- **列挙体**：`unreal.TextureRenderTargetFormat`（×`RenderTargetFormat`）、
  `RenderingLibrary`（×`KismetRenderingLibrary`）、`SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST`。
- **Depth は RGBA16F 必須**：`SCS_SCENE_DEPTH` を **R32F** に撮ると全画素一定値になる不具合。
  RGBA16F に撮り `RenderingLibrary.read_render_target_raw(world, rt, False)` で R チャンネル(cm)を直接読む
  （`.hdr` 経由不要・cv2/imageio 不要）。
- **show_only_actors**：`set_editor_property` 不可。`comp.clear_show_only_components()` →
  `comp.show_only_actor_components(actor)`。非表示は `comp.hide_actor_components(actor)`。
- **Color の α**：`SCS_FINAL_COLOR_LDR` は α≈0（透明 PNG に見える）→ 不透明化が必要。
- **ビューポート厳密一致は SceneCapture 不可**：物理露出を持てない。Beauty は MRQ 一択。
- **MRQ**：一時 LevelSequence にカメラカット1フレーム → `MoviePipelinePIEExecutor`。
  単一フレームは OutputSetting の `use_custom_playback_range`+`custom_end_frame=1`、`file_name_format` に
  フレーム番号トークンを入れない。`flush_disk_writes_per_shot=True` で読み取り前に確実に書き出す。
  **多重起動防止**：`MoviePipelineQueueSubsystem.is_rendering()` が True なら起動を弾く。
  チェイン時は完了デリゲートで `_KEEP.clear()` を on_done より前に（次の executor の GC 防止）。
- **オーファン**：`importlib.reload` で `_window_ref` が None に戻り旧ウィンドウが閉じ残る → 登録簿/`gc` で一掃。
- **クリーンアップ**：SceneCapture アクターは `finally` で破棄＋`collect_garbage()`。CustomDepth/filmback/NearClip は復元。

---

## CUI / CONFIG（tkinter 無し環境・バッチ）
`capture_tool.py` の `LAUNCH_GUI=False`＋`CONFIG` 辞書で実行（Beauty(MRQ) は GUI 側オーケストレーション。
CUI はデータ系パス中心）。詳細は `capture_tool.py` 冒頭コメント参照。
