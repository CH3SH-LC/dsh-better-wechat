@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
echo 此入口会联网安装采集依赖到本机私有运行环境。
echo 如 DSH 使用自定义数据目录，请传入相同的 --state 参数。
where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo 需要安装 Python 3.10 或更高版本。
    popd
    pause
    exit /b 1
  )
  python -X utf8 -B setup.py --install-collector-dependencies %*
) else (
  py -3 -X utf8 -B setup.py --install-collector-dependencies %*
)
set "wechat_exit=%errorlevel%"
echo.
pause
popd
exit /b %wechat_exit%
