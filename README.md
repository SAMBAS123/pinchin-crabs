# DegenUnstableCannon (Telegram Raid Bot)

A minimal, modular Telegram raid bot built with pyTelegramBotAPI (telebot).

## Features
- Detects `x.com` / `twitter.com` links or `/raid <link>`
- Replies with "RAID TARGET LOCKED" + the link
- Drops 6 random bullish $USDUC copypastas
- Two buttons: `I'M IN 🔥` and `More Ammo`
- Unique raider counting per raid (`I'M IN 🔥`)
- `More Ammo` returns 5 new copypastas
- .env-driven config, simple and modular

## Windows Setup
1) Install Python 3.10+ from Microsoft Store or python.org
2) Open PowerShell in this folder
3) Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

4) Install dependencies:

```powershell
pip install -r requirements.txt
```

5) Add your Telegram bot token to `.env`:

```
BOT_TOKEN=123456789:ABC-your-botfather-token
```

6) Run the bot:

```powershell
python -m degen_unstable_cannon
```

Notes:
- Counts are kept in-memory (reset if the bot restarts).
- Buttons are inline and work per raid message.