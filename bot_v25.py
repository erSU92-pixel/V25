#!/usr/bin/env python3
"""
==============================================================================
V25.0 ULTIMATE TRADING BOT - PRODUCTION READY (GITHUB ACTIONS OPTIMIZED)
==============================================================================
"""

import os
import sys
import json
import time
import argparse
import statistics
import logging
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

import pytz
import requests
import pandas as pd
import yfinance as yf
import websocket
import yaml

# Suppress warnings
warnings.filterwarnings("ignore", message="Failed to extract font properties")
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")
warnings.filterwarnings("ignore", module="matplotlib.font_manager")

# ==============================================================================
# 1. DEFAULT CONFIGURATION
# ==============================================================================
DEFAULT_CONFIG: Dict[str, Any] = {
    "bot": {
        "symbol": "frxXAUUSD",
        "deriv_app_id": "1089",
        "mt5_offset": 0.0,
        "consensus_threshold": 60,
        "signal_cooldown_minutes": 30
    },
    "telegram": {
        "token": "",
        "chat_id": ""
    },
    "data_sources": {
        "deriv": {
            "enabled": True,
            "candle_limit_m15": 300,
            "candle_limit_h1": 200,
            "candle_limit_h4": 200,
            "timeout": 10,
            "max_retries": 3
        },
        "binance": {
            "enabled": True,
            "symbol": "PAXGUSDT",
            "timeout": 6
        },
        "yahoo": {
            "enabled": True,
            "symbol": "GC=F",
            "period": "2d",
            "interval": "15m",
            "offset": -46.50
        }
    },
    "risk_management": {
        "atr_period": 14,
        "sl_min": 6.0,
        "sl_max": 16.0,
        "sl_buffer": 1.2,
        "tp_multipliers": {
            "tp1": 1.3,
            "tp2": 2.2,
            "tp3": 3.5,
            "tp4": 5.0
        }
    },
    "session": {
        "timezone": "Asia/Jakarta",
        "active_hours": {"start": 13, "end": 23},
        "active_mult": 1.35,
        "low_mult": 1.15
    },
    "validation": {
        "max_price_deviation": 25.0
    },
    "dna": {
        "weight_min": 0.5,
        "weight_max": 2.0,
        "reward_aligned": 0.02,
        "penalty_opposed": -0.03,
        "mean_reversion_rate": 0.05
    },
    "paths": {
        "dna_file": "data/dna_10_engines.json",
        "journal_file": "data/trade_journal.json",
        "chart_path": "/tmp/chart_v250.png",
        "journal_max_entries": 50
    },
    "logging": {
        "level": "INFO"
    }
}

# ==============================================================================
# 2. CONFIGURATION LOADER
# ==============================================================================
def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def load_config() -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    config_path = Path("config.yml")
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                yml_cfg = yaml.safe_load(f) or {}
            cfg = deep_merge(cfg, yml_cfg)
        except Exception as e:
            print(f"⚠️ Gagal baca config.yml: {e}. Menggunakan default.")
    
    env_map = {
        "TELEGRAM_BOT_TOKEN": ("telegram", "token"),
        "TELEGRAM_TOKEN": ("telegram", "token"),
        "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
        "DERIV_SYMBOL": ("bot", "symbol"),
        "DERIV_APP_ID": ("bot", "deriv_app_id"),
        "MT5_OFFSET": ("bot", "mt5_offset"),
    }
    for env_key, (section, key) in env_map.items():
        val = os.getenv(env_key)
        if val is not None and val.strip():
            if key == "mt5_offset":
                try:
                    val = float(val)
                except ValueError:
                    val = 0.0
            cfg[section][key] = val
    return cfg

CFG: Dict[str, Any] = load_config()

logging.basicConfig(
    level=getattr(logging, CFG["logging"]["level"], logging.INFO),
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S %d-%m"
)
log = logging.getLogger("bot_v25")

WIB = pytz.timezone(CFG["session"]["timezone"])
Path(CFG["paths"]["dna_file"]).parent.mkdir(parents=True, exist_ok=True)

# ==============================================================================
# 3. TELEGRAM
# ==============================================================================
def send_text(m: str) -> None:
    token = CFG["telegram"]["token"]
    chat = CFG["telegram"]["chat_id"]
    if not token or not chat:
        log.debug("Telegram tidak dikonfigurasi")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": m, "parse_mode": "HTML"},
            timeout=12
        )
    except Exception as e:
        log.warning(f"Gagal kirim teks: {e}")

def send_photo(cap: str, p: str) -> None:
    token = CFG["telegram"]["token"]
    chat = CFG["telegram"]["chat_id"]
    if not token or not chat:
        return
    try:
        with open(p, 'rb') as f:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat, "caption": cap, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=20
            )
    except Exception as e:
        log.warning(f"Gagal kirim foto: {e}")
        send_text(cap)

# ==============================================================================
# 4. PERSISTENCE
# ==============================================================================
def backup_file(path: str) -> None:
    p = Path(path)
    if p.exists():
        backup = p.with_suffix(f".backup_{int(time.time())}.json")
        try:
            p.rename(backup)
            backups = sorted(p.parent.glob(f"{p.stem}.backup_*.json"))
            for old in backups[:-3]:
                old.unlink()
        except Exception as e:
            log.warning(f"Gagal backup {path}: {e}")

def load_json(p: str, default: Any) -> Any:
    try:
        path = Path(p)
        if path.exists() and path.stat().st_size > 0:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Error load {p}: {e}")
    return default

def save_json(p: str, d: Any) -> None:
    try:
        backup_file(p)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Error save {p}: {e}")

# ==============================================================================
# 5. DATA FETCHING (ROBUST FALLBACK)
# ==============================================================================
def fetch_deriv(limit: int = 300, gran: int = 900) -> Tuple[Optional[pd.DataFrame], Optional[float]]:
    ds = CFG["data_sources"]["deriv"]
    if not ds["enabled"]:
        return None, None
    
    url = f"wss://ws.derivws.com/websockets/v3?app_id={CFG['bot']['deriv_app_id']}"
    req_id = int(time.time() * 1000)
    
    headers = [
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin: https://app.deriv.com"
    ]
    
    payload = {
        "ticks_history": CFG["bot"]["symbol"],
        "count": limit,
        "end": "latest",
        "granularity": gran,
        "style": "candles",
        "req_id": req_id
    }
    
    for attempt in range(ds["max_retries"]):
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=ds["timeout"], header=headers)
            ws.send(json.dumps(payload))
            
            for _ in range(15):
                res = json.loads(ws.recv())
                if res.get("req_id") == req_id and "candles" in res:
                    df = pd.DataFrame(res["candles"])
                    for c in ["close", "high", "low", "open"]:
                        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
                    if "volume" not in df.columns:
                        df["volume"] = 0.0
                    else:
                        df["volume"] = pd.to_numeric(df["volume"], errors='coerce').fillna(0.0)
                    ws.close()
                    return df, float(df["close"].iloc[-1])
            
            if ws:
                ws.close()
        except Exception as e:
            log.warning(f"Deriv WS attempt {attempt+1}/{ds['max_retries']} gagal: {e}")
            if ws:
                try:
                    ws.close()
                except:
                    pass
            time.sleep(2 ** attempt)
    return None, None

def fetch_binance_paxg() -> Optional[float]:
    ds = CFG["data_sources"]["binance"]
    if not ds["enabled"]:
        return None
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={ds['symbol']}",
            timeout=ds["timeout"]
        )
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Binance {ds['symbol']} gagal: {e}")
    return None

def fetch_yf_gc() -> Tuple[Optional[pd.DataFrame], Optional[float]]:
    ds = CFG["data_sources"]["yahoo"]
    if not ds["enabled"]:
        return None, None
    try:
        df = yf.Ticker(ds["symbol"]).history(period=ds["period"], interval=ds["interval"])
        if df is None or len(df) < 5:
            return None, None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df.reset_index().rename(columns={
            "Close": "close", "High": "high", "Low": "low",
            "Open": "open", "Volume": "volume"
        })
        df["volume"] = pd.to_numeric(df["volume"], errors='coerce').fillna(0.0)
        price = float(df["close"].iloc[-1])
        cols = ["close", "high", "low", "open", "volume"]
        return df[[c for c in cols if c in df.columns]].tail(300), price
    except Exception as e:
        log.warning(f"Yahoo Finance {ds['symbol']} gagal: {e}")
        return None, None

def get_multi_source_price() -> Tuple[Optional[float], Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame], Dict[str, float]]:
    """Ambil harga dengan fallback robust. Jika Deriv diblokir, gunakan Yahoo."""
    prices: Dict[str, float] = {}
    df_m15 = None
    
    # 1. Deriv
    df_deriv, p_deriv = fetch_deriv(CFG["data_sources"]["deriv"]["candle_limit_m15"], 900)
    if p_deriv:
        prices["Deriv M15"] = p_deriv
        df_m15 = df_deriv
    
    # 2. Binance
    p_binance = fetch_binance_paxg()
    if p_binance:
        prices["Binance PAXG"] = p_binance
    
    # 3. Yahoo Finance (Dengan Offset & Fallback Candle)
    ds_yf = CFG["data_sources"]["yahoo"]
    if ds_yf["enabled"]:
        df_yf, p_yf = fetch_yf_gc()
        if p_yf:
            yf_offset = float(ds_yf.get("offset", 0.0))
            p_yf_adj = p_yf + yf_offset
            prices["Yahoo GC=F"] = p_yf_adj
            
            # Terapkan offset ke DataFrame candle agar engine menghitung dengan benar
            if df_yf is not None and yf_offset != 0.0:
                for col in ["close", "high", "low", "open"]:
                    if col in df_yf.columns:
                        df_yf[col] = df_yf[col] + yf_offset
            
            # Fallback: Jika Deriv gagal, gunakan candle Yahoo yang sudah di-offset
            if df_m15 is None:
                df_m15 = df_yf
                log.info("🔄 Deriv gagal, menggunakan candle Yahoo Finance sebagai fallback.")
    
    if not prices:
        log.warning("⚠️ Semua sumber harga gagal. Melewati siklus ini.")
        return None, None, None, None, {}
    
    vals = list(prices.values())
    median_price = float(statistics.median(vals))
    max_dev = CFG["validation"]["max_price_deviation"]
    valid_prices = {k: v for k, v in prices.items() if abs(v - median_price) <= max_dev}
    if not valid_prices:
        valid_prices = prices
    
    raw_price = float(statistics.median(list(valid_prices.values())))
    global_offset = float(CFG["bot"]["mt5_offset"])
    final_price = raw_price + global_offset
    
    log.info(f"Harga: {valid_prices} | Median: {raw_price:.2f} | Global Offset: {global_offset:+.2f} | Final: {final_price:.2f}")
    
    # Fetch Higher Timeframe (Fallback ke df_m15 jika gagal)
    ds = CFG["data_sources"]["deriv"]
    df_h1, _ = fetch_deriv(ds["candle_limit_h1"], 3600)
    if df_h1 is None:
        df_h1 = df_m15
    df_h4, _ = fetch_deriv(ds["candle_limit_h4"], 14400)
    if df_h4 is None:
        df_h4 = df_m15
    
    return final_price, df_m15, df_h1, df_h4, valid_prices

# ==============================================================================
# 6. INDICATORS
# ==============================================================================
def calc_atr(df: pd.DataFrame, period: Optional[int] = None) -> float:
    if df is None or df.empty:
        return 8.0
    if period is None:
        period = CFG["risk_management"]["atr_period"]
    try:
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().fillna(8.0).iloc[-1])
    except:
        return 8.0

def rsi(df: pd.DataFrame, p: int = 14) -> pd.Series:
    if df is None or df.empty:
        return pd.Series([50.0])
    try:
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(p).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(p).mean()
        rs = gain / (loss + 1e-9)
        return (100 - (100 / (1 + rs))).fillna(50.0)
    except:
        return pd.Series([50.0] * len(df))

# ==============================================================================
# 7. 10 CORE ENGINES
# ==============================================================================
def NADI(df: pd.DataFrame) -> Tuple[int, float]:
    e9 = df["close"].ewm(9).mean().iloc[-1]
    e21 = df["close"].ewm(21).mean().iloc[-1]
    pr = df["close"].iloc[-1]
    if pr > e9 > e21: return (1, 1.5)
    if pr < e9 < e21: return (-1, 1.5)
    return (0, 1.5)

def PADI(df: pd.DataFrame) -> Tuple[int, float]:
    r = float(rsi(df).iloc[-1])
    if r > 55: return (1, 1.3)
    if r < 45: return (-1, 1.3)
    return (0, 1.3)

def API_ENGINE(df: pd.DataFrame) -> Tuple[int, float]:
    body = abs(df["close"].iloc[-1] - df["open"].iloc[-1])
    avg_b = abs(df["close"] - df["open"]).rolling(20).mean().iloc[-1]
    pr, prev = df["close"].iloc[-1], df["close"].iloc[-2]
    if body > avg_b and pr > prev: return (1, 1.2)
    if body > avg_b and pr < prev: return (-1, 1.2)
    return (0, 1.2)

def ANGIN(df: pd.DataFrame) -> Tuple[int, float]:
    hi, lo, cl = df["high"].iloc[-1], df["low"].iloc[-1], df["close"].iloc[-1]
    upper_wick = hi - cl
    lower_wick = cl - lo
    if upper_wick > lower_wick * 1.5: return (-1, 1.0)
    if lower_wick > upper_wick * 1.5: return (1, 1.0)
    return (0, 1.0)

def EMBER(df: pd.DataFrame) -> Tuple[int, float]:
    pr = df["close"].iloc[-1]
    ma10 = df["close"].rolling(10).mean().iloc[-1]
    return (1, 1.0) if pr > ma10 else (-1, 1.0)

def LUMPUR(df: pd.DataFrame) -> Tuple[int, float]:
    vol = df["volume"].iloc[-1]
    vma = df["volume"].rolling(20).mean().iloc[-1]
    pr, prev = df["close"].iloc[-1], df["close"].iloc[-2]
    if vol > vma and pr > prev: return (1, 1.1)
    if vol > vma and pr < prev: return (-1, 1.1)
    return (0, 1.1)

def SEMUT(df: pd.DataFrame) -> Tuple[int, float]:
    e50 = df["close"].ewm(50).mean().iloc[-1]
    pr = df["close"].iloc[-1]
    return (1, 1.4) if pr > e50 else (-1, 1.4)

def WAYANG(df: pd.DataFrame) -> Tuple[int, float]:
    hi = df["high"].rolling(20).max().iloc[-1]
    lo = df["low"].rolling(20).min().iloc[-1]
    pr = df["close"].iloc[-1]
    return (1, 1.2) if pr > (hi + lo) / 2 else (-1, 1.2)

def AKAR(df: pd.DataFrame) -> Tuple[int, float]:
    if len(df) >= 200:
        s200 = df["close"].rolling(200).mean().iloc[-1]
    else:
        s200 = df["close"].mean()
    pr = df["close"].iloc[-1]
    return (1, 2.0) if pr > s200 else (-1, 2.0)

def SAWAH(df: pd.DataFrame) -> Tuple[int, float]:
    hi = df["high"].rolling(50).max().iloc[-1]
    lo = df["low"].rolling(50).min().iloc[-1]
    pr = df["close"].iloc[-1]
    f62 = lo + (hi - lo) * 0.618
    f38 = lo + (hi - lo) * 0.382
    if pr > f62: return (1, 1.8)
    if pr < f38: return (-1, 1.8)
    return (0, 1.8)

# ==============================================================================
# 8. DNA EVOLUTION
# ==============================================================================
def evolve_dna(dna: Dict[str, Any], engines_results: List[Tuple[str, int, float]], h1_tr: int, h4_tr: int) -> Dict[str, Any]:
    if "engines" not in dna:
        dna["engines"] = {}
    dna_cfg = CFG["dna"]
    macro_bias = 0
    if h1_tr == 1 and h4_tr == 1:
        macro_bias = 1
    elif h1_tr == -1 and h4_tr == -1:
        macro_bias = -1
    for name, sc, _ in engines_results:
        if name not in dna["engines"]:
            dna["engines"][name] = {"weight": 1.0}
        curr = dna["engines"][name]["weight"]
        if macro_bias != 0 and sc == macro_bias:
            target_adj = dna_cfg["reward_aligned"]
        elif macro_bias != 0 and sc == -macro_bias:
            target_adj = dna_cfg["penalty_opposed"]
        else:
            target_adj = (1.0 - curr) * dna_cfg["mean_reversion_rate"]
        new_w = max(dna_cfg["weight_min"], min(dna_cfg["weight_max"], curr + target_adj))
        dna["engines"][name]["weight"] = round(new_w, 4)
    return dna

# ==============================================================================
# 9. CHART
# ==============================================================================
def make_chart(df: pd.DataFrame, entry: float, sl: float, t1: float, t3: float, 
               signal: str, conf: float, atr_val: float) -> Optional[str]:
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams['axes.unicode_minus'] = False
        
        plt.figure(figsize=(12, 6))
        sub = df.tail(80)
        plt.plot(sub["close"].values, label="Price M15", color="gold", linewidth=1.5)
        if len(df) >= 50:
            sma50 = df["close"].rolling(50).mean().tail(80).values
            plt.plot(sma50, label="H1 SMA 50", color="blue", linestyle="--", alpha=0.6)
        plt.axhline(entry, color="cyan", linewidth=2, label=f"Entry {entry:.2f}")
        plt.axhline(sl, color="red", linewidth=2, label=f"SL {sl:.2f}")
        plt.axhline(t1, color="green", linestyle=":", label=f"TP1 {t1:.2f}")
        plt.axhline(t3, color="darkgreen", linestyle="-", label=f"TP3 {t3:.2f}")
        plt.title(f"{signal} | Confidence: {conf:.0f}% | ATR: {atr_val:.2f}", fontsize=12, fontweight='bold')
        plt.legend(fontsize=9, loc='upper left')
        plt.grid(alpha=0.3)
        plt.tight_layout()
        chart_path = CFG["paths"]["chart_path"]
        plt.savefig(chart_path, dpi=150, bbox_inches='tight')
        plt.close()
        return chart_path
    except Exception as e:
        log.error(f"Gagal membuat chart: {e}")
        return None

# ==============================================================================
# 10. MAIN
# ==============================================================================
def check_cooldown(journal: List[Dict[str, Any]], signal: str) -> bool:
    if not journal:
        return True
    last = journal[-1]
    if last["signal"] != signal:
        return True
    try:
        last_time = datetime.fromisoformat(last["time"])
        now = datetime.now(WIB)
        cooldown = timedelta(minutes=CFG["bot"]["signal_cooldown_minutes"])
        return (now - last_time) > cooldown
    except:
        return True

def main() -> int:
    parser = argparse.ArgumentParser(description="V25.0 Trading Bot")
    parser.add_argument("--dry-run", action="store_true", help="Test tanpa kirim sinyal")
    parser.add_argument("--check-config", action="store_true", help="Validasi konfigurasi")
    parser.add_argument("--show-engines", action="store_true", help="Tampilkan status engine")
    args = parser.parse_args()
    
    if args.check_config:
        log.info("✅ Konfigurasi valid:")
        log.info(f"  Symbol: {CFG['bot']['symbol']}")
        log.info(f"  MT5 Offset: {CFG['bot']['mt5_offset']}")
        log.info(f"  Telegram: {'✅' if CFG['telegram']['token'] else '❌'}")
        return 0
    
    log.info("🚀 V25.0 ULTIMATE START")
    dna = load_json(CFG["paths"]["dna_file"], {"engines": {}})
    journal = load_json(CFG["paths"]["journal_file"], [])
    
    try:
        price, df_m15, df_h1, df_h4, sources = get_multi_source_price()
    except Exception as e:
        log.error(f"Gagal mengambil harga: {e}")
        send_text(f"🛑 <b>CRITICAL ERROR</b>\nGagal mengambil data harga:\n{e}")
        return 1

    # Jika semua sumber gagal, keluar dengan aman (exit 0 agar tidak merah di GitHub Actions)
    if price is None or df_m15 is None:
        log.warning("⚠️ Data harga atau candle tidak tersedia. Melewati siklus ini dengan aman.")
        return 0
    
    engine_map = [
        ("NADI", NADI, df_m15), ("PADI", PADI, df_m15), ("API", API_ENGINE, df_m15),
        ("ANGIN", ANGIN, df_m15), ("EMBER", EMBER, df_m15), ("LUMPUR", LUMPUR, df_m15),
        ("SEMUT", SEMUT, df_h1), ("WAYANG", WAYANG, df_h1),
        ("AKAR", AKAR, df_h4), ("SAWAH", SAWAH, df_h4)
    ]
    
    if args.show_engines:
        log.info("📊 Status Engine:")
        for name, fn, df_target in engine_map:
            try:
                sc, base_w = fn(df_target)
                dna_w = dna.get("engines", {}).get(name, {}).get("weight", 1.0)
                final_w = base_w * dna_w
                signal_str = "BUY" if sc > 0 else ("SELL" if sc < 0 else "NEUTRAL")
                log.info(f"  {name:8s}: {signal_str:8s} | Base: {base_w:.2f} | DNA: {dna_w:.4f} | Final: {final_w:.2f}")
            except Exception as e:
                log.error(f"  {name}: ERROR - {e}")
        return 0
    
    buy_w, sell_w = 0.0, 0.0
    engines_results: List[Tuple[str, int, float]] = []
    
    for name, fn, df_target in engine_map:
        try:
            sc, base_w = fn(df_target)
            dna_w = dna.get("engines", {}).get(name, {}).get("weight", 1.0)
            final_w = base_w * dna_w
            engines_results.append((name, sc, final_w))
            if sc > 0:
                buy_w += final_w
            elif sc < 0:
                sell_w += final_w
        except Exception as e:
            log.warning(f"Engine {name} error: {e}")
    
    def get_trend(df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        try:
            s50 = df["close"].rolling(50).mean().iloc[-1]
            pr = df["close"].iloc[-1]
            return 1 if pr > s50 else (-1 if pr < s50 else 0)
        except:
            return 0
    
    h1_tr = get_trend(df_h1)
    h4_tr = get_trend(df_h4)
    dna = evolve_dna(dna, engines_results, h1_tr, h4_tr)
    save_json(CFG["paths"]["dna_file"], dna)
    
    total_w = buy_w + sell_w
    consensus = (max(buy_w, sell_w) / total_w * 100) if total_w > 0 else 50.0
    signal = "BUY" if buy_w > sell_w else "SELL"
    
    threshold = CFG["bot"]["consensus_threshold"]
    if consensus < threshold:
        log.info(f"⏸️ Konsensus {consensus:.0f}% < {threshold}%. Skip.")
        return 0
    
    if not check_cooldown(journal, signal):
        log.info(f"⏸️ Sinyal {signal} terlalu baru (cooldown). Skip.")
        return 0
    
    now_hour = datetime.now(WIB).hour
    active_start = CFG["session"]["active_hours"]["start"]
    active_end = CFG["session"]["active_hours"]["end"]
    is_active = (active_start <= now_hour <= active_end) or (0 <= now_hour <= 2)
    session_mult = CFG["session"]["active_mult"] if is_active else CFG["session"]["low_mult"]
    
    atr_m15 = calc_atr(df_m15)
    sl_base = max(CFG["risk_management"]["sl_min"], 
                  min(CFG["risk_management"]["sl_max"], atr_m15 * session_mult)) + CFG["risk_management"]["sl_buffer"]
    
    entry = price
    tp_mult = CFG["risk_management"]["tp_multipliers"]
    if signal == "BUY":
        sl = entry - sl_base
        t1 = entry + (sl_base * tp_mult["tp1"])
        t2 = entry + (sl_base * tp_mult["tp2"])
        t3 = entry + (sl_base * tp_mult["tp3"])
        t4 = entry + (sl_base * tp_mult["tp4"])
    else:
        sl = entry + sl_base
        t1 = entry - (sl_base * tp_mult["tp1"])
        t2 = entry - (sl_base * tp_mult["tp2"])
        t3 = entry - (sl_base * tp_mult["tp3"])
        t4 = entry - (sl_base * tp_mult["tp4"])
    
    trade_entry = {
        "time": datetime.now(WIB).isoformat(),
        "signal": signal,
        "price": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(t1, 2),
        "tp3": round(t3, 2),
        "consensus": round(consensus, 2),
        "atr_used": round(atr_m15, 2)
    }
    
    if not args.dry_run:
        journal.append(trade_entry)
        max_entries = CFG["paths"]["journal_max_entries"]
        if len(journal) > max_entries:
            journal = journal[-max_entries:]
        save_json(CFG["paths"]["journal_file"], journal)
    
    sumber_text = "\n".join([f"• {k}: {v:.2f}" for k, v in sources.items()])
    offset = CFG["bot"]["mt5_offset"]
    offset_info = f"\n⚙️ <b>MT5 Offset:</b> {offset:+.2f}" if offset != 0 else ""
    session_info = "🔥 London/NY Overlap" if is_active else "🌙 Asian Session"
    
    caption = (
        f"💎 <b>V25.0 ULTIMATE - {signal} ({consensus:.0f}%)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Sumber Harga:</b>\n{sumber_text}\n"
        f"🎯 <b>Adjusted Price:</b> {price:.2f} {offset_info}\n"
        f"🕒 <b>Sesi:</b> {session_info} | ATR: {atr_m15:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Entry:</b> <code>{entry:.2f}</code>\n"
        f"🔴 <b>SL:</b> <code>{sl:.2f}</code> (-{sl_base:.2f}$)\n"
        f"🟩 <b>TP1:</b> <code>{t1:.2f}</code> | <b>TP2:</b> <code>{t2:.2f}</code>\n"
        f"🟩 <b>TP3:</b> <code>{t3:.2f}</code> | <b>TP4:</b> <code>{t4:.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{'🧪 DRY-RUN MODE | ' if args.dry_run else ''}✅ Synced | {datetime.now(WIB).strftime('%H:%M:%S WIB')}"
    )
    
    if args.dry_run:
        log.info(f" DRY-RUN: {signal} @ {entry:.2f} (Consensus: {consensus:.0f}%)")
        return 0
    
    chart_path = make_chart(df_m15, entry, sl, t1, t3, signal, consensus, atr_m15)
    if chart_path and Path(chart_path).exists():
        send_photo(caption, chart_path)
    else:
        send_text(caption)
    
    log.info(f"✅ SINYAL DIKIRIM: {signal} @ {entry:.2f} (Consensus: {consensus:.0f}%)")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("🛑 Bot dihentikan (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        log.critical(f" Fatal error: {e}", exc_info=True)
        sys.exit(1)
