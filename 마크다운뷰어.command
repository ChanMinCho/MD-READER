#!/bin/sh
# Finder 에서 더블클릭하면 뷰어가 실행되고 브라우저가 열린다.
# 이 터미널 창을 닫거나 Ctrl+C 를 누르면 뷰어가 종료된다.
cd "$(dirname "$0")" || exit 1
clear
echo "  마크다운 뷰어를 실행합니다. 이 창을 닫으면 종료됩니다."
echo
exec python3 -u server.py
