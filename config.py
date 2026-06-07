"""Configuration for the Polymarket BTC 5-minute last-second bot.

Strategy (researched from profitable bots): do NOT predict BTC 5 minutes ahead
(that is ~a coin flip). Instead wait until the final seconds of each 5-minute
window, when the Chainlink price has nearly settled the outcome, and buy the
near-certain winning side IF Polymarket's lagging odds still misprice it.

Price + resolution both use Polymarket's own Chainlink BTC/USD oracle feed
(the exact source Polymarket settles with), via the public RTDS websocket.
No exchange, no API key.
"""

# --- Price feed (Polymarket's own Chainlink oracle) ---
RTDS_URL = "wss://ws-live-data.polymarket.com"
CHAINLINK_TOPIC = "crypto_prices_chainlink"
CHAINLINK_SYMBOL = "btc/usd"

# --- Market ---
WINDOW_SEC = 300                 # 5-minute windows
MARKET_SLUG_PREFIX = "btc-updown-5m"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# --- Strategy timing ---
DECISION_LEAD_SEC = 45           # decide this many seconds before window close
RESOLVE_BUFFER_SEC = 8           # wait this long after close before resolving

# --- Brownian probability model ---
# P(up at settlement) = Phi( move / sigma_remaining ), where move = current
# price - price_to_beat, and sigma_remaining is the $ volatility over the
# remaining seconds. Volatility is estimated from recent oracle ticks.
VOL_LOOKBACK_SEC = 120           # window for per-second volatility estimate
MIN_SIGMA = 1.0                  # floor on per-second sigma ($) to avoid /0

# --- Betting / edge ---
MIN_EDGE = 0.05                  # require model_prob - ask > this to bet
TRADE_COST = 0.0                 # extra modeled cost (ask already includes spread)

# --- Sizing (fractional Kelly, capped) ---
KELLY_FRACTION = 0.5             # half-Kelly
MAX_STAKE_FRAC = 0.05            # never risk more than 5% of bankroll per bet
MIN_STAKE = 1.0                  # skip if Kelly stake below this

# --- Paper bankroll ---
STARTING_BANKROLL = 100.0
BANKROLL_FILE = "bankroll.json"
