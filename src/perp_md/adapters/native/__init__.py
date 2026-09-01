from __future__ import annotations

from perp_md.transport import JsonTransport

from . import (
    binance,
    bingx,
    bitfinex,
    btse,
    bybit,
    deepcoin,
    gate,
    grvt,
    htx,
    hyperliquid,
    kraken,
    kucoin,
    lighter,
    mexc,
    okx,
    phemex,
    toobit,
    xt,
)
from ._common import NativeAdapter
from .binance import BinanceAdapter
from .bingx import BingxAdapter
from .bitfinex import BitfinexAdapter
from .btse import BtseAdapter
from .bybit import BybitAdapter
from .deepcoin import DeepcoinAdapter
from .gate import GateAdapter
from .grvt import GrvtAdapter
from .htx import HtxAdapter
from .hyperliquid import HyperliquidAdapter
from .kraken import KrakenAdapter
from .kucoin import KucoinAdapter
from .lighter import LighterAdapter
from .mexc import MexcAdapter
from .okx import OkxAdapter
from .phemex import PhemexAdapter
from .toobit import ToobitAdapter
from .xt import XtAdapter


_NATIVE_ADAPTER_TYPES: tuple[tuple[str, type[NativeAdapter]], ...] = (
    ("BINANCE", BinanceAdapter),
    ("BINGX", BingxAdapter),
    ("BYBIT", BybitAdapter),
    ("GATE", GateAdapter),
    ("BITFINEX", BitfinexAdapter),
    ("DEEPCOIN", DeepcoinAdapter),
    ("KUCOIN", KucoinAdapter),
    ("HTX", HtxAdapter),
    ("TOOBIT", ToobitAdapter),
    ("PHEMEX", PhemexAdapter),
    ("GRVT", GrvtAdapter),
    ("LIGHTER", LighterAdapter),
    ("BTSE", BtseAdapter),
    ("XT", XtAdapter),
    ("OKX", OkxAdapter),
    ("HYPERLIQUID", HyperliquidAdapter),
    ("MEXC", MexcAdapter),
    ("KRAKEN", KrakenAdapter),
)


def native_adapters(transport: JsonTransport) -> dict[str, NativeAdapter]:
    return {
        venue: adapter_type(transport)
        for venue, adapter_type in _NATIVE_ADAPTER_TYPES
    }
