# NSIS 安装脚本
# 需要安装 NSIS (Nullsoft Scriptable Install System)

!define APP_NAME "大洋播控自动化程序"
!define APP_VERSION "1.0.0"
!define APP_PUBLISHER "Jerry Lee"
!define APP_EXE "大洋播控自动化程序.exe"

# 安装程序属性
Name "${APP_NAME}"
OutFile "${APP_NAME}_Setup_${APP_VERSION}.exe"
InstallDir "$PROGRAMFILES\${APP_NAME}"
RequestExecutionLevel admin

# 页面
Page directory
Page instfiles

# 默认安装部分
Section "MainSection" SEC01
    SetOutPath "$INSTDIR"
    File "dist\${APP_EXE}"

    # 创建开始菜单快捷方式
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载.lnk" "$INSTDIR\Uninstall.exe"

    # 创建桌面快捷方式
    CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"

    # 写入卸载信息
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"

    # 创建卸载程序
    WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

# 卸载部分
Section "Uninstall"
    Delete "$INSTDIR\${APP_EXE}"
    Delete "$INSTDIR\Uninstall.exe"

    # 删除快捷方式
    Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\卸载.lnk"
    Delete "$DESKTOP\${APP_NAME}.lnk"
    RMDir "$SMPROGRAMS\${APP_NAME}"

    # 删除注册表项
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"

    # 删除安装目录
    RMDir "$INSTDIR"
SectionEnd
