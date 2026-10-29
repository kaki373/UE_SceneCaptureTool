# Changelog

版番号は [Semantic Versioning](https://semver.org/lang/ja/) に従う。版はリポジトリ直下の
`VERSION` ファイルが正本（`Install.bat` が表示し、`build_dist.ps1` が zip 名に使う）。

## 1.0.0 - 2026-10-29

社内での運用実績をもとにした最初の配布版。

### 対応環境
- Unreal Engine **5.7 / 5.8** に対応（エディタ同梱 Python 3.11）。

### インストーラ（Install.bat）
- 1 ファイルで完結するインストーラ。エンジン検出 → 同梱 Python へ numpy / Pillow /
  imageio / imageio-ffmpeg → `Documents\UnrealEngine\Python` へ起動スクリプト →
  `.uproject` の PythonScriptPlugin / MovieRenderPipeline 有効化。
- `.uproject` を渡すとその `EngineAssociation` のエンジンを使う。指定が無ければ対応版の最新。
- 起動スクリプトは全エンジン共通なので、見つかった他の対応エンジンにもライブラリを入れる。
- `--uninstall` / `--dry-run` / `--no-libs` / `--help` を追加。
- 配布 zip に `wheels\` があればオフラインで pip を通す（`build_dist.ps1 -Wheels`）。

### キャプチャ機能（この版までの主な変更）
- **露出固定**: レンダ中だけ自動露出を切り、フレーム間の明るさの揺れを止める（両タブ）。
- **Matteの奥**: 空マット同時選択時は奥プレートも空黒で撮る。マットを跨ぐアクターと
  手前のアクターは黒塗りでなく非表示にする。傾いたマット板でも画素単位の深度比較で
  手前ジオメトリを除去する。600m より遠いマット板が抜け落ちる不具合を修正。
- **UI**: ダークのカード型レイアウト、出力のグループ化、ツールチップ、追従する実行ボタン。
- **雲マット**: UDS の雲レイヤーや雲内フォグ板も拾う「ベール2本差分（4レンダ）」方式に移行
  （両タブ）。出力名に濃度タグ、雲マット/空マットのみ選択時はメイン Beauty ジョブを省略。
  ヘルパージョブの固定コストを削減。
- **空マット（SkyMatte）**: 空だけ純黒・PPマテリアル / ブルーム / fog OFF の専用ジョブ（両タブ）。
- **Z-Depth**: 可視レンジの自動フィット（Near/Far の「自動」）。16bit PNG / EXR で far が
  655m を超えると空が黒にならなかった不具合を修正（RGBA32F 取得）。
- `render_beauty` / `render_sequence` に `pp_materials_off` 引数を追加。
