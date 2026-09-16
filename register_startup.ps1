# 로그인 시 자동 시작 등록(창 없음)
$vbs = Join-Path $PSScriptRoot 'start.vbs'
$act = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
$trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName 'SepowerManager' -Action $act -Trigger $trg -Settings $set -Force | Out-Null
"등록 완료: SepowerManager"
