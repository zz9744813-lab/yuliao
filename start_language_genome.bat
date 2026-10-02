@echo off
rem Language Genome - one-click launcher (server + console)
rem Console: http://127.0.0.1:8787/    API docs: /docs
rem NOTE: all system tools are called by absolute path so a polluted PATH cannot break us.
title Language Genome
cd /d "F:\agi\language-genome"

%SystemRoot%\System32\netstat.exe -ano | %SystemRoot%\System32\findstr.exe ":8787" | %SystemRoot%\System32\findstr.exe "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [Language Genome] server already running - opening console...
    start "" "http://127.0.0.1:8787/"
    %SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul
    exit /b 0
)

rem Only direct loopback visits skip the token; proxied/remote traffic still needs it.
set LG_LOCAL_BYPASS=1
echo [Language Genome] starting server on http://127.0.0.1:8787/ ...
start "Language Genome server" /min "C:\Users\6\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8787

set /a tries=0
:waitport
%SystemRoot%\System32\timeout.exe /t 1 /nobreak >nul
%SystemRoot%\System32\netstat.exe -ano | %SystemRoot%\System32\findstr.exe ":8787" | %SystemRoot%\System32\findstr.exe "LISTENING" >nul 2>&1
if %errorlevel%==0 goto ready
set /a tries+=1
if %tries% lss 25 goto waitport
echo [Language Genome] server did not come up. Run this from a cmd window to see errors:
echo     "C:\Users\6\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8787
pause
exit /b 1

:ready
start "" "http://127.0.0.1:8787/"
echo [Language Genome] console opened in your browser.
echo The server keeps running minimized - close the "Language Genome server" window to stop it.
%SystemRoot%\System32\timeout.exe /t 4 /nobreak >nul
exit /b 0