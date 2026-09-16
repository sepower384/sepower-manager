# 세력의 매니저 끄기
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*sepower-manager*run.py*serve*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "종료: $($_.ProcessId)" }
