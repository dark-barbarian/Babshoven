## Start bot script
`nano /home/botuser/DiscordBots/start_bot.sh`
```bash
#!/bin/bash

BOT_NAME="$1"
BASE_DIR="/home/botuser/DiscordBots/$BOT_NAME"

# Activate venv
source "$BASE_DIR/venv/bin/activate"

# Run the bot
exec python "$BASE_DIR/main.py"
```
`chmod +x /home/botuser/DiscordBots/start_bot.sh`

---

## Service File  
`sudo nano /etc/systemd/system/discordbot@.service`
```ini
[Unit]
Description=Discord Bot - %i
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/DiscordBots/%i
ExecStart=/home/botuser/DiscordBots/start_bot.sh %i
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
MemoryAccounting=true
CPUAccounting=true

[Install]
WantedBy=multi-user.target
```

---

## Enable & Start Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable discordbot@Babshoven
sudo systemctl start discordbot@Babshoven
```

---

## Check Status & Logs
```bash
sudo systemctl status discordbot@Babshoven
journalctl -u discordbot@Babshoven -f
journalctl -u discordbot@Babshoven -n 50 --no-pager
```

---

## Restart & Stop
```bash
sudo systemctl restart discordbot@Babshoven
sudo systemctl stop discordbot@Babshoven
sudo systemctl disable discordbot@Babshoven
```

---

## List All Services
```bash
systemctl list-units --type=service
```
