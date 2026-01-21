# 大洋播控自动化程序

## 简介
大洋播控自动化程序，用于自动生成和发送播控监播记录表。

## 版本信息
- 版本: 1.0.0
- 作者: Jerry Lee
- 构建日期: {build_date}

## 功能特性
- 自动爬取播控数据
- 生成 Excel 报表
- 自动发送邮件
- 定时任务调度
- GUI 界面操作

## 打包说明

### 方法一：一键打包（推荐）
1. 运行 `python build_app.py`
2. 程序会自动检查依赖并开始打包
3. 打包完成后可执行文件位于 `dist` 目录

### 方法二：手动打包
1. 安装依赖: `pip install -r requirements.txt`
2. 运行批处理: `build.bat` (Windows) 或 `./build.sh` (Linux/Mac)

### 方法三：使用 PyInstaller
```bash
# 基础打包
pyinstaller --onefile --windowed main.py

# 完整打包（推荐）
pyinstaller --clean 大洋播控自动化程序.spec
```

## 部署说明
1. 将生成的 exe 文件复制到目标机器
2. 确保目标机器安装了必要的运行时环境
3. 首次运行时可能需要安装 Playwright 浏览器：
   ```
   playwright install chromium
   ```

## 故障排除
- 如果程序无法启动，请检查是否缺少 VC++ 运行库
- 如果网页爬取失败，请检查网络连接和目标网站状态
- 如果邮件发送失败，请检查邮箱配置和网络连接

## 注意事项
- 程序运行时请保持网络连接
- 首次使用时需要配置邮箱设置
- 建议在测试环境中先运行测试功能

## 联系方式
如有问题请联系: Jerry Lee
邮箱: jerry.lee@eracom.com.tw
