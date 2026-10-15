@echo off
chcp 65001 >nul
call "%~dp0配置并导入.cmd" --refresh %*
exit /b %errorlevel%
