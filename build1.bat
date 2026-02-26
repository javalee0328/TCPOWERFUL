@echo off
chcp 65001
echo 開始打包 大洋播控監播...

echo 檢查 Python 環境...
"C:\Users\jerry.lee\AppData\Local\Microsoft\WindowsApps\python.exe" --version
if errorlevel 1 (
    echo Python 路徑錯誤，請檢查 where python 輸出！
    pause
    exit /b 1
)

echo 安裝依賴...
"C:\Users\jerry.lee\AppData\Local\Microsoft\WindowsApps\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo 依賴安裝失敗！
    pause
    exit /b 1
)

echo 安裝 Playwright 瀏覽器...
"C:\Users\jerry.lee\AppData\Local\Microsoft\WindowsApps\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo Playwright 瀏覽器安裝失敗！
    pause
    exit /b 1
)

echo 開始打包...
"C:\Users\jerry.lee\AppData\Local\Microsoft\WindowsApps\python.exe" -m PyInstaller --clean 大洋播控監播.spec
if errorlevel 1 (
    echo 打包失敗！
    pause
    exit /b 1
)

if exist "dist\大洋播控監播.exe" (
    echo 打包成功！
    echo 可執行文件位置: dist\大洋播控監播.exe
) else (
    echo 打包失敗！請檢查 .spec 文件或依賴。
)

pause