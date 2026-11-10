@echo off
rem Class Copilot - Windows 启动脚本
rem 双击即可: 自动打开浏览器访问 http://127.0.0.1:8321
cd /d "%~dp0"
if not exist config.json (
  echo.
  echo   首次运行需要配置文件: 已为你复制 config.example.json -^> config.json
  echo   请打开 config.json 填入 API Key 后重新运行。
  echo.
  copy /y config.example.json config.json >nul
  if not exist config.json (
    echo   [错误] 无法创建 config.json, 请手动复制 config.example.json
    pause
    exit /b 1
  )
  start "" http://127.0.0.1:8321
  python app.py
  goto :eof
)
start "" http://127.0.0.1:8321
python app.py
pause
