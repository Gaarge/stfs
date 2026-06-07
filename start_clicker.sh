#!/usr/bin/env bash

export YDOTOOL_SOCKET=/run/user/1000/.ydotool_socket

X=1050
Y=1157

# Сообщение и небольшая задержка, чтобы успеть открыть окно
echo "Запуск скрипта. Убедитесь, что нужное окно активно."
sleep 1

# Наводим мышь и кликаем
ydotool mousemove -a "$X" "$Y"
sleep 0.2
ydotool click 0xC0
sleep 0.2

# Пишем команду /start и нажимаем Enter
ydotool type "/start"
sleep 0.1
ydotool key 28:1 28:0

echo "Команда /start отправлена."
