@echo off
chcp 65001 >nul
cd /d E:\code-diff-scanner
:loop
echo [%date% %time%] CodeDiffScanner starting...
E:\python\python.exe scan_server.py --port 8899 >> E:\code-diff-scanner\server.log 2>&1
echo [%date% %time%] CodeDiffScanner exited, restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
