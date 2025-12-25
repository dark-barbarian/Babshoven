## Start bot script
```bash
nano /home/botuser/DiscordBots/start_bot.sh
```
```bash
#!/bin/bash

BOT_NAME="$1"
BASE_DIR="/home/botuser/DiscordBots/$BOT_NAME"

# Activate venv
source "$BASE_DIR/venv/bin/activate"

# Run the bot
exec python "$BASE_DIR/main.py"
```
```bash
chmod +x /home/botuser/DiscordBots/start_bot.sh
```

---

## Environment File  
```bash
nano /etc/Babshoven.env
```
```ini
DISCORD_TOKEN=xxx
```
```bash
chmod 600 /etc/Babshoven.env
chown root:root /etc/Babshoven.env
```

---

## Environment File  
`sudo nano /etc/Babshoven.env`
```ini
DISCORD_TOKEN=xxx
```
`sudo chmod 600 /etc/Babshoven.env`

`sudo chown root:root /etc/Babshoven.env`

---

## Service File  
```bash
nano /etc/systemd/system/discordbot@.service
```
```ini
[Unit]
Description=Discord Bot - %i
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/DiscordBots/%i
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/%i.env
ExecStart=/home/botuser/DiscordBots/start_bot.sh %i
Restart=always
RestartSec=5
MemoryAccounting=true
CPUAccounting=true

[Install]
WantedBy=multi-user.target
```

---

## Enable & Start Service
```bash
systemctl daemon-reload
systemctl enable --now discordbot@Babshoven
```

---

## Check Status & Logs
```bash
systemctl status discordbot@Babshoven
journalctl -u discordbot@Babshoven -f
journalctl -u discordbot@Babshoven -n 50 --no-pager
```

---

## Restart & Stop
```bash
systemctl restart discordbot@Babshoven
systemctl stop discordbot@Babshoven
systemctl disable discordbot@Babshoven
```

---

## List All Services
```bash
systemctl list-units --type=service
```
