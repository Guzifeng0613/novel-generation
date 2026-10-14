@echo off
set NOVEL_TEST_FAKE_PROVIDER=0
set NOVEL_PORT=8020
set NOVEL_DEEPSEEK_MODEL=deepseek-chat
set NOVEL_DATA_DIR=C:\Users\39187\Desktop\novel\data\live-8020
set NOVEL_DATABASE_URL=
cd /d C:\Users\39187\Desktop\novel
D:\Python\3.14.4\python.exe -m backend.app.main
