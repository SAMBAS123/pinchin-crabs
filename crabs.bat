@echo off
title Crab Simulation
:loop
python "%~dp0crab_sim.py"
echo.
echo Press any key to restart, or close the window to quit.
pause >nul
goto loop
