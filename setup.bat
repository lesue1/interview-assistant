@echo off
chcp 65001 >nul
echo === 开始搭建环境 ===

set PY=C:\Games\anaconda\python.exe

if not exist "%PY%" (
    echo [错误] 找不到 %PY%
    echo 请确认 anaconda 安装路径，或手动修改 setup.bat 里的 set PY= 一行。
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/3] 创建虚拟环境 .venv ...
    "%PY%" -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在，跳过创建
)

echo [2/3] 升级 pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [3/3] 安装依赖（这一步会比较久，请耐心等待）...
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败，请把上面的报错信息发给我。
    pause
    exit /b 1
)

echo.
echo === 环境搭建完成 ===
echo 接下来两步：
echo   1. 把 .env.example 复制一份改名为 .env，填上你的 DeepSeek API Key
echo   2. 双击 run.bat 启动
pause
