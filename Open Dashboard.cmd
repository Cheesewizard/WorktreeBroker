@echo off
rem Start the dashboard without a console window. install.py records the pythonw next to the Python it ran
rem with in .python-path; otherwise try the py launcher, then pythonw on PATH.
set "PYW="
if exist "%~dp0.python-path" set /p PYW=<"%~dp0.python-path"
if defined PYW if exist "%PYW%" (start "" "%PYW%" "%~dp0dashboard.py" & exit /b 0)
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0dashboard.py" & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0dashboard.py" & exit /b 0)
echo No pythonw found. Run "python install.py config" with the Python you installed requirements.txt into.
pause
