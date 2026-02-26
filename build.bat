@echo off
pip install pyinstaller pandas schedule
pyinstaller --onefile --windowed --name="CH05综合台QSHEET检查程序" paste.py
echo 打包完成！exe文件在dist文件夹中
pause