@echo off
echo.
echo  =============================================
echo   SENTINEL FX -- Running Setup Hunter
echo  =============================================
echo.

cd /d C:\Users\ksapk\sentinelfx\architect
python setup_hunter.py --scan

echo.
echo  Scan complete. Run --list to see results.
pause
