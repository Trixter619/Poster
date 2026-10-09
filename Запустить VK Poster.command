#!/bin/sh
cd "$(dirname "$0")" || exit 1
sh ./start.sh
result=$?
if [ "$result" -ne 0 ]; then
  printf '\nНе удалось запустить. Нажми Enter, чтобы закрыть окно. '
  read -r answer
fi
exit "$result"
