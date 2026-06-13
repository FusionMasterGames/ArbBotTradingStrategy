"""Best-effort asset-class + hedge-venue classifier for Hyperliquid symbols,
used to analyse where cross-venue funding opportunities concentrate.

Heuristic, not authoritative. Native perps (no ':' prefix) are crypto; HIP-3
symbols are matched against explicit TradFi sets and anything unmatched defaults
to crypto. Some tickers are genuinely ambiguous (GAS = nat-gas or the GAS token,
BIRD, QNT, CAR, DRAM, ...) and may be miscategorised — gap_stats prints the
members of each non-crypto class so they can be eyeballed and the sets fixed.

The point is the decision split: CRYPTO is hedgeable today on a CEX/DEX; the
TradFi classes each need a different (and much heavier) hedge venue.
"""

CRYPTO = "crypto"
EQUITY = "equity"
ETF = "etf"
COMMODITY = "commodity"
FX = "fx"
INDEX = "index"
BASKET = "basket"

HEDGE_VENUE = {
    CRYPTO:    "CEX/DEX spot — hedgeable today",
    EQUITY:    "Equity broker (e.g. IBKR)",
    ETF:       "Equity broker (e.g. IBKR)",
    COMMODITY: "Futures / commodity ETF (CME, ICE)",
    FX:        "FX spot or futures",
    INDEX:     "Index futures (CME, Eurex, ...)",
    BASKET:    "No single-instrument hedge — replicate or skip",
}

_BASKET = {"ANTHROPIC", "OPENAI", "BIOTECH", "DEFENSE", "ENERGY", "INFOTECH",
           "NUCLEAR", "ROBOT", "MAG7", "SEMIS", "AI", "BIGTECH"}
_ETF = {"EWJ", "EWT", "EWY", "EWZ", "KWEB", "SPY", "QQQ", "GLD", "IWM", "EEM", "XLF", "XLE"}
_INDEX = {"US500", "SPX", "US30", "DJI", "USTECH", "USTEC", "US100", "USA100", "NAS100",
          "NDX", "US2000", "SMALL2000", "RUSSELL", "JP225", "NIKKEI", "IBOV", "KR200",
          "KOSPI", "HSI", "DAX", "FTSE", "EU50", "XYZ100", "USA"}
_FX = {"EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNH", "MXN", "DXY"}
_COMMODITY = {"GOLD", "SILVER", "COPPER", "ALUMINIUM", "ALUMINUM", "PALLADIUM", "PLATINUM",
              "OIL", "USOIL", "BRENT", "BRENTOIL", "WTI", "CL", "NATGAS", "NGAS", "GAS",
              "CORN", "WHEAT", "SOY", "SOYBEAN", "COCOA", "COFFEE", "SUGAR", "USBOND"}
_EQUITY = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AMD", "AVGO",
           "ARM", "ASML", "BABA", "INTC", "MU", "DELL", "IBM", "ORCL", "CRM", "ADBE",
           "NFLX", "COIN", "HOOD", "GME", "DKNG", "EBAY", "COST", "BX", "BB", "ZM", "NOW",
           "HIMS", "CRCL", "CRWV", "NBIS", "WDC", "SMSN", "HYUNDAI", "USAR", "CAR",
           "PLTR", "MSTR", "RDDT", "UBER", "ABNB", "SHOP", "PYPL", "DIS", "BA", "JPM",
           "GS", "KO", "WMT", "MCD", "PFE", "XOM", "CVX"}

# checked in this order; sets are curated to not overlap
_ORDERED = ((_BASKET, BASKET), (_ETF, ETF), (_INDEX, INDEX), (_FX, FX),
            (_COMMODITY, COMMODITY), (_EQUITY, EQUITY))


def classify(market_name: str) -> tuple[str, str]:
    """Return (asset_class, hedge_venue) for a market name like 'xyz:TSLA'."""
    if ":" not in market_name:
        return CRYPTO, HEDGE_VENUE[CRYPTO]
    symbol = market_name.split(":")[-1].upper()
    for symbols, cls in _ORDERED:
        if symbol in symbols:
            return cls, HEDGE_VENUE[cls]
    return CRYPTO, HEDGE_VENUE[CRYPTO]
