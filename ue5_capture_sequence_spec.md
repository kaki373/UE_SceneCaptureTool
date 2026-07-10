# UE5 Scene Capture Tool - シーケンスレンダリング機能 仕様書

対象: `D:/webui/ClaudeCode/UE_capture/ue5_capture/` (capture_core.py / capture_mrq.py / capture_ui.py)
対象エンジン: Unreal Engine 5.7 (エディタ内 Python)
ステータス: 仕様確定前ドラフト (実装未着手)

本仕様書中の `unreal.*` クラス名 / プロパティ名は、特記なき限り UE 5.7 のエンジンソース
(`D:/Unreal/UE_5.7/Engine/Plugins/MovieScene/MovieRenderPipeline/` ほか) で実在を確認済み。
未確認の項目は【要検証】と明記する。

---

## 1. 概要

### 1.1 目的

現行ツールは「1 フレームの静止画キャプチャ」専用である。Beauty は Movie Render Queue (MRQ) で
一時 1 フレーム LevelSequence を生成してレンダリングし、データパス (Depth / Matte / Object ID /
behind-matte) は SceneCapture2D で同フレームを撮る。

本機能は、ユーザーが Sequencer で開いているアニメーション付き LevelSequence を対象に、

1. **PNG 連番** の書き出し
2. **MP4 動画** の書き出し
3. **Z-Depth AOV** の毎フレーム出力 (PNG 連番 / MP4 とも可。7 章)

を追加する。既存の非同期 MRQ 基盤 (PIEExecutor / on_done チェーン / 多重起動ガード /
状態復元) と、出力命名規則 (`任意名_カメラ名_素材名_NNN`) / テイク番号 / 設定 JSON を
そのまま拡張して実現する。

### 1.2 静止画フローとの構造差分

| 項目 | 静止画 (現行 render_beauty) | シーケンス (新規 render_sequence) |
|---|---|---|
| job.sequence | ツールが生成する一時 1 フレームシーケンス (`/Game/_UE5Capture_Tmp/MRQ_TempSeq`) | **ユーザーのシーケンスアセットを直接指定**。一時シーケンスは作らない |
| カメラ | ツールの Camera ドロップダウンで選択し、一時シーケンスのカメラカットに焼く | **シーケンス自身のカメラカットトラックが決める**。ツールの Camera ドロップダウンは使わない |
| フレームレンジ | `use_custom_playback_range` で [0,1) に固定 | 既定はシーケンスの Playback Range。UI で開始/終了を上書き可能 |
| fps | 固定 24 (一時シーケンスの display rate) | **シーケンスの Display Rate に従う** (`use_custom_frame_rate=False`) |
| file_name_format | フレーム番号なしの単一ファイル名 | `{frame_number}` トークンを含む連番名 (動画出力は自動でトークンが除去される) |
| 完了後処理 | 一時シーケンス削除 + 状態復元 | **シーケンスアセットには一切触らない** (削除・保存・変更いずれも禁止)。一時 AOV マテリアルの削除 + 状態復元 |
| データパス | SceneCapture2D で同フレームを出力 | **Z-Depth は MRQ 追加ポストプロセスマテリアルで毎フレーム出力** (7 章)。Matte / Object ID は対象外 |

### 1.3 v1 スコープ (決定事項)

- 出力形式は **PNG 連番と MP4** の 2 種。EXR 連番は v1 から除外する (将来拡張へ)。
- 出力パスは Beauty に加えて **Z-Depth AOV** (毎フレーム、PNG 連番 / MP4 とも可)。
  実現手段は SceneCapture2D ではなく MRQ の追加ポストプロセスマテリアル (7 章)。
  他 AOV を後から追加できる構造にする。Matte / Object ID の毎フレーム出力は対象外
  (SceneCapture2D の per-frame 実行は非現実的に遅く、現行パスは単フレーム前提の設計のため)。
- UI は既存 Capture ボタンとは独立した専用ボタン「シーケンスレンダ」とする (3 章)。
- 連番の置き場所は **テイク毎サブフォルダが既定**。UI トグルでフラット配置に切替可能 (4 章)。
- MP4 のレートは **品質プリセット** (最高 / 高 / 標準 / 軽量) から選択する (6 章)。
  v1 は Quality モード (CRF) のみで、ビットレート指定モードは対象外。
- MP4 の音声は含めない (`include_audio=False` 固定)。
- Overscan はシーケンスモードでは **% モードのみ** サポートする。実現手段は既存静止画と同じ
  `unreal.MoviePipelineCameraSetting` の `override_camera_overscan` / `overscan_percentage`
  (MRQ ネイティブ overscan。カメラアクターの filmback には触らない)。px 指定 (X/Y 独立) は
  filmback 改変が必要になるため対象外 (シーケンス途中でカメラが切り替わると破綻する)。

---

## 2. シーケンス選択仕様

### 2.1 対象シーケンスの発見

優先順位:

1. **Sequencer で現在開いているシーケンス** (既定)
   `unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()`
   (LevelSequenceEditor プラグイン。戻り値 `unreal.LevelSequence`、未オープン時 None)。
   サブシーケンスにフォーカス中でもルートを取るため `get_current_level_sequence()` を使う
   (`get_focused_level_sequence()` はフォーカス中のサブシーケンスを返すので使わない)。
2. **アセットコンボによる明示選択** (フォールバック)
   Asset Registry からプロジェクト内の LevelSequence を列挙する:
   ```python
   ar = unreal.AssetRegistryHelpers.get_asset_registry()
   assets = ar.get_assets_by_class(
       unreal.TopLevelAssetPath("/Script/LevelSequence", "LevelSequence"))
   ```
   コンボの先頭項目は `"(Sequencer で開いているシーケンス)"` とし、既定でこれを選ぶ。

シーケンス本体の読み込みは `unreal.EditorAssetLibrary.load_asset(asset_path)`。
`job.sequence` へは `unreal.SoftObjectPath("<PackagePath>.<AssetName>")` を渡す
(既存 render_beauty と同形式)。

### 2.2 カメラの決定 (優先順位の定義)

- **シーケンスモードでは、カメラは常にシーケンスのカメラカットトラックが決める。**
  MRQ はカメラカットセクションからショットリストを構築するため、ツール側でカメラを
  指定する余地はない。ツール上部の Camera ドロップダウンはシーケンスレンダには関与しない
  (解像度アスペクト算出のフォールバックにも使わない)。
- カメラカットトラックの存在確認:
  ```python
  tracks = unreal.MovieSceneSequenceExtensions.find_tracks_by_type(
      seq, unreal.MovieSceneCameraCutTrack)
  ```
  空ならエラー (10 章 E3)。v1 ではカメラカットの自動生成はしない (ユーザーのアセットを
  変更しないため)。将来拡張として「一時ラッパーシーケンス」案を 12 章に記載。
- UI の情報表示用に、先頭カメラカットのカメラ名を解決して表示する:
  ```python
  section = tracks[0].get_sections()[0]          # MovieSceneCameraCutSection
  binding_id = section.get_camera_binding_id()   # BlueprintPure 確認済み
  proxy = unreal.MovieSceneSequenceExtensions.resolve_binding_id(seq, binding_id)
  bound = unreal.SequencerTools.get_bound_objects(
      world, seq, [proxy],
      unreal.MovieSceneSequenceExtensions.get_playback_range(seq))
  ```
  `SequencerTools` (SequencerScriptingEditor, ScriptName="SequencerTools") と
  `get_playback_range()` は 5.7 ソースで確認済み。解決に失敗しても表示を
  `"(カメラカット解決不可)"` にするだけでレンダは続行する
  (【要検証】エディタ (非 PIE) コンテキストでの get_bound_objects の解決可否。
  失敗時フォールバックが表示のみなので実害なし)。

### 2.3 フレームレンジと fps

- 既定レンジ: `unreal.MovieSceneSequenceExtensions.get_playback_start(seq)` /
  `get_playback_end(seq)` (Display Rate フレーム、end は排他的)。
- fps: `unreal.MovieSceneSequenceExtensions.get_display_rate(seq)` が返す
  `unreal.FrameRate` (numerator / denominator)。MRQ 側は
  `use_custom_frame_rate=False` のままにしてシーケンスの Display Rate を使わせる。
  ffmpeg / 表示用の fps 値は `numerator/denominator` の有理数のまま扱う
  (23.976 対策。丸めない)。
- UI 上書き時: 「終了フレーム」は **含む** 意味で入力させ、内部で
  `custom_end_frame = 終了 + 1` に変換する (MRQ の CustomEndFrame は排他的。
  既存静止画コードが [0,1) = フレーム 0 のみで実証済み)。

---

## 3. UI 仕様

### 3.1 配置

既存の「Beauty (MRQ)」セクションの直後、ステータス行と Capture ボタンの手前に
新セクション「シーケンス出力」を追加する。既存の静止画フローには一切手を入れず、
専用のレンダボタンを持つ独立セクションとする (Capture ボタンとのモード統合はしない)。
ウィンドウ高さは 980 -> 1200 程度に拡大する。

### 3.2 レイアウト案

```
─────────────────────────────────────────────────────
シーケンス出力（Sequencer のアニメーションを連番 / 動画で出力）
 シーケンス: [▼ (Sequencer で開いているシーケンス)      ] [⟳]
   → SEQ_Shot010  カメラカット: CineCameraActor_1  0〜239 / 24fps
 レンジ:  (●) シーケンス設定   (○) 指定: 開始 [    ] 終了 [    ] （終了を含む）
 出力形式: ☑ PNG連番   ☑ MP4   ☑ テイク毎サブフォルダ
 パス:    ☐ Z-Depth (AOV)  ※ Near/Far/Invert は上の Z-Depth 欄の値を使用
 MP4:  エンコーダ (●) UE内蔵 H.264   (○) ffmpeg
       品質 [▼ 高 (CRF 20)      ]   ffmpeg: [                    ] [...]
       ☐ エンコード後に中間PNGを削除（ffmpeg時のみ）
 ※ 解像度 / ウォームアップ / サンプリングフレーム / Fogなし / Overscan(%) は上の設定を共用

              [ シーケンスレンダ ]      [ キャンセル ]

 status:（既存 status_var を共用）
─────────────────────────────────────────────────────
```

### 3.3 ウィジェット一覧

| ウィジェット | tk 変数 (実装名) | 型 / 値 | 既定値 | 備考 |
|---|---|---|---|---|
| シーケンス選択コンボ | `seq_asset_var` | str (asset path または先頭特殊項目) | `"(Sequencer で開いているシーケンス)"` | `ttk.Combobox` readonly。⟳ で再列挙 + 情報行更新 |
| 情報行ラベル | `seq_info_var` | str | "" | シーケンス名 / カメラカットのカメラ名 / レンジ / fps を表示。カメラカット無しなら赤字警告 |
| レンジ選択ラジオ | `seq_range_mode_var` | "sequence" / "custom" | "sequence" | |
| 開始フレーム | `seq_start_var` | str (int) | "" | custom 時のみ有効 |
| 終了フレーム | `seq_end_var` | str (int) | "" | custom 時のみ有効。**終了フレームを含む** |
| PNG連番 | `seq_png_var` | bool | True | |
| MP4 | `seq_mp4_var` | bool | True | |
| テイク毎サブフォルダ | `seq_subfolder_var` | bool | True | OFF で出力先直下にフラット配置 (4.1) |
| Z-Depth (AOV) | `seq_aov_depth_var` | bool | False | Near/Far/Invert は既存 Z-Depth 欄 (`near_var` / `far_var` / `depth_invert_var`) の値を流用 |
| MP4 エンコーダ | `seq_mp4_encoder_var` | "native" / "ffmpeg" | "native" | |
| MP4 品質プリセット | `seq_mp4_rate_var` | "best" / "high" / "standard" / "light" | "high" | 表示は 最高(CRF 17) / 高(CRF 20) / 標準(CRF 24) / 軽量(CRF 28)。`ttk.Combobox` readonly (6.2) |
| ffmpeg パス | `seq_ffmpeg_path_var` | str | "" | `[...]` は filedialog.askopenfilename |
| 中間PNG削除 | `seq_ffmpeg_delpng_var` | bool | False | ffmpeg 時のみ有効。PNG連番チェックが ON の場合は削除しない (3.5 参照) |
| シーケンスレンダボタン | `seq_render_btn` | Button ("Big.TButton") | - | レンダ中は disabled |
| キャンセルボタン | `seq_cancel_btn` | Button | - | 待機中は disabled、レンダ中のみ enabled |

共用する既存設定: 解像度 (Override W/H と「カメラのアスペクト」チェック)、
`mrq_warmup_var`、`mrq_ts_var`、`fog_off_var`、Overscan (% モードのみ、px は無視して警告)。
「カメラのアスペクト」チェックが ON の場合、アスペクトは 2.2 の手順で解決した
カメラカット先頭カメラから取得し、解決不可なら W/H 入力値をそのまま使う。

### 3.4 振る舞い

- ⟳ ボタン: コンボ再列挙 + 選択中シーケンスを load して情報行を更新
  (カメラカット有無 / レンジ / fps / カメラ名)。
- シーケンスレンダボタン押下: バリデーション (10 章) -> `_save_ui_state()` ->
  `capture_mrq.render_sequence(...)` を非同期起動 -> ボタンを disabled、
  キャンセルボタンを enabled、進捗ポーリング開始 (5.6)。
- 完了 (on_done): ボタン状態を復帰し、status に
  `"シーケンスレンダ完了: 240 フレーム -> <出力先>"` を表示。
- tkinter の作法は既存踏襲 (Tk ルート destroy 禁止 / Slate post-tick ポンプ /
  レンダ中の busy ガード)。

### 3.5 中間 PNG の扱い (ffmpeg エンコーダ時)

| PNG連番チェック | 中間PNG削除チェック | 動作 |
|---|---|---|
| ON | (無視) | PNG はユーザーの成果物。削除しない |
| OFF | OFF | PNG を中間生成し、エンコード後も残す |
| OFF | ON | PNG を中間生成し、**ffmpeg 正常終了後に削除** (サブフォルダ配置時はテイクフォルダごと削除)。ffmpeg 失敗時は残す |

削除対象は glob ではなく、`on_individual_job_work_finished_delegate` が返す
`unreal.MoviePipelineOutputData.shot_data[].render_pass_data` の `file_paths`
(実際に書かれたファイルの正確なリスト。構造体は 5.7 ソースで確認済み) を使う。
Z-Depth AOV が ON の場合、中間 PNG には depth パスの連番も含まれ、同じ規則で扱う
(depth の MP4 も Beauty と同時にエンコードしてから削除する)。

---

## 4. 出力ファイル仕様

### 4.1 命名規則 (既存規則との整合)

既存規則 `任意名_カメラ名_素材名_NNN` を維持し、フレーム番号は **ドット区切りで末尾に追加** する
(VFX 慣習の `name.####.ext` 形式)。

- ベース名: `core.out_basename(settings, "Beauty", take)` をそのまま使う。
  ただしシーケンスモードの「カメラ名」はツールのドロップダウンではなく、
  2.2 で解決したカメラカット先頭カメラのラベルを使う (解決不可時はシーケンス名で代替)。
- テイク番号 `NNN`: 既存 `core.next_take_number(output_dir)` と同じ 3 桁通し番号。

### 4.1.1 配置レイアウト (「テイク毎サブフォルダ」トグル)

**既定 = サブフォルダ ON**。UI トグル (3.3) で OFF (フラット) に切替できる。

サブフォルダ ON (既定):

| 出力 | 置き場所 | 例 (任意名 MyShot、カメラ CamA、テイク 007) |
|---|---|---|
| PNG 連番 | テイクフォルダ `<出力Dir>/MyShot_CamA_Beauty_007/` | `MyShot_CamA_Beauty_007/MyShot_CamA_Beauty_007.0000.png` 〜 `.0239.png` |
| Z-Depth 連番 | 同じテイクフォルダ | `MyShot_CamA_Beauty_007/MyShot_CamA_ZDepth_007.0000.png` (4.4 のリネーム後) |
| MP4 (連番と同時) | 同じテイクフォルダ | `MyShot_CamA_Beauty_007/MyShot_CamA_Beauty_007.mp4` |
| MP4 (単独) | `<出力Dir>` 直下 | `MyShot_CamA_Beauty_007.mp4` (単一ファイルなのでフォルダを作らない) |

サブフォルダ OFF (フラット):

| 出力 | 置き場所 | 例 |
|---|---|---|
| PNG 連番 | `<出力Dir>` 直下 | `MyShot_CamA_Beauty_007.0000.png` 〜 `.0239.png` |
| Z-Depth 連番 | 同上 | `MyShot_CamA_ZDepth_007.0000.png` 〜 |
| MP4 | 同上 | `MyShot_CamA_Beauty_007.mp4` / `MyShot_CamA_ZDepth_007.mp4` |

MRQ 側の差分は `output_directory` をテイクフォルダにするか `<出力Dir>` にするかのみで、
`file_name_format` は共通。フラット時もテイク番号 `_NNN` が全ファイルに入るため
静止画テイクと衝突しない (フレーム番号部がテイク走査に誤マッチしないことは 4.3 で確認済み)。

**サブフォルダを既定とする理由**: 静止画テイクと共用する出力フォルダに数百ファイルの
連番が平置きされると目視運用が破綻する。1 テイク = 1 フォルダなら削除もドラッグも
1 操作で済む。フラットは「連番を直接受け取りたい後段ツールがある」場合向け。

### 4.2 MRQ 側の設定値

```python
out.set_editor_property("output_directory", unreal.DirectoryPath(target_dir))  # 4.1.1 のレイアウトに従う
if aov_depth:
    # 複数レンダパス時は {render_pass} で 素材名 スロットを埋める (4.4)
    out.set_editor_property("file_name_format",
                            prefix + "_{render_pass}_" + take + ".{frame_number}")
else:
    out.set_editor_property("file_name_format", base + ".{frame_number}")
out.set_editor_property("zero_pad_frame_numbers", 4)
out.set_editor_property("frame_number_offset", 0)
```

(`prefix` = `任意名_カメラ名`、`base` = `prefix + "_Beauty_" + take`)

- `{frame_number}` はシーケンスの Display Rate フレーム番号 (Sequencer の表示と一致)。
  他に `{frame_number_rel}` (0 起点) 等もあるが、Sequencer と突き合わせられる
  `{frame_number}` を採用する。トークン名はエンジンソース
  (`MoviePipelineUtils.cpp` の `GetOutputStateFormatArgs`) で確認済み:
  `frame_number` / `frame_number_shot` / `frame_number_rel` / `frame_number_shot_rel` /
  `camera_name` / `shot_name` / `sequence_name` / `level_name` / `date` / `time` / `version` など。
- ゼロパディングは 4 桁 (`0000`)。1 万フレーム超は自動で 5 桁になる (MRQ の仕様)。
- 動画出力 (MP4) は同じ `file_name_format` を渡してよい。MRQ が
  `RemoveFrameNumberFormatStrings()` でフレーム番号トークンを除去し、末尾に残るドットも
  自動で削除する (エンジンソース `MoviePipelineVideoOutputBase.cpp` で確認済み)。
  結果 `base.mp4` の単一ファイルになる。

### 4.3 テイク番号走査の拡張

`core.next_take_number()` は現在ファイル名のみ走査する。テイクフォルダ名
(`..._Beauty_007` のようにディレクトリ名末尾が `_NNN`) も走査対象に加える:

- 既存: ファイル名に対し `_(\d{3})(?=[._])`
- 追加: ディレクトリ名に対し `_(\d{3})$`

両者の最大値 +1 を返す。なおフレーム番号 (`.0001.` のようにドット区切り 4 桁) は
既存正規表現にマッチしないことを確認済み (`_` 始まりでない / 3 桁 + 区切りの
lookahead を満たさない) ため、静止画側の走査と衝突しない。

### 4.4 複数レンダパス時の命名 (Z-Depth AOV ON)

Z-Depth AOV が ON のときはジョブが 2 レンダパス (Beauty + depth) になるため、
`file_name_format` に `{render_pass}` トークンを含めてパスごとにファイル名を分ける。
`{render_pass}` に入る値 (エンジンソース `MoviePipelineDeferredPasses.cpp` で確認済み):

- Beauty パス: `"FinalImage"` (固定。`PassIdentifier = FMoviePipelinePassIdentifier("FinalImage")`)
- 追加 PP マテリアルパス: `"FinalImage" + <パス名>`。パス名は
  `FMoviePipelinePostProcessPass.Name` (空ならマテリアル名) が使われる
  (`GetNameForPostProcessMaterial()` で確認済み)。ツールは Name="ZDepth" を設定するので
  `"FinalImageZDepth"` になる。

エンジン内部名をユーザーに見せないため、**レンダ完了後にリネーム** する
(`on_individual_job_work_finished_delegate` の `file_paths` から正確な対象リストを取得):

| レンダ直後 (MRQ が書く名前) | リネーム後 (ツールの素材名規則) |
|---|---|
| `MyShot_CamA_FinalImage_007.0000.png` | `MyShot_CamA_Beauty_007.0000.png` |
| `MyShot_CamA_FinalImageZDepth_007.0000.png` | `MyShot_CamA_ZDepth_007.0000.png` |
| `MyShot_CamA_FinalImage_007.mp4` | `MyShot_CamA_Beauty_007.mp4` |
| `MyShot_CamA_FinalImageZDepth_007.mp4` | `MyShot_CamA_ZDepth_007.mp4` |

- 置換は `_FinalImageZDepth_` -> `_ZDepth_` を先に、`_FinalImage_` -> `_Beauty_` を後に行う
  (部分文字列の包含関係のため順序が必要)。
- リネームは同一ディレクトリ内の `os.rename` で高速。失敗してもレンダ結果は有効なので
  警告表示のみで続行する (整形は cosmetic 扱い)。
- AOV OFF (単一パス) のときは従来どおり `{render_pass}` を含めず `..._Beauty_NNN` 名で
  直接出力されるため、リネームは発生しない。

MP4 がレンダパスごとに別ファイルになる根拠: `MoviePipelineVideoOutputBase.cpp` の
書き出しループは `InMergedOutputFrame->ImageOutputData` (パスごとのエントリ) を反復し、
パスごとに解決したファイル名で個別のライターを生成する (5.7 ソースで確認済み。
burn-in 等の合成パスのみ FinalImage に合成されてスキップされる)。
PNG 連番も同じ仕組みでパスごとに別連番になる。

---

## 5. MRQ 設定仕様

### 5.1 エントリポイント

`capture_mrq.py` に新規関数を追加する (render_beauty とコードを共有しつつ、
一時シーケンス系の処理を通らない別経路):

```python
def render_sequence(seq_asset_path, output_dir, width, height,
                    png=True, mp4=None,                # mp4: None or dict(encoder, crf)
                    aov_depth=None,                    # None or dict(near, far, invert)
                    subfolder=True,                    # テイク毎サブフォルダ (4.1.1)
                    range_override=None,               # None or (start, end_inclusive)
                    temporal_samples=8, warmup=32,
                    name_prefix="seq", take="001", fog_off=False,
                    overscan=0.0, on_done=None):
    """ユーザーの LevelSequence を MRQ でレンダリング (非同期)。executor を返す。
    on_done(success, output_dir, output_data) を完了時に呼ぶ。"""
```

### 5.2 ジョブ構築

既存 `_start_render()` と同じ骨格。差分のみ記す。

```python
job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
job.job_name = "UE5Capture_Sequence"
job.map = unreal.SoftObjectPath(_current_map_softpath())
job.sequence = unreal.SoftObjectPath(seq_asset_path)      # ユーザーのシーケンスを直接指定

cfg = job.get_configuration()
deferred = cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
if aov_depth:
    # 一時 Z-Depth マテリアルを追加ポストプロセスパスとして登録 (7 章)
    pp = unreal.MoviePipelinePostProcessPass()
    pp.set_editor_property("enabled", True)
    pp.set_editor_property("name", "ZDepth")          # {render_pass} = "FinalImageZDepth" (4.4)
    pp.set_editor_property("material", depth_material)  # 7.2 で生成した一時マテリアル
    deferred.set_editor_property("additional_post_process_materials", [pp])
    # disable_multisample_effects は False のまま (7.5 の含意を参照)

if png:
    fmt = cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
    fmt.set_editor_property("write_alpha", False)
if mp4 and mp4["encoder"] == "native":
    v = cfg.find_or_add_setting_by_class(unreal.MoviePipelineMP4EncoderOutput)   # 6.2 参照
    v.set_editor_property("encoding_rate_control",
                          unreal.MoviePipelineMP4EncodeRateControlMode.QUALITY)
    v.set_editor_property("constant_rate_factor", int(mp4["crf"]))
    v.set_editor_property("include_audio", False)
```

`FMoviePipelinePostProcessPass` (Python: `unreal.MoviePipelinePostProcessPass`) の
フィールド `enabled` / `name` / `material` / `high_precision_output` /
`use_lossless_compression` と、`UMoviePipelineDeferredPassBase` の
`additional_post_process_materials` (TArray) / `disable_multisample_effects` は
`MoviePipelineDeferredPasses.h` (5.7) で確認済み。

### 5.3 OutputSetting

```python
out = cfg.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
out.set_editor_property("output_directory", unreal.DirectoryPath(target_dir))
out.set_editor_property("output_resolution", unreal.IntPoint(W, H))
out.set_editor_property("file_name_format", name_format)   # 4.2 / 4.4 の規則で組む
out.set_editor_property("zero_pad_frame_numbers", 4)
out.set_editor_property("override_existing_output", True)
out.set_editor_property("flush_disk_writes_per_shot", True)
# フレームレンジ:
if range_override:
    out.set_editor_property("use_custom_playback_range", True)
    out.set_editor_property("custom_start_frame", start)
    out.set_editor_property("custom_end_frame", end_inclusive + 1)   # end は排他的
else:
    out.set_editor_property("use_custom_playback_range", False)      # Playback Range に従う
# fps: use_custom_frame_rate は触らない (False のまま = シーケンスの Display Rate)
```

`use_custom_frame_rate` / `output_frame_rate` / `use_custom_playback_range` /
`custom_start_frame` / `custom_end_frame` / `zero_pad_frame_numbers` /
`frame_number_offset` はいずれも `UMoviePipelineOutputSetting` の BlueprintReadWrite
プロパティとして 5.7 ソースで確認済み。

### 5.4 AA / GameOverride / コンソール変数

静止画と同一 (`MoviePipelineAntiAliasingSetting` の TSR + temporal_sample_count +
engine/render_warm_up_count、`MoviePipelineGameOverrideSetting` の高品質設定、
`_HQ_CONSOLE` リスト)。補足:

- `r.MotionBlurQuality 0` は維持する。temporal_samples >= 2 ならモーションブラーは
  テンポラルサンプルの蓄積で得られ、ポストプロセス MB と二重掛けになるのを防ぐ
  (MRQ の標準推奨と同じ)。
- warmup はショット開始時に 1 回だけ発生するため、長いシーケンスでもコスト増は無視できる。
- `near_clip_cm` (behind-matte 用) はシーケンスモードでは使わない。
- `fog_off` は同じ実装 (start_console_commands に `r.Fog 0` / `r.VolumetricFog 0`、
  完了時に 1 へ復元) を使う。

### 5.5 Overscan (% のみ)

```python
if overscan > 0.0:
    camset = cfg.find_or_add_setting_by_class(unreal.MoviePipelineCameraSetting)
    camset.set_editor_property("override_camera_overscan", True)
    camset.set_editor_property("overscan_percentage", float(overscan))
```
解像度は呼び出し側で W,H を (1+f) 倍して渡す (既存静止画と同じ規約)。

### 5.6 進捗表示

方式: **出力フォルダのポーリング** (既存の Slate post-tick コールバックに相乗り)。

- レンダ開始時に総フレーム数 `total = end_exclusive - start` を確定。
- tick 約 30 回に 1 回 (およそ 1 秒間隔) 出力フォルダを `os.listdir` し、
  `.NNNN.png` 形式にマッチするファイル数を数えて `done = ファイル数 // パス数`
  (AOV ON なら 2 パス) を算出、`status_var` を `"シーケンスレンダ中 37/240 (15%)"` に更新。
  ポーリング時点のファイル名は 4.4 のリネーム前 (FinalImage 系) である点に注意。
- MP4 単独 (native) のときは途中ファイルが存在しないため、経過時間表示のみ:
  `"シーケンスレンダ中… (01:23 経過)"`。
- 代替案として `unreal.MoviePipelineLibrary.get_completion_percentage(pipeline)`
  (`UMoviePipelineBlueprintLibrary`, ScriptName="MoviePipelineLibrary" 確認済み) があるが、
  実行中の `UMoviePipeline` インスタンスは PIEExecutor の `ActiveMoviePipeline` が
  Python 非公開 (BlueprintReadOnly でない UPROPERTY) のため取得経路がない。
  よってフォルダポーリングを正とする。

### 5.7 完了通知とキャンセル

- 完了: 既存と同じ `executor.on_executor_finished_delegate.add_callable(fn)`
  (引数 `(executor, success)`)。加えて
  `executor.on_individual_job_work_finished_delegate.add_callable(fn)`
  (`UMoviePipelinePIEExecutor` の BlueprintAssignable、確認済み。引数は
  `unreal.MoviePipelineOutputData`) を張り、`shot_data[].render_pass_data{}.file_paths`
  から実出力ファイルリストを得る。ffmpeg 入力・削除対象・完了メッセージに使う。
- キャンセル: `executor.cancel_all_jobs()` を呼ぶ。
  `CancelCurrentJob` / `CancelAllJobs` は `UMoviePipelineExecutorBase` の
  BlueprintCallable で、`UMoviePipelineLinearExecutorBase` (PIEExecutor の基底) に
  実装があることをソースで確認済み。
  【要検証】キャンセル後に `on_executor_finished_delegate` が success=False で
  発火するか (発火する想定で状態復元を on_finished に集約するが、発火しない場合に
  備えキャンセル操作直後にもボタン状態だけは復帰させる)。
- 多重起動ガード: 既存と同じ (`sub.is_rendering()` チェック + モジュールレベル `_KEEP` dict)。
  静止画 Capture とシーケンスレンダは同じガードを共有し、どちらかが実行中なら
  もう一方は起動を拒否する。

### 5.8 on_done チェーン

v1 のシーケンスレンダは単一 MRQ ジョブで完結する (PNG / MP4 native / Z-Depth AOV は
同一ジョブの複数出力・複数パス)。on_done 側の後処理は次の順で行う:

```
render_sequence (MRQ)
  --on_done--> 4.4 リネーム (AOV 時)
           --> ffmpeg subprocess 起動 (ffmpeg 選択時。パスごとに逐次)
           --tickポーリング--> 完了 / 中間PNG削除 / 一時マテリアル削除は on_finished 内で実施済み
```

---

## 6. MP4 エンコード仕様

### 6.1 方式比較と結論

| 方式 | 依存 | 品質/自由度 | プロジェクトへの影響 | 判定 |
|---|---|---|---|---|
| (a) `unreal.MoviePipelineMP4EncoderOutput` (UE 5.7 内蔵 H.264) | なし (エンジン標準。Win64/Mac/Linux) | H.264 / CRF 16-51 / VBR。コーデック固定 | なし | **推奨 (既定)** |
| (b) ツール側 ffmpeg 後段エンコード (PNG 連番 -> subprocess) | ffmpeg.exe (ユーザー用意、パスを設定 JSON に保持) | 自由 (libx264 preset、将来 H.265/ProRes) | なし | **オプションとして実装** |
| (c) `unreal.MoviePipelineCommandLineEncoder` (MRQ 標準の CLI エンコード) | ffmpeg + **プロジェクト設定** (`UMoviePipelineCommandLineEncoderSettings`、config=Engine のため DefaultEngine.ini の `[/Script/MovieRenderPipelineCore.MoviePipelineCommandLineEncoderSettings]` に永続化) | 自由 | **DefaultEngine.ini を書き換える** | 不採用 |

(c) を不採用とする理由: 本ツールは「プロジェクトに何も残さない」設計
(Transient 生成 / .uasset 不保存) であり、ExecutablePath / VideoCodec 等を
プロジェクト config に書き込む方式は方針違反。機能面でも (b) で同等以上のことができる。

(a) の根拠 (5.7 ソース確認済み):
`MovieRenderPipelineMP4Encoder` モジュールが `MovieRenderPipeline.uplugin` に
Runtime / PlatformAllowList [Mac, Win64, Linux] で登録済み。
`UMoviePipelineMP4EncoderOutput` は `UMoviePipelineVideoOutputBase` 派生の
UCLASS(BlueprintType) で、表示名 "H.264 MP4 [8bit]"。Windows 実装は
Media Foundation (Sink Writer) による H.264 / YUV 4:2:0 エンコード。
ソースコメント上 "Experimental" 扱いである点に留意 (問題があれば (b) へ切替可能な
二段構えにしておく)。

### 6.2 UE 内蔵エンコーダの設定値と品質プリセット

レート指定は UI の品質プリセット (決定事項)。v1 は Quality モード (CRF) のみで、
`VariableBitRate` (`average_bitrate_in_mbps` 指定) は使わない。

| プリセット ID | UI 表示 | CRF | 用途目安 |
|---|---|---|---|
| `best` | 最高 (CRF 17) | 17 | 知覚的ロスレス相当。最終納品 / アーカイブ |
| `high` | 高 (CRF 20) | 20 | **既定**。通常のプレビュー・共有 |
| `standard` | 標準 (CRF 24) | 24 | 軽めの確認用 |
| `light` | 軽量 (CRF 28) | 28 | チャット添付など最小サイズ優先 |

| プロパティ (Python 名) | 値 | 備考 |
|---|---|---|
| `encoding_rate_control` | `unreal.MoviePipelineMP4EncodeRateControlMode.QUALITY` | UENUM(BlueprintType) 確認済み。既定値も Quality |
| `constant_rate_factor` | プリセットの CRF (int32、クランプ 16-51、エンジン既定 20) | ヘッダで確認済み |
| `average_bitrate_in_mbps` / `max_bitrate_in_mbps` | 触らない (VBR モード用) | 将来拡張 |
| `encoding_profile` / `encoding_level` | 既定のまま (High / Auto) | UI 非公開 |
| `include_audio` | False 固定 | 決定事項 (1.3) |

**解像度の偶数丸め**: H.264 YUV 4:2:0 は偶数解像度が前提。MP4 出力が ON のとき
W, H を偶数へ切り下げ、丸めた場合は status に表示する
(【要検証】内蔵エンコーダが奇数解像度をどう扱うか。丸めておけば依存しない)。

### 6.3 ffmpeg 後段エンコード (オプション)

- **入力**: 同ジョブで出力した PNG 連番 (PNG 連番チェックが OFF でも中間として強制出力。3.5)。
- **起動**: エディタ Python から `subprocess.Popen` で非同期起動する。
  `subprocess.run` / `check_call` は UE のゲームスレッドをブロックするため禁止。
  Windows では `creationflags=subprocess.CREATE_NO_WINDOW` を付ける。
  stdout/stderr はテイクフォルダ横のログファイル (`<base>_ffmpeg.log`) へリダイレクトし、
  失敗時に status へ誘導を出す。
- **完了検知**: 既存 Slate tick で `proc.poll()` をポーリング。returncode 0 で成功。
- **ffmpeg の所在**: ツールは同梱しない。設定 JSON の `seq_ffmpeg_path` に絶対パスを保持し、
  UI の `[...]` で選択させる。空の場合は `PATH` 上の `ffmpeg` を試す
  (`shutil.which("ffmpeg")`)。どちらも無ければレンダ開始前にエラー (10 章 E7)。

推奨コマンドライン:

```
<ffmpeg> -hide_banner -y
  -framerate <num>/<den>              # シーケンスの Display Rate を有理数のまま
  -start_number <start_frame>         # レンジ開始フレーム (=最初の PNG の番号)
  -i "<dir>/<pass_base>.%04d.png"     # pass_base = リネーム後のパス別ベース名 (4.4)
  -c:v libx264 -preset slow
  -crf <CRF>                          # 6.2 のプリセット CRF をそのまま
  -pix_fmt yuv420p                    # 再生互換性 (4:2:0)
  -movflags +faststart
  "<dir>/<pass_base>.mp4"
```

- **ガンマ / 色**: MRQ の PNG はトーンマップ済み sRGB (ディスプレイリファード) なので、
  色変換なしの直エンコードで正しい絵になる。
  色タグ (`-color_primaries bt709 -color_trc bt709 -colorspace bt709`) は任意
  (sRGB と bt709 のガンマ差は実用上無視される慣習に従う)。
- 偶数丸めは native と同じ規則を適用する (奇数のままだと libx264 + yuv420p が失敗する)。
- **パスごとに 1 回起動する**: Z-Depth AOV ON のときは Beauty 用と ZDepth 用の 2 回、
  逐次実行する (同時起動しない。完了検知の単純化と負荷抑制のため)。

### 6.4 エンコーダ選択の指針 (README 記載用)

- 通常は UE 内蔵 (追加インストール不要・レンダと同時に 1 パスで完了)。
- ffmpeg を選ぶのは: 内蔵エンコーダで問題が出た場合 / preset・ビットレート等を
  細かく制御したい場合 / 将来の H.265 等が必要な場合。

---

## 7. AOV 出力仕様 (Z-Depth)

Z-Depth を毎フレームの AOV として、Beauty と同じジョブで PNG 連番 / MP4 に出力する。
他の AOV を後から追加できる構造にする (7.6)。

### 7.1 方式: MRQ 追加ポストプロセスマテリアル

`unreal.MoviePipelineDeferredPassBase` の `additional_post_process_materials`
(`TArray<FMoviePipelinePostProcessPass>`) に深度可視化マテリアルを登録する。
構造体フィールド (5.7 `MoviePipelineDeferredPasses.h` で確認済み):

| フィールド (Python 名) | 用途 |
|---|---|
| `enabled` | True |
| `name` | `"ZDepth"`。`{render_pass}` トークンに反映される (4.4) |
| `material` | `TSoftObjectPtr<UMaterialInterface>`。7.2 の一時マテリアル |
| `high_precision_output` | False (32bit 出力は EXR 用途。v1 の 8bit PNG/MP4 では不要) |
| `use_lossless_compression` | False (PNG は元々可逆) |

SceneCapture2D を使わないため per-frame の速度問題がなく、
MRQ の 1 回のシーンレンダに相乗りする (追加コストは PP マテリアル 1 パス分)。

### 7.2 一時深度マテリアルの動的生成

エンジン標準の `/Engine/.../MovieRenderQueue_WorldDepth` (プラグイン Content に存在確認済み)
は **非正規化の距離 (cm) をそのまま出力する** ため 8bit PNG / MP4 では白飛びして使えない。
ツールが正規化済みマテリアルを **一時アセットとしてレンダ時に生成し、完了後に削除** する
(旧一時シーケンスと同じライフサイクル: 生成 -> save -> レンダ -> 削除)。

- 置き場所: `/Game/_UE5Capture_Tmp/MRQ_ZDepthMat` (既存 `_TMP_PKG` と同じパッケージ)。
  `additional_post_process_materials` はソフト参照のため、PIE 側で解決できるよう
  `unreal.EditorAssetLibrary.save_loaded_asset()` で一度保存する (一時シーケンスと同じ理由)。
- 実装 (0-1 正規化 `clamp((SceneDepth - near) / (far - near), 0, 1)`、invert 時は
  `1 - x` で 手前=白):

```python
at = unreal.AssetToolsHelpers.get_asset_tools()
mat = at.create_asset("MRQ_ZDepthMat", "/Game/_UE5Capture_Tmp",
                      unreal.Material, unreal.MaterialFactoryNew())
mat.set_editor_property("material_domain", unreal.MaterialDomain.MD_POST_PROCESS)
mat.set_editor_property("blendable_location",
                        unreal.BlendableLocation.BL_SCENE_COLOR_AFTER_TONEMAPPING)

MEL = unreal.MaterialEditingLibrary
depth = MEL.create_material_expression(mat, unreal.MaterialExpressionSceneTexture, -800, 0)
depth.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_SCENE_DEPTH)
# 以降 Subtract(near) -> Divide(far - near) -> Clamp(0,1) -> (invert 時 OneMinus) と接続し、
# 最終ノードを connect_material_property(node, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
# で EmissiveColor へ出力する

MEL.recompile_material(mat)
unreal.EditorAssetLibrary.save_loaded_asset(mat)
```

- Near / Far / Invert は既存 Z-Depth UI (`near_var` / `far_var` / `depth_invert_var`) の
  値を **定数ノードとして焼き込む** (テイクごとに作り直すためパラメータ化は不要)。
- 列挙値の根拠 (5.7 エンジンソース確認済み):
  `EBlendableLocation::BL_SceneColorAfterTonemapping` (5.4 で BL_AfterTonemapping から改名。
  MRQ ヘッダの指示「Post Process domain + Blendable Location = After Tonemapping」に対応)、
  `ESceneTextureId::PPI_SceneDepth`、`unreal.MaterialExpressionSceneTexture.scene_texture_id`。
- 削除: render_sequence 専用 on_finished 内で
  `unreal.EditorAssetLibrary.delete_asset("/Game/_UE5Capture_Tmp/MRQ_ZDepthMat")`。
  起動失敗 (例外) 時も削除してから raise する (既存の一時シーケンスと同じ規約)。

### 7.3 8bit 書き出し時のガンマ【要検証】

PP マテリアルの emissive 出力 (リニア 0-1) が 8bit PNG / MP4 に書かれる際、
sRGB エンコードが掛かるかはソース未確認。実装タスク (11 章) で既知距離のシーンを使い
実測する。掛かっている場合は一時マテリアル末尾に `Power(x, 2.2)` ノードを入れて相殺し、
既存 SceneCapture 版 Z-Depth (リニア値の直書き) と画素値互換にする。

### 7.4 レンダパス命名と per-pass ファイル出力

4.4 で規定 (render_pass = "FinalImage" / "FinalImageZDepth"、完了後リネーム、
PNG 連番 / MP4 ともパスごとに別ファイル。いずれも 5.7 ソース確認済み)。

### 7.5 AA との相互作用 (確認済み) と v1 の選択

MRQ ヘッダには「追加 PP マテリアルはピクセルを一致させるために
`bDisableMultisampleEffects` が必要 (DoF / MotionBlur / TAA を無効化)」とある。
ソースを追うと `IsAntiAliasingSupported()` が `!bDisableMultisampleEffects` を返し、
`MoviePipelineImagePassBase.cpp` が `View->AntiAliasingMethod = AAM_None` に落とす。
つまりこのフラグは **DeferredPassBase 全体 = Beauty パスにも効く** (Beauty の TSR /
DoF / MB が失われる)。

v1 の決定: `disable_multisample_effects = False` のまま (既定値) とする。

- Beauty の品質を落とさないことを優先する。
- 深度パスは temporal samples でジッタ蓄積され、エッジがアンチエイリアスされた
  正規化深度になる (連続値として妥当)。DoF / MB が強く掛かる画素では Beauty と
  深度の像が厳密には一致しない。この制限は README に明記する。
- ピクセル一致が必要な用途向けの「2 ジョブ分割 (Beauty ジョブ + データジョブ
  disable_multisample_effects=True / temporal_samples=1)」は将来拡張 (12 章)。

### 7.6 AOV の拡張構造

AOV はコード上テーブルで定義し、チェックボックス追加だけで増やせる形にする:

```python
AOV_DEFS = {
    "ZDepth": dict(label="Z-Depth", build=_build_depth_material),   # 7.2
    # 将来: "WorldNormal": dict(label="Normal", build=_build_normal_material), ...
}
```

`build` は一時マテリアルを生成して返す関数。ジョブ構築側は有効な AOV を列挙して
`FMoviePipelinePostProcessPass` を積むだけ (name=AOV id -> {render_pass} ->
リネーム表も同テーブルから引く)。

### 7.7 Matte / Object ID (対象外の明記)

Matte / Object ID の毎フレーム出力は引き続き対象外。現行実装は「エディタ選択 /
アクターリスト」前提の単フレーム設計であり、SceneCapture2D の per-frame 実行は
非現実的に遅い。将来は `unreal.MoviePipelineObjectIdRenderPass`
(MoviePipelineMaskRenderPass プラグイン、Cryptomatte 形式 EXR。クラス名確認済み) を
別系統として追加する (12 章)。UI のシーケンスセクションには既存 Passes 系チェックを
表示せず、「Matte / Object ID は静止画専用」と注記する。

---

## 8. エディタ状態の安全性

既存ルールを全て踏襲し、シーケンス固有の規則を追加する。

1. **ユーザーのシーケンスアセットは読み取り専用**。
   - 変更しない / 保存しない (`save_loaded_asset` を呼ばない) / 削除しない。
   - 静止画フローの `_delete_temp_sequence()` はシーケンスモードの完了処理から
     絶対に呼ばれないよう、経路を分離する (render_sequence 専用の on_finished を持つ)。
2. **一時 AOV マテリアル** (`/Game/_UE5Capture_Tmp/MRQ_ZDepthMat`) は旧一時シーケンスと
   同じライフサイクルで管理する: 生成 -> save -> レンダ -> on_finished で削除。
   起動失敗 (例外) 時も削除してから raise。ツール起動時に残骸があれば掃除する。
3. コンソール変数の復元: 既存と同じ。`fog_off` 時は on_finished で `r.Fog 1` /
   `r.VolumetricFog 1` に戻す。near-clip はシーケンスモードでは変更しない。
4. hidden actors / CustomDepth / filmback の一時変更: シーケンスモード v1 では
   一切行わない (復元対象そのものを作らない)。
5. `_KEEP` ガード: 起動失敗時 (例外) は `_KEEP.clear()` して次回レンダを塞がない。
   成功完了時は on_done 呼び出し前に clear (既存規約。on_done がチェーンで次の処理を
   起動できるように)。
6. 未保存 (dirty) のシーケンス: `unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()`
   (BlueprintCallable 確認済み) に対象パッケージが含まれる場合、警告を status に出して
   **続行** する。PIE はメモリ上のアセット状態を評価するため未保存でもレンダ自体は
   最新状態で行われる想定【要検証】。再現性 (後で同じ絵が出るか) の観点から警告のみ行う。
7. レンダ中のシーケンス操作: PIE 中の Sequencer 編集はユーザー責任とし、ツールは
   関与しない (MRQ 標準の挙動に従う)。

---

## 9. 設定永続化 (JSON)

既存 `<Project>/Saved/UE5Capture_ui_settings.json` に以下のキーを追加する
(`_save_ui_state` / `_load_ui_state` に対で追記):

| キー | 型 | 既定値 | 対応ウィジェット |
|---|---|---|---|
| `seq_asset` | str | "" ( = 開いているシーケンス) | シーケンス選択コンボ (アセットパス、特殊項目時は "") |
| `seq_range_mode` | str | "sequence" | レンジ選択ラジオ |
| `seq_start` | str | "" | 開始フレーム |
| `seq_end` | str | "" | 終了フレーム |
| `seq_png` | bool | true | PNG連番 |
| `seq_mp4` | bool | true | MP4 |
| `seq_subfolder` | bool | true | テイク毎サブフォルダ |
| `seq_aov_depth` | bool | false | Z-Depth (AOV) |
| `seq_mp4_encoder` | str | "native" | MP4 エンコーダラジオ |
| `seq_mp4_rate` | str | "high" | MP4 品質プリセット ("best" / "high" / "standard" / "light"。6.2) |
| `seq_ffmpeg_path` | str | "" | ffmpeg パス |
| `seq_ffmpeg_delpng` | bool | false | 中間PNG削除 |

読み込み時の後方互換: キー欠落は既定値 (既存 `_setvar` パターンをそのまま使う)。
Z-Depth AOV の Near / Far / Invert は既存キー `near` / `far` / `depth_invert` を
そのまま流用する (新キーは作らない)。

---

## 10. エラー処理

レンダ開始前のバリデーションで検出し、status_var へ日本語メッセージを表示して中断する。
(レンダ開始後に判明するものは on_finished / ポーリングで表示。)

| # | 条件 | 検出方法 | メッセージ / 動作 |
|---|---|---|---|
| E1 | Sequencer で何も開いていない (特殊項目選択時) | `get_current_level_sequence()` が None | 「Sequencer でシーケンスを開くか、コンボでアセットを選択してください」 |
| E2 | 選択アセットがロードできない | `load_asset` が None | 「シーケンスを読み込めません: <path>」 |
| E3 | カメラカットトラックが無い | `find_tracks_by_type(seq, unreal.MovieSceneCameraCutTrack)` が空、またはセクション 0 個 | 「シーケンスにカメラカットがありません。Sequencer でカメラカットトラックを追加してください」(v1 は自動生成しない) |
| E4 | レンジ不正 | custom 時: start > end / 数値でない。sequence 時: playback range 長 0 | 「フレームレンジが不正です (開始 <= 終了)」 |
| E5 | 開始フレームが負 | resolved start < 0 | 「負の開始フレームは未対応です。レンジ指定で 0 以上にしてください」(ffmpeg の `%04d` とファイル名衝突のため v1 制限) |
| E6 | 出力形式が 1 つも選ばれていない | png/mp4 とも False | 「出力形式 (PNG連番 / MP4) を選択してください」(AOV は出力形式ではなくパスなので単独では不可) |
| E7 | ffmpeg 選択時に ffmpeg が見つからない | パス空かつ `shutil.which` 失敗 / パスのファイル不存在 | 「ffmpeg が見つかりません。パスを設定してください」(**レンダ開始前**に検出。レンダ後に気づかせない) |
| E8 | Z-Depth AOV で Near/Far が不正 | far <= near / 数値でない (既存 Z-Depth 欄の値) | 「Z-Depth の Far は Near より大きくしてください」(既存バリデーションと同文言) |
| E9 | MRQ 実行中 | `sub.is_rendering()` or `_KEEP` 非空 | 既存文言 (多重起動防止) |
| E10 | シーケンスが未保存 (dirty) | `get_dirty_content_packages()` に含まれる | **警告のみ**「シーケンスに未保存の変更があります (そのままレンダします)」 |
| E11 | 出力先未指定 / 作成失敗 | 既存と同じ | 既存文言 |
| E12 | MP4 で奇数解像度 | W or H が奇数 | 偶数へ切り下げて続行 + status に「解像度を <W>x<H> に調整しました (MP4)」 |
| E13 | レンダ失敗 | on_finished success=False | 「シーケンスレンダ失敗。Output Log を確認してください」+ 状態復元 + 一時マテリアル削除 |
| E14 | キャンセル | キャンセルボタン | 「キャンセルしました (出力は途中フレームまで)」。途中ファイルは削除しない。一時マテリアルは削除 |
| E15 | ffmpeg 失敗 | returncode != 0 | 「MP4 エンコード失敗。<pass_base>_ffmpeg.log を確認してください」。中間 PNG は削除しない |
| E16 | 一時 AOV マテリアル生成失敗 | create_asset / recompile が None・例外 | 「Z-Depth 用マテリアルの生成に失敗しました」。レンダ開始前に中断し、部分生成物を削除、`_KEEP` を汚さない |

---

## 11. 実装タスク分割 (推奨実装順)

各タスクは独立に動作確認できる小さい単位にする。

1. **core: テイク走査拡張** - `next_take_number()` にディレクトリ名 `_NNN$` の走査を追加。
   既存静止画テイクとの相互干渉が無いことをローカルフォルダで確認。
2. **mrq: シーケンス情報取得ヘルパ** - `get_sequence_info(seq)` を新設
   (playback start/end / display rate / カメラカット有無 / カメラ名解決)。
   Output Log の Python REPL から単体で呼んで戻り値を確認。
3. **mrq: render_sequence() 骨格 (PNG 連番のみ)** - ジョブ構築 / レンジ / 命名
   (サブフォルダ / フラット両レイアウト) / 多重起動ガード / on_finished 分離
   (一時シーケンス削除を通らないこと)。
   短いテストシーケンス (24 フレーム程度) で連番出力とフレーム番号一致を確認。
4. **mrq: 内蔵 MP4 出力の追加** - 同一ジョブへの複数出力設定。品質プリセット -> CRF 反映を
   実ファイルで確認 (ファイルサイズ差)。偶数丸め。MP4 単独時の出力先フラット化。
5. **mrq: 一時 Z-Depth マテリアル生成** (7.2) - `create_material_expression` による
   ノード構築 / save / 削除のライフサイクル。**MRQ 配線前に単体検証する**:
   生成したマテリアルを SceneCapture2D の `post_process_settings` に blendable として
   一時登録し (`weighted_blendables`)、`read_render_target_raw` で既知距離の画素値が
   (SceneDepth - near)/(far - near) に一致することを確認する。ここで 7.3 の
   sRGB エンコード有無も実測し、必要なら補正ノードを確定する。
6. **mrq: AOV の MRQ 配線** (7.1 / 4.4) - `additional_post_process_materials` 登録 /
   `{render_pass}` 命名 / 完了後リネーム / PNG と MP4 がパス別に出ることを E2E 確認。
   Beauty 側の品質 (TSR / DoF) が変化しないことも確認 (7.5)。
7. **ui: シーケンスセクション** - 3 章のウィジェット群 (サブフォルダトグル / 品質プリセット /
   AOV チェック含む) + 設定 JSON (9 章) + バリデーション (10 章 E1-E12, E16)。
   レンダボタンから 3.-6. を起動。
8. **ui: 進捗ポーリングとキャンセル** - tick 相乗りのフォルダポーリング (5.6) /
   `cancel_all_jobs()` (5.7)。キャンセル時の状態復元 (一時マテリアル削除含む) と
   E14 表示を確認 (このタスクで【要検証】の on_finished 発火有無を実測して仕様を確定する)。
9. **ffmpeg 後段エンコード** - subprocess.Popen + tick ポーリング + ログファイル +
   パスごとの逐次エンコード + 中間 PNG 削除規則 (3.5)。ffmpeg 不在時 E7 が開始前に
   出ることを確認。
10. **ドキュメント更新** - README.md (機能一覧 / 依存関係に ffmpeg 任意を追記 /
    7.5 の深度とBeautyの画素一致制限) と ue5_capture/README.md (出力仕様)。

---

## 12. 将来拡張

- **EXR 連番** (v1 から除外した項目): `unreal.MoviePipelineImageSequenceOutput_EXR`
  (プロパティ `compression` (`unreal.EXRCompressionFormat`) / `multilayer` / `multipart`、
  5.7 ソース確認済み) によるリニア HDR 連番。AOV と組み合わせれば
  `high_precision_output=True` の 32bit 深度も出せる。
- **カメラカット無しシーケンスの救済**: 一時ラッパーシーケンス (SubTrack で対象シーケンスを
  参照 + ツールの Camera ドロップダウンで選んだカメラのカメラカットトラックを持つ) を
  Transient に生成してレンダする。ユーザーアセット無変更のまま任意カメラでレンダできる。
- **AOV の追加** (7.6 のテーブルに追記するだけ): WorldNormal / MotionVectors 等。
  正規化不要なものはエンジン標準マテリアル
  (`/Engine/Plugins/MovieScene/MovieRenderPipeline/Content/Materials/` の
  MovieRenderQueue_WorldNormal ほか、存在確認済み) の流用も検討。
- **ピクセル一致データパス (2 ジョブ分割)**: Beauty ジョブとは別に
  `disable_multisample_effects=True` / `temporal_samples=1` のデータジョブを
  on_done チェーンで回し、DoF / MB / TAA の影響を受けない深度を出す (7.5 の制限の解消)。
- **Object ID (Cryptomatte)**: `unreal.MoviePipelineObjectIdRenderPass`
  (MoviePipelineMaskRenderPass プラグイン) を別系統として追加する。
- **ffmpeg プリセット拡張**: H.265 (libx265) / ProRes (prores_ks) / アルファ付き動画
  (qtrle / prores 4444)。設定 JSON にプリセット名を持たせる。
- **MP4 ビットレート指定モード**: 内蔵エンコーダの `VariableBitRate` +
  `average_bitrate_in_mbps` の UI 公開 (v1 は Quality モードのみ)。
- **音声**: 内蔵 MP4 の `include_audio=True` 化と、シーケンスのオーディオトラック検証。
- **per-shot 出力**: `{shot_name}` トークンとショット単位サブフォルダ
  (複数カメラカット / ショットトラック構成のシーケンス向け)。
- **進捗の精密化**: MRQ Graph 移行時の progress API 再調査
  (現行は ActiveMoviePipeline が Python 非公開のためフォルダポーリング)。
- **静止画 UI との統合**: モードラジオで Capture ボタンを共通化し、静止画専用 UI を
  シーケンスモード時にグレーアウトする (v1 は独立ボタンで安全側に倒す)。
