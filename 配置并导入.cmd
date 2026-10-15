@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo 需要安装 Python 3.10 或更高版本。
    popd
    pause
    exit /b 1
  )
  python -X utf8 -B setup.py %*
) else (
  py -3 -X utf8 -B setup.py %*
)
set "wechat_exit=%errorlevel%"
echo.
pause
popd
exit /b %wechat_exit%
