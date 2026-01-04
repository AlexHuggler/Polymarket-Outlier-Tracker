# Polymarket Whale Monitor

A Python tool to monitor Polymarket prediction markets for **"Fresh Whale"** activity - detecting when newly created accounts suddenly invest large sums (>$10,000 USD) into markets.

## Features

- Real-time monitoring via Polymarket Subgraph (GraphQL)
- Detects large trades from new/inactive accounts
- Discord webhook notifications with rich embeds
- Configurable detection thresholds
- **Historical backtest** for validating detection logic
- CSV export for analysis
- Available as both Python script and Google Colab notebook

## Quick Start

### Option 1: Google Colab (Easiest)

1. Open `Polymarket_Whale_Monitor.ipynb` in Google Colab
2. Run the installation cell
3. Configure your Discord webhook URL
4. Start the monitor

### Option 2: Local/VPS Deployment

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/Polymarket-Outlier-Tracker.git
cd Polymarket-Outlier-Tracker

# Install dependencies
pip install -r requirements.txt

# Set your Discord webhook
export DISCORD_WEBHOOK_URL="your_webhook_url_here"

# Run the monitor
python whale_monitor.py
```

## Configuration

### Command Line Arguments

```bash
python whale_monitor.py --help

Options:
  -w, --webhook URL       Discord webhook URL
  -m, --min-value USD     Minimum trade value (default: 10000)
  -i, --interval SEC      Polling interval in seconds (default: 60)
  -t, --max-trades N      Max historical trades for "new account" (default: 5)
  -a, --account-hours H   Account age threshold in hours (default: 72)
  --debug                 Enable debug logging
  --test                  Run single poll cycle then exit
```

### Examples

```bash
# Monitor trades over $50,000 from accounts with < 3 trades
python whale_monitor.py --min-value 50000 --max-trades 3

# Fast polling (every 30 seconds) with debug output
python whale_monitor.py --interval 30 --debug

# Test run (single poll)
python whale_monitor.py --test
```

## Detection Logic

An account is flagged as a **"Fresh Whale"** if it makes a trade over the threshold AND meets either:

1. **Few Historical Trades**: Has fewer than 5 total trades on Polymarket, OR
2. **New Account**: First trade was within the last 72 hours

This catches:
- Brand new wallets making large first bets
- Dormant wallets suddenly becoming active
- Potential insider activity or informed trading

## Historical Backtest (Validation)

The backtest module lets you analyze historical trades to validate the detection logic works correctly. This is essential for:

- Verifying the Fresh Whale detection criteria
- Analyzing patterns in past whale activity
- Generating reports for research

### Running a Backtest

```bash
# Analyze last 7 days (default)
python whale_monitor.py backtest

# Or run backtest.py directly
python backtest.py --days 7

# Analyze last 30 days with higher threshold
python backtest.py --days 30 --min-value 50000

# Export results to CSV
python backtest.py --days 14 --output fresh_whales.csv

# Export ALL large trades (for further analysis)
python backtest.py --days 7 --output-all all_trades.csv --output whales_only.csv
```

### Backtest Options

```bash
python backtest.py --help

Options:
  -d, --days N          Days to look back (default: 7)
  -m, --min-value USD   Minimum trade value (default: 10000)
  -t, --max-trades N    Max prior trades for "new account" (default: 5)
  -a, --account-hours H Account age threshold in hours (default: 72)
  -o, --output FILE     Export Fresh Whales to CSV
  --output-all FILE     Export ALL analyzed trades to CSV
  --debug               Enable debug logging
```

### Backtest Output

The backtest generates a detailed summary:

```
======================================================================
BACKTEST RESULTS SUMMARY
======================================================================
Period: 2024-01-01 to 2024-01-07
Minimum trade value: $10,000
Fresh account criteria: <5 trades OR <72h old
----------------------------------------------------------------------
Total trades fetched: 1,234
Large trades analyzed: 156
Fresh Whales detected: 23
Unique whale addresses: 19
Total whale volume: $847,500.00
Average whale trade size: $36,847.83

----------------------------------------------------------------------
TOP 10 LARGEST FRESH WHALE TRADES:
----------------------------------------------------------------------
1. $125,000.00 - 2024-01-05 14:32:00 UTC
   Market: Will Bitcoin reach $50,000 by March?
   Position: Yes @ $0.650
   Reason: Only 2 prior trades
   Wallet: 0x1234abcd...ef567890
...
```

### CSV Export Format

The exported CSV includes:

| Column | Description |
|--------|-------------|
| timestamp | Human-readable trade time |
| value_usd | Trade value in USD |
| market_title | Market question |
| outcome | Yes/No position |
| price | Share price at purchase |
| user_address | Wallet address |
| account_trades_before | Trades BEFORE this one |
| account_age_hours | Account age at time of trade |
| is_fresh_whale | Detection result |
| detection_reason | Why flagged |
| profile_url | Link to Polymarket profile |

### Key Difference: Point-in-Time Analysis

The backtest analyzes account state **at the time of each trade**, not the current state. This means:

- If an account had 2 trades when they made a $50k bet, it shows "2 prior trades"
- Even if that account now has 100 trades, the historical analysis is accurate
- This is crucial for validating that real-time detection would have caught them

## Getting a Discord Webhook URL

1. Open Discord and go to your server
2. Right-click on the channel where you want alerts
3. Select **"Edit Channel"**
4. Go to **"Integrations"** in the left sidebar
5. Click **"Webhooks"**
6. Click **"New Webhook"**
7. Give it a name (e.g., "Whale Monitor")
8. Click **"Copy Webhook URL"**

The URL will look like:
```
https://discord.com/api/webhooks/1234567890/abcdefghijklmnop...
```

## Discord Alert Format

When a Fresh Whale is detected, you'll receive an alert like:

```
[Fresh Whale Detected!]

Market: Will Bitcoin reach $100,000 by end of 2024?
Position: Yes @ $0.450
Amount Invested: $25,000.00
Shares Purchased: 55,555.56
Account Age: 2.5 hours
Total Prior Trades: 1
Detection Reason: Only 1 prior trades
Wallet: 0x1234abcd...ef567890
```

Color coding:
- Green: $10,000 - $49,999
- Orange: $50,000 - $99,999
- Red: $100,000+

## Running 24/7

### Google Colab Limitations

| Issue | Description |
|-------|-------------|
| Session Timeouts | Disconnects after ~90 min idle, ~12 hr max runtime |
| Browser Required | Stops when tab is closed |
| IP Changes | Colab IPs rotate, can affect rate limiting |
| No Persistence | State is lost on disconnect |

### VPS Deployment (Recommended for 24/7)

#### Using systemd (Linux)

Create `/etc/systemd/system/whale-monitor.service`:

```ini
[Unit]
Description=Polymarket Whale Monitor
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/Polymarket-Outlier-Tracker
Environment=DISCORD_WEBHOOK_URL=your_webhook_url
ExecStart=/usr/bin/python3 whale_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable whale-monitor
sudo systemctl start whale-monitor
sudo systemctl status whale-monitor
```

#### Using nohup (Simple)

```bash
nohup python whale_monitor.py > whale_monitor.log 2>&1 &
```

#### Using screen

```bash
screen -S whale-monitor
python whale_monitor.py
# Press Ctrl+A, then D to detach
# Use `screen -r whale-monitor` to reattach
```

## Data Source

This tool uses the **Polymarket Subgraph** via The Graph protocol:

```
https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-subgraph/prod/gn
```

The subgraph provides:
- Real-time trade data
- User trading history
- Market information

## Project Structure

```
Polymarket-Outlier-Tracker/
├── whale_monitor.py              # Main Python script (real-time monitoring)
├── backtest.py                   # Historical backtest module
├── Polymarket_Whale_Monitor.ipynb # Google Colab notebook
├── requirements.txt              # Python dependencies
├── .env.example                  # Environment variable template
└── README.md                     # This file
```

## Troubleshooting

### "No trades found"
- The subgraph schema may have changed
- Check if the Polymarket subgraph is operational
- Try running with `--debug` for more info

### Discord alerts not sending
- Verify your webhook URL is correct
- Check that the webhook hasn't been deleted
- Ensure the bot has permission to post in the channel

### Rate limiting
- Increase `--interval` to poll less frequently
- The default 60-second interval should be safe

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Disclaimer

This tool is for **educational and research purposes only**.

- Not financial advice
- No guarantees of accuracy
- Use at your own risk
- Respect Polymarket's terms of service

## License

MIT License - see LICENSE file for details.
