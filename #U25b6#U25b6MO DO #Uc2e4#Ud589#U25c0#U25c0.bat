@echo off
setlocal
cd /d "%~dp0"
title 모두 (MO DO) v3.0

echo ==========================================
echo 모두 (MO DO) v3.0
echo ==========================================

where python >nul 2>&1
if %errorlevel%==0 goto RUN_PYTHON

where py >nul 2>&1
if %errorlevel%==0 goto RUN_PY

echo [ERROR] Python was not found.
echo Please install Python and try again.
pause
exit /b 1

:RUN_PYTHON
python "%~dp0server_stdlib.py"
set EXITCODE=%errorlevel%
goto END

:RUN_PY
py -3 "%~dp0server_stdlib.py"
set EXITCODE=%errorlevel%
goto END

:END
if not "%EXITCODE%"=="0" (
  echo.
  echo [ERROR] MOA WORK stopped with exit code %EXITCODE%.
  pause
)
endlocal
exit /b %EXITCODE%
