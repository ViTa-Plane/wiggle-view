@echo off
setlocal

:: Define paths (assumes run.bat and \assets\icon.ico are in the same folder as this script)
set "TARGET=%~dp0run.bat"
set "ICON=%~dp0\assets\icon.ico"
set "SHORTCUT=%USERPROFILE%\Desktop\WiggleView.lnk"
set "VBS=%TEMP%\create_shortcut_%RANDOM%.vbs"

:: Build temporary VBScript
(
    echo Set oWS = WScript.CreateObject("WScript.Shell"^)
    echo Set oLink = oWS.CreateShortcut("%SHORTCUT%"^)
    echo oLink.TargetPath = "%TARGET%"
    echo oLink.WorkingDirectory = "%~dp0"
    echo oLink.IconLocation = "%ICON%, 0"
    echo oLink.Save
) > "%VBS%"

:: Run the VBScript and clean it up
cscript //nologo "%VBS%"
del "%VBS%"

echo Shortcut created on Desktop.
pause
