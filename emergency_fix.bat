@echo off
echo 紧急修复pandas打包问题

echo 1. 重新安装所有依赖...
pip uninstall pandas numpy pyinstaller -y
pip install pandas numpy pyinstaller

echo 2. 强制包含pandas...
pyinstaller --onefile --windowed --add-data "pandas;pandas" --add-data "numpy;numpy" --hidden-import pandas --hidden-import numpy --name="QSHEET检查程序" paste.py

echo 3. 完成！
pause