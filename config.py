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
# We don't fire once at the last second (a speed race we can't win). Instead we
# watch the whole back half of the window and take a favorable offer whenever it
# appears (resting/marketable limit order behaviour). ENTRY_START = seconds into
# the window before we start looking; ENTRY_CUTOFF = stop this many seconds
# before close (leave time for a real fill).
ENTRY_START_SEC = 255            # only hunt in the last ~45s (was 120 = too
                                 # early; outcomes reversed and we lost). Later
                                 # entry = outcome more settled, fewer reversals.
ENTRY_CUTOFF_SEC = 8             # stop hunting this many seconds before close
RESOLVE_BUFFER_SEC = 8           # wait this long after close before resolving

# --- Brownian probability model ---
# P(up at settlement) = Phi( move / sigma_remaining ), where move = current
# price - price_to_beat, and sigma_remaining is the $ volatility over the
# remaining seconds. Volatility is estimated from recent oracle ticks.
VOL_LOOKBACK_SEC = 120           # window for per-second volatility estimate
MIN_SIGMA = 1.0                  # floor on per-second sigma ($) to avoid /0

# --- Betting / edge ---
# Only bet when the outcome is NEARLY SETTLED (our model is confident) AND the
# lagging market still misprices it. Betting a cheap side with no real
# information (P~0.5) is just gambling, so we require high confidence first.
MIN_CONFIDENCE = 0.85            # only buy if model prob for the side >= this
MIN_EDGE = 0.05                  # max price we'll pay = model_prob - MIN_EDGE
TRADE_COST_USD = 0.0             # per-trade fee/gas estimate (Polymarket CLOB
                                 # currently ~0; bump if you observe fees)

# --- Sizing (fractional Kelly, capped) ---
KELLY_FRACTION = 0.5             # half-Kelly
MAX_STAKE_FRAC = 0.05            # never risk more than 5% of bankroll per bet
MIN_STAKE = 1.0                  # skip if Kelly stake below this

# --- Paper bankroll ---
STARTING_BANKROLL = 100.0
BANKROLL_FILE = "bankroll.json"            # "HOLD" strategy (buy & hold to settle)

# --- Stop-loss / early-exit experiment ---
# We run a second virtual portfolio on the SAME entries that, instead of
# holding to settlement, SELLS the position back into the order book if the
# outcome turns against us. This lets us compare HOLD vs STOP head-to-head.
BANKROLL_STOP_FILE = "bankroll_stop.json"  # "STOP" strategy
EXIT_PROB = 0.45        # if our side's win-prob falls below this, sell to cut loss
