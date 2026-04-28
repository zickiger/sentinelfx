@echo off
echo.
echo  =============================================
echo   SENTINEL FX -- Starting All Services
echo  =============================================
echo.

echo  [1/4] Starting FastAPI backend (port 8000)...
start "SentinelFX Backend" cmd /k "cd /d %~dp0backend && uvicorn main:app --reload --port 8000"

timeout /t 2 /nobreak >nul

echo  [2/4] Starting Executor Bridge (port 8080)...
start "SentinelFX Executor" cmd /k "cd /d %~dp0executor && python executor.py"

timeout /t 2 /nobreak >nul

echo  [3/4] Starting Ollama...
start "SentinelFX Ollama" cmd /k "ollama serve"

timeout /t 2 /nobreak >nul

echo  [4/5] Starting frontend HTTP server (port 3000)...
start "SentinelFX Portal" cmd /k "cd /d %~dp0frontend && python -m http.server 3000"

timeout /t 2 /nobreak >nul

echo  [5/5] Opening portal in browser...
start "" http://localhost:3000

echo.
echo  Backend   : http://localhost:8000
echo  Executor  : http://localhost:8080
echo  Portal    : http://localhost:3000
echo  Validator : http://localhost:3000/validator.html
echo  API Docs  : http://localhost:8000/docs
echo.
echo  TradingView webhook URL:
echo  http://localhost:8080/webhook  (use ngrok for public URL)
echo.
pause
