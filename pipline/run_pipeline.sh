#!/usr/bin/env bash
set -e

# Запускает оба скрипта параллельно:
# 1) addres-sayta_pipeline.py добывает телефон+сайт и пишет leads_queue.jsonl
# 2) bulk_site_pitch_telega_queue_worker.py слушает эту очередь и обрабатывает новые лиды

QUEUE_FILE="leads_queue.jsonl"

python3 addres-sayta_pipeline.py &
ADDR_PID=$!

echo "[RUNNER] addres-sayta_pipeline.py запущен, PID=$ADDR_PID"
echo "[RUNNER] Запускаю bulk_site_pitch_telega_queue_worker.py в режиме ожидания очереди"

python3 bulk_site_pitch_telega_queue_worker.py --queue "$QUEUE_FILE" --watch "$@"

wait "$ADDR_PID"
