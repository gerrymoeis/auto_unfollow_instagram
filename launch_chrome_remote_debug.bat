@echo off
REM Launch Chrome with remote debugging and a dedicated user-data-dir
REM This script supports overrides via a .env file in the same directory.

setlocal EnableDelayedExpansion

REM ---- Defaults (used if .env not present or keys missing) ----
if not defined CHROME_PATH set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not defined USER_DATA_DIR set "USER_DATA_DIR=%USERPROFILE%\ChromeAutomationProfile"
if not defined REMOTE_PORT set "REMOTE_PORT=9222"

REM ---- Load overrides from .env (simple KEY=VALUE lines; lines starting with # are ignored) ----
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "k=%%A"
    set "v=%%B"
    if not "!k!"=="" (
      echo !k!| findstr /b /r /c:"#" >NUL || (
        if /i "!k!"=="CHROME_PATH" call set "CHROME_PATH=!v!"
        if /i "!k!"=="CHROME_USER_DATA_DIR" call set "USER_DATA_DIR=!v!"
        if /i "!k!"=="REMOTE_DEBUG_PORT" call set "REMOTE_PORT=!v!"
      )
    )
  )
)

REM Optional: ensure Chrome is closed first
REM taskkill /IM chrome.exe /F >NUL 2>&1

if not exist "%USER_DATA_DIR%" mkdir "%USER_DATA_DIR%"

"%CHROME_PATH%" --remote-debugging-port=%REMOTE_PORT% --user-data-dir="%USER_DATA_DIR%" --disable-blink-features=AutomationControlled

echo Launched Chrome with remote debugging on port %REMOTE_PORT%.
echo Profile dir: %USER_DATA_DIR%
echo Log in to Instagram, open your profile, and open the Following dialog before running the Python script.
pause
