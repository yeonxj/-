@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 server.py
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python server.py
    goto finished
)
echo Python 실행 명령을 찾지 못했습니다.
echo VS Code에서 server.py를 열고 Ctrl+Shift+P를 누른 뒤
echo Python: Run Python File in Terminal 을 선택해 주세요.
:finished
echo.
echo 서버가 종료되었습니다. 오류가 있다면 위 내용을 확인해 주세요.
pause
