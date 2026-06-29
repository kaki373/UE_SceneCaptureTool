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
