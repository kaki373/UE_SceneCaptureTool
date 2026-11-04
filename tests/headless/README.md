# ヘッドレス スモークテスト

【実行】
`powershell -ExecutionPolicy Bypass -File tests\headless\run_all_smokes.ps1 -Engines "D:\Unreal\UE_5.7","D:\Unreal\UE_5.8"`
エンジンごとに捨てプロジェクトを `-Work`（既定 `%TEMP%\UE_SceneCaptureTool_smoke`）に作り、順番に実行します（1エンジン約6分）。

【前提】
エンジン Python に numpy / Pillow / imageio が必要です（Install.bat 済み）。未導入なら `-Site <site-packages>` で場所を渡します。

【結果の見方】
`<Work>\smoke_report.txt` に各スクリプトの `SUMMARY ... fail=N` と FAIL 行が出ます。全行 fail=0 なら合格。
自分で起動したコマンドレットだけを PID で止めるので、開いている他のエディタには触れません。
