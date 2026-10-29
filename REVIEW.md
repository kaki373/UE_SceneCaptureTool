# コードレビュー（2026-10-29・capture-dev c98ea4e 時点のコードに対して）

対象: `ue5_capture/`（capture_core.py / capture_mrq.py / capture_ui.py / capture_tool.py /
capture_menu.py / startup_menu.py）。読み取りレビュー 3 本（ファイル別）＋ UE5.7/5.8 の
Python リフレクション全数比較＋エンジン同梱 Python での実測を根拠にしている。
`ue5-scene-capture` スキルに「意図的な回避策」として載っているもの（RGBA32F/16F 退避、
cvars 配列、ステンシル 250 予約、TimeWarp 固定レート 0、Guid の export_text 比較、
PIE ウォッチャ等）は指摘から外した。

凡例 — **CONFIRMED**: コードを追って確定（一部は実測あり） / **PLAUSIBLE**: 実機での確認が要る。
**状態** — 済: 本レビュー後のコミットで修正 / 要判断: 挙動が変わるのでユーザー判断待ち /
実機: ライブエディタでしか検証できない / 保留: 低優先で未着手。

---

## 1. 重要度順の所見

### Critical（誤った出力・データ消失・シーン改変が残る）

| ID | 場所 | 内容 | 根拠 | 状態 |
|---|---|---|---|---|
| C-1 | capture_core `_write_exr_gray` | Depth の EXR float が **half** で書かれ、655m 超と空（1e8）が `inf`、近距離も量子化される。RGBA32F で撮った意味が書き出しで失われていた | CONFIRMED（実測: `[123.456, 50000, 70000, 1e8]` → `[123.4375, 49984, inf, inf]`） | 済（`IO_FLAGS.EXR_FLOAT` / OpenEXR は FLOAT） |
| C-2 | capture_mrq `render_beauty` / `render_sequence` | シーン変更（非表示・マットモード・バッキング材・空マット・PIE ウォッチャ登録）が巻き戻し用 try の**外**にあり、途中の例外（UDS 派生 BP で `_set_sky_matte_mode` が失敗等）で板が隠れたまま残る。漏れた PIE ウォッチャは次ジョブでも隠し続ける | CONFIRMED | 済（セットアップ全体を try 内へ・段階的 saved） |
| C-3 | capture_ui `_on_mrq` | 静止画 Depth「自動」が 16bit PNG / EXR で効かない。`_collect_settings()` が Near/Far を読んだ**後**に実測していた（欄には実測値が出るのに出力は旧値で正規化） | CONFIRMED | 済（実測を `_collect_settings()` の前へ） |
| C-4 | capture_ui `_sequence_camera_at` | frame を無視して常に先頭カットのカメラ（スポーナブル時は配置カメラ）を返す。複数カットの Behind / MatteSil / Depth 自動レンジ / カメラアスペクトが 1 カット目基準になる | CONFIRMED | 済（frame を含むカメラカットセクションの binding を `get_bound_objects` で解決・失敗時は警告ログ付きフォールバック） |
| C-5 | capture_ui `_after_encode` | ffmpeg が失敗しても「連番チェック無し」素材の PNG を削除 → MP4 も PNG も残らない（データ消失） | CONFIRMED | 済（失敗時は削除せず作業フォルダを残す） |
| C-6 | capture_ui `_on_seq_render` | MP4 出力時に奇数解像度を偶数化していない（libx264/yuv420p が拒否 → C-5 経由で消失） | CONFIRMED | 済（偶数化＋注記、ffmpeg 側にも pad フィルタ） |
| C-7 | capture_ui `_on_mrq` / on_done チェーン | 例外 1 つで filmback（Overscan）・PropagateAlpha・一時マテリアルの復元が丸ごと飛ぶ（`capture_mrq._on_finished` が on_done の例外を握りつぶす） | CONFIRMED（発火条件は PLAUSIBLE） | 済（復元フック `_restore_hooks` と `_run_guarded`/`_finalize` の finally で必ず復元） |

### High

| ID | 場所 | 内容 | 根拠 | 状態 |
|---|---|---|---|---|
| H-1 | capture_mrq hidden_actors | 復元が元状態を見ずに一律 `hidden=False`。元々 Hidden in Game の板がレンダ後に表示される | CONFIRMED | 済（非表示でない物だけ隠して記録） |
| H-2 | capture_core `restore_matte_materials` | 元材のロード失敗で `set_material(None)`（WorldGrid 化）＋退避タグ消失。Transient MID が元材の場合も同じ | CONFIRMED | 済（ロード失敗はスキップ＋警告、Transient は退避時に警告） |
| H-3 | capture_core 静止画 ObjectID / Matte / Behind 合成 | 600m しきい値の RGBA16F 経路が残存（映像側で修正済みの「左右で前後に割れる」バグが静止画に残る） | CONFIRMED | 済（共通ヘルパで RGBA32F＋定数値時のみ 16F 退避） |
| H-4 | capture_core `render_matte_sil_sequence` | 板がフレームアウトしたフレームの show-only 深度（全画素=空）を「R32F 定数値バグ」と誤判定し、以降全フレーム 16F 固定 | CONFIRMED | 済（全画素が空のときは判定しない） |
| H-5 | capture_core `find_ffmpeg` | 個人 Python の glob しか探さず、UE 同梱 Python に入れた imageio_ffmpeg を見ない。配布先で MP4・EXR 読み（雲マット系）が全滅 | CONFIRMED（実測） | 済（`imageio_ffmpeg.get_ffmpeg_exe()` を最優先） |
| H-6 | capture_core `_spawn_capture` | Spawn 後の例外で SceneCapture2D がレベルに残る。transient でないためレベルが dirty | CONFIRMED / PLAUSIBLE | 済（transient=True・例外時に破棄） |
| H-7 | capture_core フレームループ | RenderTarget を解放せず GC 任せ（HD/aa2/89f で理論上 23GB） | PLAUSIBLE | 済（読み出し直後に release、8 フレーム毎に GC。GC 間隔は 2〜3 フレームのスモークでは未実走） |
| H-8 | capture_core `_write_png_u16_gray` | `fromarray(a, "I;16")` は Pillow 13 で削除（新規 PC は Pillow 13 が入る） | CONFIRMED（5.8 スモークで `Invalid mode for data type` を再現） | 済（mode 指定なし。5.7/5.8 でバイト一致を確認） |
| H-9 | capture_mrq `_start_pie_actor_hider` | 照合キーが「クラス名＋丸め位置」のみ。原点 pivot のインポート群が巻き添えで消える | PLAUSIBLE | 済（メッシュパス＋バウンズを追加） |
| H-10 | capture_mrq `render_beauty` | スポーナブルカメラを `add_possessable` で束縛しており、PIE で解決されず既定視点になる可能性 | PLAUSIBLE | 実機（警告ログのみ追加） |
| H-11 | capture_mrq EXR | 追加 PP パスがある静止画で `multilayer` が True のまま → `{render_pass}` が空になり `Beauty.exr` が出ない見込み | PLAUSIBLE | 済（PP パスがあれば multilayer=False） |
| H-12 | capture_ui 多重実行 | busy ガードが無く、ハンドラ内の `root.update()` が溜まったクリックを発火。二度押しで共有一時マテリアルが削除される。チェーンの合間・ffmpeg 中もガード無し | PLAUSIBLE | 済（`unreal._ue5capture_busy`＋ボタン無効化、`_final`/`_finalize` まで維持。パネル再構築ガードも参照） |
| H-13 | capture_ui 完了報告 | Behind / SkyMatte / データパス / 合成の失敗を「完了」と報告 | CONFIRMED | 済（注記を追加） |
| H-14 | capture_ui 映像 BehindPlate | プレート失敗でメイン素材の後処理まで放棄 | CONFIRMED | 済（`_bp_done` で注記して続行） |
| H-15 | capture_ui 映像 | 雲マット / Behind 失敗後も `wants` が残り、ffmpeg が存在しない連番で失敗 → C-5 連鎖 | CONFIRMED | 済 |
| H-16 | capture_ui / capture_mrq JPG | Beauty 形式 JPG のとき Depth(8bit)/Normal/Matteの前 が無言で欠落（PP パスが .jpg で出るのに .png を探す） | CONFIRMED | 済（JPG でも内部 PNG を併用） |
| H-17 | capture_core `next_take_number` | `.jpg` を走査せず、JPG だけの連続撮りで前テイクを上書き | CONFIRMED（実測） | 済 |
| H-18 | capture_ui マット対象の自動適用/復元 | 起動時はラベルで再解決して無効化するが、削除時は生パスで復元するため戻らない | CONFIRMED | 済（復元もラベルで解決） |

### Medium

| ID | 場所 | 内容 | 根拠 | 状態 |
|---|---|---|---|---|
| M-1 | capture_mrq | Capture のたびにエディタ共有の MRQ キューを全削除（ユーザーの未保存ジョブが消える） | CONFIRMED | 済（退避して完了時に復元） |
| M-2 | capture_core / capture_mrq | `r.SetNearClipPlane` の戻し先が 10cm 固定（プロジェクト設定を無視） | CONFIRMED | 済（`default_near_clip_cm()` でエンジン設定値へ） |
| M-3 | capture_mrq `_on_finished` | `restore_scene()` の例外で `_KEEP` が残りツールが使えなくなる | CONFIRMED（低確率） | 済（try/finally＋メニュー「強制リセット」） |
| M-4 | capture_mrq 一時シーケンス | `delete_asset` の失敗を無視して同名 create_asset | PLAUSIBLE | 済（GC 後に再試行・既存再利用） |
| M-5 | capture_mrq バッキング材 | 復元で既定スロットが override スロットに変わる | PLAUSIBLE | 済（`override_materials` 配列ごと退避） |
| M-6 | capture_mrq UDS 判定 | クラス名の完全一致（派生 BP で失敗） | CONFIRMED | 済（部分一致） |
| M-7 | capture_mrq / capture_core | `write_alpha` / AA / TextureStreaming / 接続結果の失敗を `except: pass` で捨てる | CONFIRMED | 済（警告・雲マットの write_alpha は raise） |
| M-8 | capture_core `next_take_number` | カメラ名の `_NNN_` を拾って番号が飛ぶ（無害）、1000 テイク目以降は上書き | CONFIRMED（実測） | 保留（.jpg 追加のみ。パターン変更は影響範囲が広い） |
| M-9 | capture_core ObjectID | マニフェストの色と画素の色が 40 色中 31 色で 1 ずれる（切り捨て vs 丸め） | CONFIRMED（実測） | 済 |
| M-10 | capture_core MatteSil / BackingT | 終端を含めて処理（UI は排他終端を渡す）→ 1 フレーム余計に撮る | CONFIRMED | 済（排他に統一） |
| M-11 | capture_core `encode_mp4_cmd` | 奇数解像度・欠番の対策なし（欠番で MP4 が黙って短くなる） | CONFIRMED | 済（pad フィルタ。欠番検査は保留） |
| M-12 | capture_core `_resolve_target_actors` | リストが空だとエディタ選択にフォールバック → ビューポートで選択中の無関係アクターが Beauty から隠される | CONFIRMED | **要判断**（仕様として「空ならエディタ選択」を残すか） |
| M-13 | capture_core 走査 | `get_all_level_actors()` は LevelInstance の中身を返さない（`level_volumetrics` / `volumetrics_nearer_than` / `_resolve_target_actors`） | CONFIRMED | **要判断**（GameplayStatics 統一は対象の解決範囲が変わる） |
| M-14 | capture_core `get_or_create_matteboard_material` | 呼ばれるたびに式再構築・再コンパイル・保存（SVN 作業コピーが常に変更あり） | CONFIRMED | 済（接続済みなら再利用） |
| M-15 | capture_core `_export_rt` | 一時ファイル名に `id(rt)` を使い回し・削除しない → 古い画像を読む可能性 | PLAUSIBLE | 済（uuid＋前後で削除） |
| M-16 | capture_core `compose_visible_cloud_h5` | 先頭が空だけのフレームだと生成されず欠番、失敗フレームの入力も削除 | PLAUSIBLE | 済（コンパイル確認のみ・雲合成は未実走） |
| M-17 | capture_ui | レンダ完了後にライブのウィジェット値（Beauty チェック・カメラ・Fog）を読み直す | CONFIRMED | 済（クリック時に確定） |
| M-18 | capture_ui 静止画 | 追加ジョブ 1 本の起動失敗でキューの残りを捨てる | CONFIRMED | 済（注記して続行） |
| M-19 | capture_ui 露出固定 | `unreal._ue5capture_extra_cvars` がレンダ後も残り、他スクリプトの capture_mrq 呼び出しに合流 | CONFIRMED | 済（完了時に解除） |
| M-20 | capture_ui カメラ同定 | ラベルで同定（重複ラベル・選択カメラ消失時に黙って先頭カメラ） | CONFIRMED | 保留（表示に注記あり。パス化は UI 変更） |
| M-21 | capture_ui 画像タブ解像度 | ラジオ「指定」が MRQ に効かず常に `mrq_camasp` で高さを上書き | CONFIRMED | **要判断**（「指定」で H をそのまま使う挙動に変えるか） |
| M-22 | capture_ui 入力検証 | W/H=0・TS/warmup=0・Far≤Near がそのまま通る | CONFIRMED | 済（最低限: W/H≥16、TS/warmup≥1、Far≤Near は既定値＋注記） |
| M-23 | capture_ui `askdirectory` | `parent` 未指定で別ツールの Tk ルートが親になり固まって見える | PLAUSIBLE | 済 |
| M-24 | capture_ui 映像「Matte 対象を隠す」 | ON で対象が無いとレンダ全体を拒否 | CONFIRMED | 済（注記して続行） |
| M-25 | capture_ui 映像 一時マテリアル | 生成途中の失敗で作成済み分が残る | CONFIRMED | 済 |

### Low（抜粋。残りは scratch のレビュー報告を参照）

- capture_mrq: 29.97fps の凍結フレームの丸め誤差（済: 有理数で計算）、黒バッキング時に強度 0 のライト 3 灯を spawn（済）、`startup_menu.py` の `r'%s'` 埋め込み（済: `%r`）、`capture_menu.py` の busy 判定が例外時に「非 busy」へ倒れる（済）。
- capture_core: `_read_rt_raw_r` のサンプル不足を `np.resize` で埋める（済: raise）、ffmpeg の `subprocess.run` に timeout 無し（済）、モジュール直下の `assert`（済: 関数化）、`AtmosphericFog` という存在しない ShowFlag（済: `Atmosphere` を追加。**挙動変化**: SceneCapture 経路の fog_off で大気も本当に OFF になる＝データパスの空の見え方が変わる。MRQ 経路は従来どおり）。
- capture_ui: 設定 JSON の非アトミック書き込み・dict でない JSON でクラッシュ（済）、`"MRQ 失敗: " + od` がパスを表示（済）、CRF の clamp（保留）、L3 シーケンス未オープン時の高さ欄（保留）。
- 一時シーケンス / MatteBoard 材 / Tmp PP 材を `.uasset` として保存しており、spec の「.uasset 不保存」と食い違う（保留。docstring は実態に合わせて修正）。

---

## 2. ユーザー判断が要る項目（挙動が変わるため未適用）

1. **M-12 マット対象リストが空のときエディタ選択を使う** — 「Matteの前/奥」をチェックしたままリストが空だと、ビューポートで選択中のアクターが隠される。選択フォールバックを「明示したときだけ」に限定するか。
2. **M-13 アクター走査の統一** — `get_all_level_actors()` → `GameplayStatics.get_all_actors_of_class()` にすると LevelInstance 内の雲・対象も拾えるようになるが、対象の解決範囲（ラベル一致の重複等）が変わる。
3. **M-21 画像タブの解像度ラジオ「指定」** — 現状は常にカメラアスペクトで高さを上書きする。「指定」なら入力の H をそのまま使う挙動にするか（UI の意味どおりにするなら変更）。
4. **要再検討の設計** — 一時アセット（`/Game/_UE5Capture_Tmp`、MatteBoard 材）を Content 配下に保存する方式（SVN 管理下では差分になる）。`/Temp` パッケージ化や保存抑止は要検証。
5. **Install.bat の `--yes` が他エンジンにもライブラリを入れる** / **Pillow の上限固定**（`Pillow<14` にするか。ツールは Pillow 13 対応済み）。

## 3. ライブエディタでしか検証できない項目

- 5.8 での MRQ PIE レンダ完走と `_FinalImage*` リネーム（ジョブ構成モード Preset 固定の実効）。
- 5.8 の `is_rendering()` がレンダ中に False を返す件（別案件で観測）。ガードは `_KEEP` と UI の busy フラグで二重化済みだが、ツール外（MRQ ウィンドウ）から始めたレンダは検出できない。
- H-10 スポーナブルカメラの PIE 束縛。
- 動くカメラでの Beauty と SceneCapture マスクの画素整合（5.8 で `MovieSceneSequenceTransform` の Clamp 判定が変更）。
- 5.7 用回避策（R32F 定数値バグ・holdout α）が 5.8 でも必要か。
- 新機能: 映像タブの 16bit/EXR Depth（フレーム毎 SceneCapture）と EXR/JPG 連番の後始末、共通対象カード、busy ガード（headless スモークではウィンドウ生成・設定往復・ガード動作まで確認済み）。

## 4. UE バージョン依存の総括

- 5.7.4 と 5.8.2 のリフレクション全数比較（`unreal.*` 129 件・メソッド 362 件・プロパティ 93 件）で、ツールが使う API に改名・削除・`UE_DEPRECATED(5.8)` は無し。
- 5.8 で増えた MRQ の構成モード（Preset / Graph / Basic）は `ensure_preset_config_mode()` で Preset に固定。
- 5.4 から非推奨で残存: `LevelSequenceEditorBlueprintLibrary.get_current_time / set_current_time`、`EditorLevelLibrary.get_game_world`（フォールバックのみ）。置き換え先は `get/set_global_position`。
- Movie Render Graph は未使用。旧 MRQ が廃止された場合の移行手順は scratch の `review_mrq.md` に記載（ジョブ構築を 1 関数に寄せ、`FinalImage` 前提のリネーム/トリムを出力名マップ化するのが先）。
- 効いていない cvar: `r.LocalExposure.HighlightContrastScale` / `ShadowContrastScale` / `r.Lumen.Reflections.Quality`（5.7/5.8 とも未定義）。

## 5. サイト固有値

- 実行に影響するものは `find_ffmpeg` の個人 Python パス（H-5・済）と `/Game/_UE5Capture_Tmp`（意図的）のみ。
- `D:/webui/...` の記載（capture_menu.py / README / spec）は配布準備で汎用表記に置換済み。
- AV024 / AV003A / UDS 等はコメント内の計測履歴で、挙動に影響しない。

## 6. 検証の実績（headless・捨てプロジェクト）

- UE 5.7.4 / 5.8.2 の `UnrealEditor-Cmd -run=pythonscript` で、モジュール import・一時 PP 材の生成/削除・
  SceneCapture の Depth/Matte/ObjectID 出力・MRQ ジョブ構成（executor 起動だけ差し替え）・
  `render_sequence(image_format/also_png)`・MRQ キューの退避復元・`force_reset`・PIE ハイダの照合キー・
  セットアップ失敗時の復元・`render_depth_sequence`（16bit/EXR, 32bit float 往復）・tkinter パネルの生成
  （共通対象カード・映像タブの形式/ビット深度・設定往復・busy ガード・早期リターン・カメラカット解決）を
  実走（結果 JSON は scratch の `smoke_*_result_*.json`）。
- ライブエディタ（PIE レンダ・Sequencer 連携・実ファイル出力）は未実走。§3 を参照。

## 7. 根拠ファイル

レビュー報告の原本（行番号付き・約 30 件×3）と 5.8 互換レポートは作業用 scratch に保存:
`review_core.md` / `review_mrq.md` / `review_ui.md` / `ue58_compat.md` / `api_table.md`。
