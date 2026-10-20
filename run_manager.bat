@echo off
chcp 65001 > nul
title Cloudflare IaC Portal
echo 🚀 Запуск панели управления Cloudflare IaC Portal...
echo ---------------------------------------------------

:: Переходим в папку проекта
cd /d G:\cloudflare-iac-portal

:: Активируем виртуальное окружение Python
call .venv\Scripts\activate.bat

:: Запускаем веб-интерфейс Streamlit
streamlit run app.py

pause