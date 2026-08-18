#!/bin/bash
# Обновление уже развёрнутого приложения на сервере: git pull + миграции + рестарт.
# Первоначальный деплой делается вручную по deploy/README.md.
set -euo pipefail
cd /opt/alfacybercup
sudo -u alfacybercup git pull
sudo -u alfacybercup /opt/alfacybercup/.venv/bin/pip install -q -r requirements.txt
sudo -u alfacybercup /opt/alfacybercup/.venv/bin/alembic upgrade head
systemctl restart alfacybercup
systemctl status alfacybercup --no-pager -l | head -10
