# Деплой AlfaCyberCup

## Как это работает сейчас (CI/CD)

Пуш или мердж PR в `main` → GitHub Actions (`.github/workflows/deploy.yml`)
подключается по SSH к серверу под пользователем `deploy` и запускает
`deploy/deploy.sh`. Скрипт: `git pull` → `pip install -r requirements.txt` →
`alembic upgrade head` → `systemctl restart alfacybercup`.

Коллаборатору для деплоя не нужен доступ к серверу — только права мержить в `main`.

### Пользователь `deploy` устроен так специально

У пользователя `deploy` на сервере обычный `/bin/bash`, но в
`~/.ssh/authorized_keys` у его единственного ключа стоит forced command:

```
command="sudo /opt/alfacybercup/deploy/deploy.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...
```

Что бы ни передали в `ssh deploy@сервер <любая команда>` — выполнится всегда
только `deploy.sh`, ничего больше. В `/etc/sudoers.d/deploy-cd` разрешён
`sudo` только на этот же файл (`NOPASSWD: /opt/alfacybercup/deploy/deploy.sh`).
Даже если приватный ключ утечёт, максимум, что можно сделать — вызвать
редеплой из main. Полноценного шелла или root-доступа через этот ключ нет.

### Секрет в GitHub

В настройках репозитория (Settings → Secrets and variables → Actions →
environment `production`) должен быть один секрет:

- `DEPLOY_SSH_KEY` — приватный ключ пользователя `deploy` целиком
  (`-----BEGIN OPENSSH PRIVATE KEY-----...`).

Публичный host key сервера зашит прямо в workflow (это не секрет, менять
его не нужно, пока не переустанавливали ОС на сервере — тогда см. ниже).

## Если сервер снова "сломался" (переустановка ОС)

Уже бывало один раз. Полный бутстрап с нуля:

```bash
# 1. Пакеты
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx sqlite3 python3.12-venv git

# 2. Системный пользователь под приложение
useradd -r -m -d /opt/alfacybercup -s /usr/sbin/nologin alfacybercup
rm -f /opt/alfacybercup/.bash_logout /opt/alfacybercup/.bashrc /opt/alfacybercup/.profile
chmod o+x /opt/alfacybercup   # иначе nginx не сможет читать /static (403)

# 3. Код
cd /opt/alfacybercup
sudo -u alfacybercup git clone https://github.com/Kzkovich/Cupcup.git .
sudo -u alfacybercup python3.12 -m venv .venv
sudo -u alfacybercup .venv/bin/pip install -r requirements.txt

# 4. .env — сгенерировать новый SECRET_KEY, остальное см. .env.example
#    ADMIN_EMAILS=kzkovich@gmail.com — эта почта получает права админа
#    автоматически при первой регистрации на сайте
chmod 600 /opt/alfacybercup/.env
chown alfacybercup:alfacybercup /opt/alfacybercup/.env

# 5. База
mkdir -p /opt/alfacybercup/data && chown alfacybercup:alfacybercup /opt/alfacybercup/data
sudo -u alfacybercup /opt/alfacybercup/.venv/bin/alembic upgrade head

# 6. systemd
cp /opt/alfacybercup/deploy/alfacybercup.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now alfacybercup

# 7. nginx (сначала http-заглушка на порт 80, проксирующая на 127.0.0.1:8090),
#    затем: certbot --nginx -d cup.kzkovich.ru --non-interactive --agree-tos \
#           -m kzkovich@gmail.com --redirect
#    certbot сам допишет SSL-блок в конфиг

# 8. Бэкапы (см. /opt/backups/alfacybercup-backup.sh + crontab -l)

# 9. Пользователь deploy для CI — см. раздел выше: создать заново,
#    сгенерировать новую пару ключей, положить публичный в authorized_keys
#    с forced command, приватный — в секрет DEPLOY_SSH_KEY на GitHub,
#    новый host key сервера — в workflow вместо старого (ssh-keyscan -t ed25519 <ip>)
```

Порт приложения: `127.0.0.1:8090`. Сервер: `194.59.221.90` / `cup.kzkovich.ru`.
Это общий VPS — там же живут другие проекты (`cf.kzkovich.ru`,
`podelim.kzkovich.ru`, 3x-ui и т.д.), их бутстрап-процедура не связана с этой.
