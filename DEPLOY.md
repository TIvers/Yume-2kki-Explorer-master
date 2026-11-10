# Развёртывание на Ubuntu/Debian

Целевая схема: Node.js-приложение и MySQL на одном сервере, Nginx принимает HTTPS-запросы для `yume2kki.iversdev.ru` и проксирует их на локальный порт приложения. Порт приложения и MySQL наружу открывать не нужно.

## 1. DNS и доступ к серверу

В DNS-зоне `iversdev.ru` создайте запись `A` для `yume2kki` с IPv4-адресом сервера. Добавляйте `AAAA` только если на сервере действительно настроен доступный извне IPv6.

В сетевом экране сервера разрешите входящие порты `22`, `80` и `443`. Не открывайте `3306` и `5000` наружу. После обновления DNS проверьте, что `yume2kki.iversdev.ru` указывает на этот сервер.

## 2. Установка системных пакетов

Подключитесь к серверу через Termius по SSH и выполните:

```sh
sudo apt update
sudo apt install -y nginx mysql-server certbot python3-certbot-nginx curl ca-certificates
```

Для Node.js 20 установите официальный пакет NodeSource (если Node.js 20 ещё не установлен):

```sh
curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/nodesource_setup.sh
sudo -E bash /tmp/nodesource_setup.sh
sudo apt install -y nodejs
rm /tmp/nodesource_setup.sh
node --version
```

Убедитесь, что версия Node.js — 20.x (в `package.json` указана Node.js `^20`).

## 3. Загрузка файлов проекта

Создайте отдельного системного пользователя и каталог приложения:

```sh
sudo useradd --system --user-group --home-dir /opt/yume2kki --create-home --shell /usr/sbin/nologin yume2kki
mkdir -p "$HOME/yume2kki-upload"
```

В Termius SFTP загрузите файлы проекта в `~/yume2kki-upload`. Не загружайте `node_modules`, локальную `venv`, `.git` и локальные `config/*.js`. Если изображения должны загружаться только с сервера, передайте также `public/images` со всеми используемыми файлами: без них картинки будут недоступны. Не удаляйте `data-fallback.json`, `package-lock.json`, `index.html`, `app.js`, каталоги `src` и `public`.

Скопируйте проект и установите зависимости:

```sh
sudo cp -a "$HOME/yume2kki-upload/." /opt/yume2kki/
sudo chown -R yume2kki:yume2kki /opt/yume2kki
sudo -u yume2kki sh -c 'cd /opt/yume2kki && npm ci'
```

## 4. Создание базы MySQL

Сгенерируйте пароль БД и сохраните его: он понадобится и в MySQL, и в файле приложения.

```sh
openssl rand -hex 32
```

Откройте консоль MySQL:

```sh
sudo mysql
```

Создайте БД и пользователя, заменив `ПАРОЛЬ_ИЗ_СЛУЧАЙНОЙ_СТРОКИ` на сгенерированный пароль:

```sql
CREATE DATABASE yume2kki CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'yume2kki'@'localhost' IDENTIFIED BY '8d174ee6d70902f2e9a5e5b1ffe1ef4b4ce06ae2d0dcc0018212bf42bd372858';
GRANT ALL PRIVILEGES ON yume2kki.* TO 'yume2kki'@'localhost';
EXIT;
```

Приложение создаёт таблицы при первом запуске; отдельный SQL-дамп для пустой БД не нужен.

## 5. Настройка секретов и systemd

Сгенерируйте случайный ключ администратора:

```sh
openssl rand -hex 32
```

Создайте файл на основе [deploy/yume2kki.env.example](./deploy/yume2kki.env.example) и замените оба заполнителя реальными значениями:

```sh
sudo cp /var/www/Yume-2kki-Explorer-master/deploy/yume2kki.env.example /etc/yume2kki.env
sudo nano /etc/yume2kki.env
```

Укажите сгенерированный ключ в `ADMIN_KEY`, а тот же пароль, который задали MySQL-пользователю, — в `DATABASE_PASS`. Оставьте `DATABASE_NAME=yume2kki` и `DATABASE_USER=yume2kki`. `BOT_USERNAME` и `BOT_PASSWORD` оставьте пустыми, если не настраивали бота Wiki.

Чтобы использовать только локальные изображения, добавьте в `/etc/yume2kki.env` строку `USE_LOCAL_IMAGES=true` и загрузите файлы изображений в `/opt/yume2kki/public/images`. При отсутствии файла приложение не будет подставлять внешний URL, поэтому соответствующее изображение будет недоступно, пока файл не появится на диске.

Ограничьте доступ к файлу с секретами, установите службу и запустите приложение:

```sh
sudo chown root:root /etc/yume2kki.env
sudo chmod 600 /etc/yume2kki.env
sudo cp /var/www/Yume-2kki-Explorer-master/deploy/yume2kki.service /etc/systemd/system/yume2kki.service
sudo systemctl daemon-reload
sudo systemctl enable --now yume2kki
sudo systemctl status yume2kki --no-pager
```

Проверьте журнал и локальный ответ сервера:

```sh
sudo journalctl -u yume2kki -n 100 --no-pager
curl -I http://127.0.0.1:5000/
```

## 6. Nginx и HTTPS

Установите конфигурацию сайта, проверьте её и выпустите сертификат:

```sh
sudo cp /opt/yume2kki/deploy/nginx.conf /etc/nginx/sites-available/yume2kki
sudo ln -s /etc/nginx/sites-available/yume2kki /etc/nginx/sites-enabled/yume2kki
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d yume2kki.iversdev.ru
```

Certbot настроит HTTPS и перенаправление с HTTP. Откройте `https://yume2kki.iversdev.ru/` для проверки.

При первом открытии приложение автоматически создаст таблицы и запустит первоначальную загрузку данных из Yume 2kki Wiki. Она может занять несколько минут; статус можно смотреть командой `sudo journalctl -u yume2kki -f`.

## Автоматический деплой через GitHub Actions

Workflow `.github/workflows/deploy.yml` запускается при каждом push в `main`. Для него нужен self-hosted runner на этом же сервере с меткой `yume2kki-deploy`. Runner получает исходники из GitHub, собирает клиентский bundle, копирует файлы в `/var/www/Yume-2kki-Explorer-master/`, устанавливает production-зависимости и перезапускает службу.

Runner должен запускаться от пользователя `yume2kki`, который владеет каталогом приложения. Установите системные зависимости runner и `rsync`, затем подготовьте каталог runner:

```sh
sudo apt install -y rsync
sudo mkdir -p /opt/actions-runner
sudo chown -R yume2kki:yume2kki /opt/actions-runner
```

В настройках GitHub репозитория откройте **Settings → Actions → Runners → New self-hosted runner**, выберите Linux x64 и выполните показанные GitHub команды загрузки и настройки runner от пользователя `yume2kki`. При настройке добавьте пользовательскую метку `yume2kki-deploy`. Регистрационный token краткосрочный: не сохраняйте его в репозитории и не вставляйте в workflow. Установите runner как системную службу:

```sh
sudo -u yume2kki -H bash
cd /opt/actions-runner
# Выполните здесь download/configure команды из GitHub UI, добавив метку yume2kki-deploy.
exit
cd /opt/actions-runner
sudo ./svc.sh install yume2kki
sudo ./svc.sh start
sudo ./svc.sh status
```

Разрешите runner только запуск и остановку приложения, без полного доступа через `sudo`. Создайте правило:

```sh
sudo visudo -f /etc/sudoers.d/yume2kki-deploy
```

Добавьте в файл:

```text
yume2kki ALL=(root) NOPASSWD: /usr/bin/systemctl stop yume2kki, /usr/bin/systemctl start yume2kki
```

Проверьте правило и права:

```sh
sudo visudo -cf /etc/sudoers.d/yume2kki-deploy
sudo chmod 440 /etc/sudoers.d/yume2kki-deploy
```

Workflow специально не запускается для pull request. Ограничьте право на push в `main` только доверенными сопровождающими: код, попавший в эту ветку, выполняется на сервере. Во время обновления приложение кратковременно останавливается на время установки production-зависимостей. Каталоги `config/` и `public/images/` не перезаписываются.

## Ручное обновление приложения

Загрузите новые файлы в `~/yume2kki-upload`, затем выполните:

```sh
sudo cp -a "$HOME/yume2kki-upload/." /opt/yume2kki/
sudo chown -R yume2kki:yume2kki /opt/yume2kki
sudo -u yume2kki sh -c 'cd /opt/yume2kki && npm ci'
sudo systemctl restart yume2kki
```

Не перезаписывайте `/etc/yume2kki.env`. Перед обновлением сохраните копию MySQL, если в базе есть важные пользовательские данные.

## Полезные команды

```sh
sudo systemctl status yume2kki --no-pager
sudo journalctl -u yume2kki -f
sudo nginx -t
sudo systemctl reload nginx
sudo certbot renew --dry-run
```
