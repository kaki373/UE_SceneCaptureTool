# VDB雲アルファ対応の調査記録（2026-07-24・全て実測）

対象: Scene Capture Tool への HeterogeneousVolume（SVT/VDB雲）のアルファ対応。
検証環境: UE 5.7.4 / GSN_V / `/Game/Vkon/GSN_MND1/AV003/AV086`（雲 = `BP_Cloud_Single_preview_C`
× HeterogeneousVolumeComponent + StaticSparseVolumeTexture、空 = Ultra_Dynamic_Sky）。

このセッションの実装一式はコミットせず **`git stash` "cloud-alpha WIP 2026-07-24"**（branch
capture-dev）に退避してある。作業ツリーは 69fd7db（素のツール）に戻し済み。

---

## 前提となる事実（成功・失敗の両方の土台）

- HV（VDB雲）は **SceneDepth / CustomDepth を一切書かない** → 既存のステンシル・深度比較
  ベースの Matte / ObjectID には原理的に写らない。`render_in_main_pass=False` も効かない。
- `r.PostProcessing.PropagateAlpha` は 5.7 では**ランタイム切替可能な bool cvar** になっており、
  さらに **MRQ は毎レンダで自動的に 1 へ強制**する（`MoviePipeline.AlphaOutputOverride` 既定 true、
  レンダ後に自動復元）。PNG の `write_alpha=True` + DeferredPassBase の
  `accumulator_includes_alpha=True` でαがファイルに出る。
- 上記の条件で **HV の透過率はαに正しく畳み込まれる**（雲コアα≈1.0・ソフトエッジまで実測OK）。

## ✅ 成功した方式（stash に実装済み・映像5フレームE2Eでα0-255実証）

**CloudMatte 分離ジョブ**: 対象雲以外を**アクター単位**で隠して 1 本追加レンダし、そのαを
既存のマスク合成に統合する。成立条件（全部必須・下の失敗一覧の裏返し）:

1. 非対象は `set_actor_hidden_in_game(True)`（**アクター単位**）で隠す
2. ただし**ライトコンポーネントを持つアクター（UDS/UDW/各ライト）は隠さない**
3. 大気/フォグは ShowFlag cvar（`ShowFlag.Atmosphere 0` / `ShowFlag.Fog 0` /
   `ShowFlag.VolumetricFog 0`）で消す。**`ShowFlag.Cloud` は使わない**
4. αのみのジョブは `temporal_sample_count=1` 固定
5. 雲は Beauty から隠さない＝通常オブジェクト扱いがユーザー確定仕様（トグル不要・固定）

---

## ❌ 失敗した方式（再試行禁止）

### 1. ホールドアウト方式（遮蔽考慮の雲マット）— UE5.7 では使用不能
- `r.Deferred.SupportPrimitiveAlphaHoldout=True` を **単独で** DefaultEngine.ini に入れる:
  **エディタがHV描画で即 Fatal クラッシュ**。
  `FRenderSingleScatteringWithLiveShadingDirectCS's required shader parameter
  FParameters::RWHoldoutTexture was not set.`（ShaderParameterStruct.cpp:473）
  原因: この cvar はシェーダ define をグローバルに立てて RWHoldoutTexture を必須化するが、
  C++ 側の束縛は `IsPrimitiveAlphaHoldoutEnabled(View)`（= PropagateAlpha が条件）ガード付き。
  PropagateAlpha=0 のエディタビューポートで必ず未束縛になる。
- `r.PostProcessing.PropagateAlpha=True` と**ペア**で入れる（エンジンの正規の組合せ）:
  クラッシュは消える（雲入りビューポート45秒安定を実測）が、**holdout のα出力がほぼゼロ**
  （全対象 holdout でα最大 2/255）。5.7 の HV holdout は実質未完成。
- 補足: 両 cvar とも ECVF_ReadOnly（ini+再起動必須）で、有効化するとシェーダ permutation が
  増えて初回起動のコンパイルが長い。**結論: ini はペアでも入れない。遮蔽なし分離モードで運用。**
- `r.HeterogeneousVolumes.Holdout` という cvar は**存在しない**（get_console_variable_int_value は
  未定義 cvar でも 0 を返すので存在確認には使えない）。HV の holdout はコンポーネントの
  `holdout` プロパティ（bHoldout）。

### 2. `ShowFlag.Cloud 0` で雲海（VolumetricCloud）だけ消す → **HV(VDB雲)も消えて全黒**
UDS の雲海を除外するつもりで入れると VDB 雲ごと描画されなくなる（αもRGBも全ゼロ）。
HV は `EngineShowFlags.HeterogeneousVolumes` 持ちだが Cloud フラグにもゲートされている。

### 3. 非対象アクターの**コンポーネント単位** hidden_in_game / visible 切替 → 全黒＋復元不能
「ライトを残すためにメッシュコンポーネントだけ隠す」方式。SCS コンポーネントのプロパティ編集が
**BPアクターの再構築を誘発し、保存しておいたコンポーネント参照が無効化** → 復元が空振りして
隠しフラグがレベルに残留する（BG_Lo等が消えたままになる事故）。さらにレンダ自体も全黒になる。
**シーン状態の一時変更はアクター単位のプロパティ（bHidden 等）だけで行うこと。**

### 4. 雲以外を**ライトごと全部**隠す分離レンダ → αも出ない
HV は**無照明だとライティングパス自体がスキップされαも書かれない**（RGB黒・α0）。
太陽/スカイライトを持つアクター（UDS/UDW）は必ず残す。

### 5. αのみのジョブに temporal_sample_count ≥ 2 → αが 1/TS に希釈
TS=2 で雲コアαが最大128で頭打ち（空のサブフレームが平均に入る）。αジョブは TS=1 固定。
（Beauty とのモーションブラー差は雲がソフトエッジなので許容範囲。）

### 6. レンダ中（ジョブチェーンの合間含む）の `importlib.reload(capture_mrq)` / パネル再構築
実行中ジョブの executor と**シーン復元コールバックを破壊**する（チェーンが無言で死に、
CloudMatte が隠した17アクターが残留した実事故）。`is_rendering()` は**チェーンのジョブ間で
False になる**ため単独チェックでは不十分 — `capture_mrq._KEEP.get("executor")` も併せて確認する。
（ガード入りコードは stash 内: _on_mrq / _on_seq_render / on_open_panel の3ヶ所。）

### 7. その他の罠（小粒だが実害あり）
- **Guid 構造体の `==` は UE5.7 Python で常に False**（`to_tuple()` も空タプル）。
  カメラカットのバインディング解決は `guid.export_text()` の文字列比較で行う。
  素のツールの `_camera_cut_camera_actors` はこのバグで常に空→Cineフォールバック頼み。
- **PIE 遷移中は `get_all_level_actors()` が空/部分リストを返す**ことがある
  （アクター解決・カメラ解決が一時的に全滅する）。レンダ中のリモート操作は避ける。
- HV 雲へのマット板用マテリアル差替え（`set_matte_unlit`）は不可: Volume ドメイン必須のうえ、
  現在材が BP 生成の Transient MID なので退避パスが復元不能（`/Engine/Transient.MID_...` の
  残骸タグがレベルに残る）。雲はマテリアル退避機構の対象から除外する。

---

## 未解決（次の課題）

1. **静止画（フレーム凍結パス）で Ultra_Dynamic_Sky の見た目がシーケンスの暗いルックにならず
   昼空になる**。映像レンダは正しい。素ツールの 17:10 出力（AV003_Dev/old2）は暗いルックで
   正しかったので、コード回帰かエディタ再起動による UDS の未保存状態消失かの切り分けが最初の一歩。
2. **雲αがフレーム/照明に依存して減衰する事例**: frame36/1920x1080 でα最大0.27、
   frame100/960x540 では全域OK。「無照明でα0」の実測と併せ、①の UDS 状態と同根の疑い。

---

## 2026-07-24 後続セッション: 上記1・2の原因確定 → stash 再適用・E2E再検証済み

**結論: 1も2もコード回帰ではなく、レベルの未保存インメモリ状態（黒空ルック）の消失が原因。**
stash "cloud-alpha WIP 2026-07-24" は再適用し、コミット済み（本ドキュメントと同じコミット群）。

### 昼空化（1）の証拠 — 環境説で確定
- 基準出力 old2 の Beauty は「真っ黒な空 + 雲海 + 遺跡」のルック。現在のシーンを
  SceneCapture で実測すると**青い昼空**（UDS Time of Day = 950 = ディスク値）。
- Seq_AV003 の UDS バインディングには **Transform トラック（Location キー1個・現在値と同一）
  しか無い**。Time of Day / 回転 / ルック系のトラックは一切なく、シーケンスは UDS の
  見た目を駆動していない。→ 静止画/映像どちらのパスでも「そのときのレベル状態」で写る。
- よって 17:10 の正ルックは当時のインメモリ状態によるもので、holdout 実験の
  クラッシュ→再起動×3 でディスク値（昼）に戻った。20:01 の素ツール出力が昼空なのも同根。
- **ユーザー回答（同日）: 黒空は UDS の設定ではなく、一定距離に置いたマットオブジェクトが
  空を遮って黒く見えているだけ（シーン構成として意図どおり）。ルック復元は課題ではない。**

### 雲α減衰（2）も同時解決
現在（昼ライティング）の再検証で **frame36 / 1920x1080 の CloudMatte αは 0-255 フルレンジ**
（ゼロ9.1% / ソフト18.6% / フル72.3%）。前回の「α最大0.27」は壊れた照明状態での実測だった。
「HVは無照明でα0」の性質どおり、ライティング状態が正しければ減衰しない。

### 再適用後の E2E 検証（全て 2026-07-24・GSN_V AV086・Cam_AV003_UE-exp）
- 静止画 frame100 960x540: MatteBeauty α 0-255（ソフトエッジ14.1%）、
  ObjectID PNG+JSON に雲2色+メッシュ1色（golden-angle 色・マニフェスト一致）
- 映像 frames100-104 960x540 TS=2: 全5フレーム MatteBeauty α 0-255（ソフト≈11%）、
  CloudMatte 中間物はトリム済み、雲 ObjectID は映像未対応の注記が出る（仕様どおり）
- 静止画 frame36 1920x1080: α 0-255 フルレンジ（上記）
- レンダ後リーク検査: hidden アクター 0・_KEEP 空・迷子アクター 0・
  一時アセットは常設 M_UE5Cap_MatteBoardUnlit のみ
- 再適用時の変更点: `_run_cloud_matte` のステータス文言「(holdout α)」→「(分離モードα)」
  （実態は分離モード。コード挙動の変更なし）

### 残タスク（このセッション終了時点）
- 黒空ルックの復元（ユーザーの UDS 設定確認待ち）→ 復元後に基準ルックで最終確認レンダ
- AV086 のディスク残骸掃除（雲の ue5cap_origmat タグ・stencil=11、BG_Lo の stencil=1）＝
  クリーンアップ後にレベル保存が必要（ユーザー判断）
- Behind（Matteの奥）の手前オブジェクト除去の後処理不具合（ユーザー要望・未着手）

---

## 2026-07-24 さらに後続: 分離レンダ廃止 → バッキング差分方式へ（全て実測）

ユーザー要件「雲を特別扱いせず、マット板の前後で他のオブジェクトと同じ遮蔽挙動に」を受け、
板対象があるときの雲マットを**バッキング差分方式**に置換（分離モードは板なし・雲のみ対象と
雲 ObjectID にのみ残置）。

### 方式
マット板を白(1.0)/黒(0.18=MatteBoardUnlit) のアンリット材で2回レンダ（EXR・TS=1・
露出固定・**何も隠さない**）。体積レンダは背景放射に対して線形なので、線形色の差分
`白−黒 ∝ 板より手前の透過率T`（数学的に正確）。`mask' = 255−(255−mask)×T` で板マスクに
統合 → 板領域外は mask=255 で不変（領域判定不要）、板の後ろの雲は板に遮られ自動無効、
手前のジオメトリは差分で相殺（ステンシルマスク側とも整合）。

### プロトタイプ実測（960x540 frame100・9ペア）
- MRQ 再レンダの再現性: 同一条件は**バイト一致**（黒×黒ペアの差 p99=スケールの0.3%）
- **露出適応が最大の汚染源**: 白板に反応して全画面が輝度比例で約-18%（相関-0.81）。
  ジョブ内 cvar（r.EyeAdaptationQuality 0 + r.LocalExposure.*ContrastScale 1）で解消
- GI/反射/スクリーントレース/ブルーム/TSR→FXAA は差分に**寄与せず**（切っても不変）。
  残差は板外 p99≈スケールの7%・平均0.5%（板隣接の細エッジ局所）でマット用途には十分
- EXR 読み: **同梱 ffmpeg で PIZ 圧縮 EXR → 16bit グレーPNG 変換が真値と相関 0.99999**
  （OpenEXR 依存を増やさない。UE python は numpy/PIL あり・OpenEXR なし）

### 実装で踏んだ罠（追加の再試行禁止）
1. **凍結静止画の TS=2 は同一ジョブ Matte/Depth PP パスを 50% 希釈**: マスク黒が
   sRGB(0.5)=187 で頭打ち（2サブフレーム中1つにしか写らない）。映像レンダでは起きない。
   対策: 静止画メインジョブは PP パスがあるとき TS=1 に自動クランプ（凍結静止画の
   サブフレームは同一内容で TS>1 に画質上の意味なし）。※これは分離モード時代からの
   既存バグ（当日の take 001 でも 187 を確認）
2. **映像の EXR 出力は multilayer 既定だと {render_pass} トークンが空**になり、
   BackingW/B ジョブが同名ファイルを上書きし合う。`multilayer=False` で FinalImage 名を
   出してリネームに乗せる
3. 白アンリット板はそのままだと露出適応で全画面を沈める（上記）。cvar はグローバル
   console でなく**ジョブ内 ConsoleVariableSetting** で入れる（MRQ の GameOverride/
   scalability と衝突しない・自動復元）

### 追補（同日・ユーザー報告2件の修正）
- **HV(VDB雲)は `r.SetNearClipPlane` の描画クリップを無視する（実測）**: Matteの奥の
  BehindPlate からメッシュは near-clip で消えるが雲はゴーストとして残る。対策:
  プレートジョブで「板より手前（カメラ視線深度 < near-clip 距離）の HV アクター」を
  アクター単位で隠す（`core.volumetrics_nearer_than`・静止画/映像両パス）。
  板より奥の雲は窓の中身として残す。
- **マット板のアンリット材を 0.18 グレー → 純黒(0.0) に変更**: グレー板はシーンの
  グレーディングで茶色に見え、Lumen スクリーントレース/反射経由で周囲のオブジェクトに
  色が被っていた（ユーザー報告）。黒＝放射ゼロで色被りが原理的に消える。Beauty は
  板が深度でのみ写る（黒穴）ため見た目不変。バッキング差分の黒側も 0 になり単純化。
- **BehindPlate の半透明ゴースト（ユーザー報告）**: `r.SetNearClipPlane` を MRQ の
  start_console_commands だけで入れると、ウォームアップ/先頭サブフレームに間に合わず
  クリップ有り/無しのフレームが平均されて手前オブジェクトが約50%の半透明で残る（実測）。
  対策: レンダ起動前にグローバルへも同コマンドを適用（完了時の 10cm 復元は既存のまま）
  ＋静止画の BehindPlate ジョブは TS=1 固定。修正後は手前がクリップ面で完全に消える。

### 検証（AV086・板2枚+雲5、Matte対象=Matte_plane）
- 静止画 frame100 960x540（take 007）: α 0-255、板の素領域=0、板の手前の雲がソフトに
  穴を塞ぐ、板領域外に雲の穴なし（旧方式 take 001 との A/B で確認）
- 映像 frames100-104（take 009）: 全5フレーム α 0-255・ゼロ0.6%・ソフト1.7%（静止画と同一
  プロファイル）。BackingW/B EXR・BackingT 中間物は自動削除
- レンダ後リーク 0（hidden 0・板マテリアル復元・cvar 全て既定値・_KEEP 空）
- 常設アセット追加: /Game/_UE5Capture_Tmp/M_UE5Cap_BackingWhite（白バッキング材）
