# magical_org – Telegram Signal Poster

A Telegram bot with an inline admin panel for publishing trading signals. Send a signal to the bot, and the bot posts it to your channel, tracks the live price, and replies in the channel at every stage of the trade. No user account or session is needed: just make the bot an admin of your channel.

## Quick Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/magical_org/main/magical_org.sh)
```

The installer installs the bot in `/opt/magical_org` and opens a menu. Select `1) Install`. The installer will:

1. Install system dependencies such as Python 3.10+ and `python3-venv`.
2. Download the repository to `/opt/magical_org`.
3. Create a Python virtual environment and install the packages from `requirements.txt`.
4. Ask for the bot token, admin IDs and timezone, then write them to `.env`.
5. Create and start the `magical_org` systemd service.

The systemd service runs the bot from `/opt/magical_org`.

After installation, verify that the service is running:

```bash
systemctl status magical_org
```

> The installer must run as `root` on a Linux server with `systemd`. The repository must be public and `magical_org.sh` must be in the root of the `main` branch.

## Installation Path

The default installation path is:

```text
/opt/magical_org
```

Project files, the Python virtual environment, `.env` and the SQLite database `data.db` are stored in this directory.

## Manual Installation

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

git clone https://github.com/Mahersaber2024/magical_org.git
cd magical_org

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cat > .env <<'ENV'
BOT_TOKEN=123456:ABC-your-bot-token
ADMIN_IDS=12345,67890
TIMEZONE=Asia/Tehran
DB_FILE=data.db
ENV

python3 magical_org.py
```

## Configuration

`.env` is created by the installer. To change the token or the admins later, use `6) Change token / admins` in the installer menu, or edit the file and restart the service.

| Key | Description | Default |
| --- | --- | --- |
| `BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) | – |
| `ADMIN_IDS` | Owner Telegram user IDs, comma separated | – |
| `TIMEZONE` | Timezone for the Jalali date shown in posts | `Asia/Tehran` |
| `DB_FILE` | Path of the SQLite database | `data.db` |
| `DATA_FILE` | Old JSON file, imported into the database once if it exists | `data.json` |

## Service Management

```bash
systemctl start magical_org
systemctl stop magical_org
systemctl restart magical_org
systemctl status magical_org
```

View logs:

```bash
journalctl -u magical_org -f
```

The installer script offers the same actions:

```bash
bash /opt/magical_org/magical_org.sh restart
bash /opt/magical_org/magical_org.sh status
bash /opt/magical_org/magical_org.sh logs
```

## Update

Run the installer again and select `2) Update`, or:

```bash
bash /opt/magical_org/magical_org.sh update
```

Updating keeps your `.env` and `data.db`.

## Uninstall

Run the installer and select `7) Uninstall`, or:

```bash
bash /opt/magical_org/magical_org.sh uninstall
```

You can choose whether to delete only the service or everything in `/opt/magical_org` (including `.env` and `data.db`).

## Bot Commands

- `/menu` – Same as `/start`.
- `/start` – Open the admin panel. A «منو» button appears on Telegram's keyboard; tap it any time to open the main menu.
- `/help` – Show help.

All other operations are available through the inline menu.

## Admin Panel

The main menu has these buttons:

| Button | What it does |
| --- | --- |
| 📋 Active Trades | Live list with current price and R, cancel pending or close open trades |
| 📈 Stats | Total R, win rate, best/worst trade, per-channel results |
| 📊 Report Post | Post a performance summary of closed trades to a channel |
| 📺 Channels | Add, test (bot admin/post permission) or remove channels |
| ⚙️ Settings | Reward steps, break-even rule, price check interval, auto report |
| 🛡 Admin Panel | Manage admins, system status, backup, purge history, restart |
| ❓ Help | Usage guide |

Destructive actions (delete, cancel, close, restart) ask for confirmation.

### Admin roles

- **Owners** are the IDs in `ADMIN_IDS` of `.env`. They can add or remove admins, download backups, purge history and restart the bot.
- **Admins** are added from the bot (🛡 Admin Panel → 👥 Admins → ➕). They can use the bot but cannot manage other admins.
- Anyone else who sends `/start` sees their own Telegram ID, which they can give to an owner.

## Usage

1. Send `/start`.
2. Add the bot to your channel as an **administrator** with permission to post messages.
3. **Channels → Add channel**: send the channel `@username`, `-100…` ID or `t.me` link. The bot checks that it can post there.
4. Send a signal:

```text
btc
Int81000
Tp87000
Sl80000
```

5. A preview appears (Risk/Reward is calculated automatically). Choose the channel and the signal is posted.

## Trade Lifecycle

- 🟡 **Pending** is posted.
- Price reaches Entry → **Position Opened** (reply to the Pending post).
- Cancel from Active Trades → **Pending Order Cancelled** (reply to the same post).
- Price reaches a reward step (configurable) → **Reward 1R reached — In Profit**.
- Price reaches TP or SL → result post with the final R.
- **Report Post** → summary with each trade result, total R, win rate and collected rewards.

## Database

Channels, open positions, trade history, admins and settings are stored in a SQLite database (`data.db`). Nothing is lost when the bot restarts, crashes or is updated. If an old `data.json` exists, it is imported automatically on the first start and renamed to `data.json.migrated`.

## Auto Report

**Settings → 📅 Auto Report** posts a daily report to every channel at a time you choose (in `TIMEZONE`):

- **Time**: pick a preset or send any `HH:MM`.
- **Condition**: *Always* (whenever trades were closed that day) or *Profitable days only* (skipped when the day's total R is not positive).
- If the bot was offline at the scheduled time, the report is sent shortly after it starts.

## Live Price

Prices come from the public Binance API with a Bybit fallback. No personal API key is needed. Touch detection is as accurate as the check interval (default 5 seconds); a very short wick between two checks may not be seen.

## Project Structure

```text
magical_org.py    Config (.env), storage (SQLite data.db), menus/handlers, price monitor, auto report
trading.py        Signal parser, live price, Jalali date and number formatting, channel post templates
magical_org.sh    Install / update / uninstall menu
```

## Security

Never commit `.env` (it holds the bot token) or `data.db` to GitHub. The backup button exports a consistent copy of `data.db` only.
