@echo off
chcp 65001
echo 开始打包 大洋播控自动化程序...

echo 检查 Python 环境...
python --version
if errorlevel 1 (
    echo Python 未安装或未添加到 PATH
    pause
    exit /b 1
)

echo 安装依赖...
pip install -r requirements.txt

echo 安装 Playwright 浏览器...
playwright install chromium

echo 开始打包...
pyinstaller --clean 大洋播控自动化程序.spec

if exist "dist\大洋播控自动化程序.exe" (
    echo 打包成功！
    echo 可执行文件位置: dist\大洋播控自动化程序.exe
) else (
    echo 打包失败！
)

pause
