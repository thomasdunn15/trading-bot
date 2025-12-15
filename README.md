# Topstep Trading Bot

A Python-based automated trading bot that connects TradingView alerts to the Topstep trading platform via webhooks. The bot receives signals, places orders, and manages positions with trailing stops.

## Features

- **Webhook Server**: Flask-based server that receives TradingView alerts
- **TopStep Integration**: Full API integration for order placement and position management
- **Real-time Quotes**: WebSocket connection for live market data via SignalR
- **Trailing Stops**: Automatic trailing stop placement triggered by ATR-based price levels
- **Position Management**: Handles entries, exits, and position reversals
- **ngrok Tunneling**: Built-in ngrok integration for exposing webhooks to the internet
- **Auto-reconnect**: Handles market hours (pauses 4-6pm ET) and connection recovery

## Prerequisites

- Python 3.11+
- TopStep trading account with API access
- ngrok account (free tier works) for webhook tunneling
- TradingView account for sending alerts

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/trading-bot.git
   cd trading-bot
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` with your credentials:
   ```
   TOPSTEP_USERNAME=your_username
   TOPSTEP_API_KEY=your_api_key
   NGROK_AUTHTOKEN=your_ngrok_token
   NGROK_DOMAIN=your-subdomain.ngrok-free.app
   ```

5. **Download ngrok**
   - Download ngrok from https://ngrok.com/download
   - Place `ngrok.exe` (Windows) or `ngrok` (Mac/Linux) in the project directory
   - Authenticate: `ngrok config add-authtoken YOUR_TOKEN`

## Usage

### Starting the Bot

```bash
python server.py
```

The bot will:
1. Start the ngrok tunnel
2. Authenticate with TopStep
3. Connect to the real-time quote feed
4. Display the webhook URL to use in TradingView

### TradingView Alert Setup

Configure your TradingView alerts to send webhooks to:
```
https://your-subdomain.ngrok-free.app/webhook
```

#### Alert Message Format

**Entry Alert:**
```json
{
  "ticker": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "price": {{strategy.order.price}},
  "qty": {{strategy.order.contracts}},
  "comment": "entry|atr={{plot_0}}",
  "time": {{timenow}}
}
```

**Exit Alert:**
```json
{
  "ticker": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "price": {{close}},
  "qty": {{strategy.order.contracts}},
  "comment": "exit",
  "time": {{timenow}}
}
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhook` | POST | Receive TradingView alerts |
| `/health` | GET | Health check endpoint |

## Configuration

Key settings in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `default_contract_symbol` | `MNQZ5` | Default futures contract |
| `tick_size` | `0.25` | NQ tick size |
| `close_holdoff_ms` | `1500` | Delay after close before new entries |
| `flask_port` | `5000` | Server port |

## Project Structure

```
Trading Bot/
├── server.py           # Main Flask server and webhook handler
├── api.py              # TopStep REST API client
├── topstep_ws.py       # WebSocket client for real-time quotes
├── config.py           # Configuration management
├── utils.py            # Utility functions
├── logging_setup.py    # Logging configuration
├── ngrok_helper.py     # ngrok tunnel management
├── requirements.txt    # Python dependencies
├── .env.example        # Environment template
└── .gitignore          # Git ignore rules
```

## How It Works

1. **Entry Signal**: TradingView sends a webhook with entry details and ATR value
2. **Order Placement**: Bot places a limit order at the specified price
3. **Trigger Monitoring**: Real-time quotes monitor for ATR-based trigger price
4. **Trailing Stop**: When trigger is hit, a trailing stop is placed
5. **Exit Signal**: Position closes via trailing stop or explicit exit webhook

## Trading Logic

- **Flip Detection**: Size of 8 contracts indicates a position reversal
- **Failsafes**: Timestamp and holdoff checks prevent duplicate entries
- **Market Hours**: Pauses WebSocket during CME maintenance (4-6pm ET)
- **Token Refresh**: Automatic re-authentication every 2 hours

## Troubleshooting

**ngrok not starting:**
- Ensure `ngrok.exe` is in the project directory
- Verify your authtoken is configured
- Check if port 5000 is available

**Authentication failing:**
- Verify your TopStep credentials in `.env`
- Check if your API key is still valid

**WebSocket disconnecting:**
- Normal during 4-6pm ET (CME maintenance)
- Bot will auto-reconnect after maintenance window

## Disclaimer

⚠️ **USE AT YOUR OWN RISK**

This software is for educational purposes only. Trading futures involves substantial risk of loss. Past performance is not indicative of future results. Always test thoroughly in a simulation environment before using with real funds.

## License

MIT License - See LICENSE file for details
