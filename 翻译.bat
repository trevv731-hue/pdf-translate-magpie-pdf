@echo off
rem Forwarder for the old desktop shortcut. ASCII only - see pdfq.bat.
rem The real launcher is pdfq.bat; this file exists so shortcuts that point at
rem the Chinese-named entry keep working.
call "%~dp0pdfq.bat" %*
