@echo off
echo.
echo  =============================================
echo   SENTINEL FX -- Starting All Services
echo  =============================================
echo.

echo  [1/4] Starting FastAPI backend (port 8000)...
start "SentinelFX Backend" cmd /k "cd /d "%~dp0backend" && uvicorn main:app --reload --port 8000"

timeout /t 2 /nobreak >nul

echo  [2/4] Starting Executor Bridge (port 8080)...
start "SentinelFX Executor" cmd /k "cd /d "%~dp0executor" && python executor.py"

timeout /t 2 /nobreak >nul

echo  [3/4] Starting Ollama...
start "SentinelFX Ollama" cmd /k "ollama serve"

timeout /t 2 /nobreak >nul

echo  [4/4] Launching portal...
start "" "%~dp0frontend\index.html"

echo.
echo  Backend  : http://localhost:8000
echo  Executor : http://localhost:8080
echo  Docs     : http://localhost:8080/docs
echo  Portal   : frontend\index.html
echo.
echo  TradingView webhook URL:
echo  http://localhost:8080/webhook  (use ngrok for public URL)
echo.
pause
