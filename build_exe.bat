@echo off
cd /d %~dp0
pyinstaller --noconsole --onefile --name gemini2a ^
  --collect-all uvicorn --collect-all playwright ^
  --hidden-import Crypto.Cipher.AES ^
  --add-data "web;web" ^
  gemini2a_gui.py
echo.
echo 输出: dist\gemini2a.exe
pause
