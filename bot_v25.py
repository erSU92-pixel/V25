#!/usr/bin/env python3
# V25.0 PERFECTED: OFFSET-SYNCHRONIZED + MULTI-TF ENGINES + SMART DNA + ROBUST FETCH
import os
import json
import time
import sys
import statistics
from datetime import datetime, timedelta
import pytz
import requests
import pandas as pd
import yfinance as yf
import websocket

# ==============================================================================
# 1. KONFIGURASI PUSAT (Menggantikan Magic Numbers)
# ==============================================================================
CONFIG = {
    "DERIV_SYMBOL": os.getenv("DERIV_SYMBOL", "frxXAUUSD"),
    "DERIV_APP_ID": os.getenv("DERIV_APP_ID", "1089"),
    "TELE_TOKEN": os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELE_CHAT": os.getenv("TELEGRAM_CHAT_ID", ""),
    "MT5_OFFSET": float(os.getenv("MT5_OFFSET", "0.0")),
    
    # Risk Management
    "ATR_PERIOD": 14,
    "SL_MIN": 6.0,
    "SL_MAX": 16.0,
    "SL_BUFFER": 1.2,
    "TP1_MULT": 1.3,
    "TP2_MULT": 2.2,
    "TP3_MULT": 3.5,
    "TP4_MULT": 5.0,
    
    # Session Multipliers (WIB)
    "SESSION_MULT_ACTIVE": 1.35,  # 13:00 - 02:00 (London & NY Overlap)
    "SESSION_MULT_LOW": 1.15,     # Sisa waktu (Asian session)
    
    # Price Validation
    "MAX_PRICE_DEVIATION": 25.0,
    
    # File Paths
    "DNA_FILE": "dna_10_engines.json",
    "JOURNAL_FILE": "trade_journal.json",
    "CHART_PATH": "/tmp/chart_v250.png"
}

WIB = pytz.timezone("Asia/Jakarta")

# ==============================================================================
# 2. UTILITAS & LOGGING
# ==============================================================================
def log(m: str):
    """Logging terstandarisasi dengan timestamp WIB."""
    timestamp = datetime.now(WIB).strftime('%H:%M:%S %d-%m')
    print(f"[{timestamp} WIB] {m}")

def send_text(m: str):
    if not CONFIG["TELE_TOKEN"] or not CONFIG["TELE_CHAT"]: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{CONFIG['TELE_TOKEN']}/sendMessage",
            json={"chat_id": CONFIG["TELE_CHAT"], "text": m, "parse_mode": "HTML"},
            timeout=12
        )
    except Exception as e:
        log(f"❌ Gagal kirim teks Telegram: {e}")

def send_photo(cap: str, p: str):
    if not CONFIG["TELE_TOKEN"] or not CONFIG["TELE_CHAT"]: return
    try:
        with open(p, 'rb') as f:
            requests.post(
                f"https://api.telegram.org/bot{CONFIG['TELE_TOKEN']}/sendPhoto",
                data={"chat_id": CONFIG["TELE_CHAT"], "caption": cap, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=20
            )
    except Exception as e:
        log(f"❌ Gagal kirim foto Telegram: {e}. Fallback ke teks.")
        send_text(cap)

def load_json(p: str, default: dict | list):
    try:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        log(f"⚠️ Error load {p}: {e}")
    return default

def save_json(p: str, d: dict | list):
    try:
        with open(p, 'w', encoding='utf-8') as f: json.dump(d, f, indent=2)
    except Exception as e:
        log(f"❌ Error save {p}: {e}")

# ==============================================================================
# 3. DATA FETCHING (ROBUST & TIMEZONE SAFE)
# ==============================================================================
def fetch_deriv(limit=300, gran=900):
    """Mengambil data candle dari Deriv dengan req_id untuk validasi respons."""
    url = "wss://ws.derivws.com/websockets/v3?app_id=" + CONFIG["DERIV_APP_ID"]
    req_id = int(time.time() * 1000)
    
    for attempt in range(3):
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=10)
            payload = {
                "ticks_history": CONFIG["DERIV_SYMBOL"],
                "count": limit,
                "end": "latest",
                "granularity": gran,
                "style": "candles",
                "req_id": req_id
            }
            ws.send(json.dumps(payload))
            
            for _ in range(15): # Tunggu maksimal 15 pesan
                res = json.loads(ws.recv())
                if res.get("req_id") == req_id and "candles" in res:
                    df = pd.DataFrame(res["candles"])
                    # PERBAIKAN KRITIS: Jangan overwrite volume jika ada, isi 0 jika tidak ada
                    if "volume" not in df.columns:
                        df["volume"] = 0.0
                    
                    for c in ["close", "high", "low", "open"]:
                        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
                    
                    ws.close()
                    return df, float(df["close"].iloc[-1])
            if ws: ws.close()
        except Exception as e:
            log(f"⚠️ Deriv attempt {attempt+1} gagal: {e}")
            if ws:
                try: ws.close()
                except: pass
            time.sleep(2)
    return None, None

def fetch_binance_paxg():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", timeout=6)
        if r.status_code == 200: return float(r.json()["price"])
    except Exception as e:
        log(f"⚠️ Binance PAXG gagal: {e}")
    return None

def fetch_yf_gc():
    try:
        df = yf.Ticker("GC=F").history(period="2d", interval="15m")
        if df is None or len(df) < 5: return None, None
        
        # PERBAIKAN KRITIS: Normalisasi timezone agar tidak bentrok dengan data Deriv
        df.index = df.index.tz_localize(None)
        df = df.reset_index().rename(columns={"Close": "close", "High": "high", "Low": "low", "Open": "open", "Volume": "volume"})
        df["volume"] = pd.to_numeric(df["volume"], errors='coerce').fillna(0.0).astype(float)
        
        price = float(df["close"].iloc[-1])
        return df[["close", "high", "low", "open", "volume"]].tail(300), price
    except Exception as e:
        log(f"⚠️ Yahoo Finance GC=F gagal: {e}")
        return None, None

def get_multi_source_price():
    prices = {}
    df_m15, p_deriv = fetch_deriv(300, 900)
    if p_deriv: prices["Deriv M15"] = p_deriv
    
    p_binance = fetch_binance_paxg()
    if p_binance: prices["Binance PAXG"] = p_binance
    
    df_yf, p_yf = fetch_yf_gc()
    if p_yf: 
        prices["Yahoo GC=F"] = p_yf
        if df_m15 is None: df_m15 = df_yf # Fallback jika Deriv gagal total
        
    if not prices:
        raise RuntimeError("Kritis: Seluruh sumber harga gagal diakses!")
        
    # PERBAIKAN KRITIS: Gunakan statistics.median untuk akurasi genap/ganjil
    vals = list(prices.values())
    median_price = float(statistics.median(vals))
    
    # Filter outlier
    valid_prices = {k: v for k, v in prices.items() if abs(v - median_price) <= CONFIG["MAX_PRICE_DEVIATION"]}
    if not valid_prices: valid_prices = prices # Fallback jika semua dianggap outlier
    
    raw_price = float(statistics.median(list(valid_prices.values())))
    final_price = raw_price + CONFIG["MT5_OFFSET"]
    
    log(f"💰 Harga Valid: {valid_prices} | Median: {raw_price:.2f} | Offset: {CONFIG['MT5_OFFSET']:+.2f} | Final: {final_price:.2f}")
    
    # Ambil data H1 dan H4 untuk konfirmasi tren makro
    df_h1, _ = fetch_deriv(200, 3600)
    if df_h1 is None: df_h1 = df_m15 # Fallback
    df_h4, _ = fetch_deriv(200, 14400)
    if df_h4 is None: df_h4 = df_h1
    
    return final_price, df_m15, df_h1, df_h4, valid_prices

# ==============================================================================
# 4. INDIKATOR & 10 CORE ENGINES (MULTI-TIMEFRAME AWARE)
# ==============================================================================
def calc_atr(df, period=14):
    try:
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        # PERBAIKAN: fillna untuk mencegah NaN jika data < period
        return tr.rolling(period).mean().fillna(8.0).iloc[-1]
    except: return 8.0

def rsi(df, p=14):
    try:
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(p).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(p).mean()
        rs = gain / (loss + 1e-9)
        return 100 - (100 / (1 + rs)).fillna(50.0)
    except: return pd.Series([50.0] * len(df))

# --- ENGINE M15 (Momentum & Short Term) ---
def NADI(df): 
    e9, e21, pr = df["close"].ewm(9).mean().iloc[-1], df["close"].ewm(21).mean().iloc[-1], df["close"].iloc[-1]
    return (1, 1.5) if pr > e9 > e21 else (-1, 1.5) if pr < e9 < e21 else (0, 1.5)

def PADI(df): 
    r = rsi(df).iloc[-1]
    return (1, 1.3) if r > 55 else (-1, 1.3) if r < 45 else (0, 1.3)

def API(df): 
    body = abs(df["close"].iloc[-1] - df["open"].iloc[-1])
    avg_b = abs(df["close"] - df["open"]).rolling(20).mean().iloc[-1]
    pr, prev = df["close"].iloc[-1], df["close"].iloc[-2]
    return (1, 1.2) if body > avg_b and pr > prev else (-1, 1.2) if body > avg_b and pr < prev else (0, 1.2)

def ANGIN(df): 
    hi, lo, cl = df["high"].iloc[-1], df["low"].iloc[-1], df["close"].iloc[-1]
    return (-1, 1.0) if (hi - cl) > (cl - lo) * 1.5 else (1, 1.0) if (cl - lo) > (hi - cl) * 1.5 else (0, 1.0)

def EMBER(df): 
    pr, ma10 = df["close"].iloc[-1], df["close"].rolling(10).mean().iloc[-1]
    return (1, 1.0) if pr > ma10 else (-1, 1.0)

def LUMPUR(df): 
    vol, vma = df["volume"].iloc[-1], df["volume"].rolling(20).mean().iloc[-1]
    pr, prev = df["close"].iloc[-1], df["close"].iloc[-2]
    # PERBAIKAN: Sekarang volume asli digunakan, bukan 100.0
    return (1, 1.1) if vol > vma and pr > prev else (-1, 1.1) if vol > vma and pr < prev else (0, 1.1)

# --- ENGINE H1 (Medium Trend) ---
def SEMUT(df): 
    e50, pr = df["close"].ewm(50).mean().iloc[-1], df["close"].iloc[-1]
    return (1, 1.4) if pr > e50 else (-1, 1.4)

def WAYANG(df): 
    hi, lo, pr = df["high"].rolling(20).max().iloc[-1], df["low"].rolling(20).min().iloc[-1], df["close"].iloc[-1]
    return (1, 1.2) if pr > (hi + lo) / 2 else (-1, 1.2)

# --- ENGINE H4 (Macro Trend) ---
def AKAR(df): 
    s200 = df["close"].rolling(200).mean().iloc[-1] if len(df) >= 200 else df["close"].mean()
    pr = df["close"].iloc[-1]
    return (1, 2.0) if pr > s200 else (-1, 2.0) # Bobot lebih tinggi karena ini filter makro

def SAWAH(df): 
    hi, lo, pr = df["high"].rolling(50).max().iloc[-1], df["low"].rolling(50).min().iloc[-1], df["close"].iloc[-1]
    f62 = lo + (hi - lo) * 0.618
    f38 = lo + (hi - lo) * 0.382
    return (1, 1.8) if pr > f62 else (-1, 1.8) if pr < f38 else (0, 1.8)

# ==============================================================================
# 5. SMART DNA EVOLUTION & CHARTING
# ==============================================================================
def evolve_dna(dna, engines_results, h1_tr, h4_tr):
    """
    PERBAIKAN KRITIS: 
    Alih-alih 'echo chamber' (menghargai kesepakatan buta), DNA sekarang 
    menghargai engine yang SEJALAN dengan tren Higher Timeframe (H1/H4).
    Ini adalah prinsip 'Multiple Timeframe Analysis' yang valid.
    """
    if "engines" not in dna: dna["engines"] = {}
    
    # Tentukan bias makro
    macro_bias = 0
    if h1_tr == 1 and h4_tr == 1: macro_bias = 1
    elif h1_tr == -1 and h4_tr == -1: macro_bias = -1
    
    for name, sc, _ in engines_results:
        if name not in dna["engines"]: dna["engines"][name] = {"weight": 1.0}
        curr = dna["engines"][name]["weight"]
        
        # Logika penyesuaian:
        # Jika engine searah dengan macro bias, naikkan sedikit.
        # Jika berlawanan, turunkan sedikit.
        # Jika netral (sc==0), kembalikan perlahan ke 1.0 (mean reversion).
        if macro_bias != 0 and sc == macro_bias:
            target_adj = 0.02
        elif macro_bias != 0 and sc == -macro_bias:
            target_adj = -0.03
        else:
            target_adj = (1.0 - curr) * 0.05 # Mean reversion ke 1.0
            
        new_w = max(0.5, min(2.0, curr + target_adj))
        dna["engines"][name]["weight"] = round(new_w, 4)
    return dna

def make_chart(df, entry, sl, t1, t3, signal, conf, atr_val):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams['axes.unicode_minus'] = False
        
        plt.figure(figsize=(12, 6))
        sub = df.tail(80)
        plt.plot(sub["close"].values, label="Price M15", color="gold", linewidth=1.5)
        
        # Tambahkan konteks H1 SMA 50 jika tersedia
        if len(df) >= 50:
            sma50 = df["close"].rolling(50).mean().tail(80).values
            plt.plot(sma50, label="H1 SMA 50 (Ref)", color="blue", linestyle="--", alpha=0.6)

        plt.axhline(entry, color="cyan", linewidth=2, label=f"Entry {entry:.2f}")
        plt.axhline(sl, color="red", linewidth=2, label=f"SL {sl:.2f}")
        plt.axhline(t1, color="green", linestyle=":", label=f"TP1 {t1:.2f}")
        plt.axhline(t3, color="darkgreen", linestyle="-", label=f"TP3 {t3:.2f}")
        
        plt.title(f"{signal} Signal | Confidence: {conf:.0f}% | ATR: {atr_val:.2f}", fontsize=12, fontweight='bold')
        plt.legend(fontsize=9, loc='upper left')
        plt.grid(alpha=0.3)
        plt.tight_layout()
        
        plt.savefig(CONFIG["CHART_PATH"], dpi=150, bbox_inches='tight')
        plt.close() # Mencegah memory leak
        return CONFIG["CHART_PATH"]
    except Exception as e:
        log(f"❌ Gagal membuat chart: {e}")
        return None

# ==============================================================================
# 6. MAIN EXECUTION
# ==============================================================================
def main():
    log("🚀 V25.0 PERFECTED START")
    dna = load_json(CONFIG["DNA_FILE"], {"engines": {}})
    journal = load_json(CONFIG["JOURNAL_FILE"], [])

    try:
        price, df_m15, df_h1, df_h4, sources = get_multi_source_price()
    except Exception as e:
        log(f"🛑 Gagal mengambil harga multi-source: {e}")
        send_text(f"🛑 <b>CRITICAL ERROR</b>\nGagal mengambil data harga:\n{e}")
        return 1

    # Mapping Engine ke Timeframe yang sesuai
    engine_map = [
        ("NADI", NADI, df_m15), ("PADI", PADI, df_m15), ("API", API, df_m15),
        ("ANGIN", ANGIN, df_m15), ("EMBER", EMBER, df_m15), ("LUMPUR", LUMPUR, df_m15),
        ("SEMUT", SEMUT, df_h1), ("WAYANG", WAYANG, df_h1),
        ("AKAR", AKAR, df_h4), ("SAWAH", SAWAH, df_h4)
    ]
    
    buy_w, sell_w = 0.0, 0.0
    engines_results = []
    
    for name, fn, df_target in engine_map:
        try:
            sc, base_w = fn(df_target)
            dna_w = dna.get("engines", {}).get(name, {}).get("weight", 1.0)
            final_w = base_w * dna_w
            engines_results.append((name, sc, final_w))
            
            if sc > 0: buy_w += final_w
            elif sc < 0: sell_w += final_w
        except Exception as e:
            log(f"⚠️ Engine {name} error: {e}")

    # Hitung Tren H1/H4 untuk validasi makro
    def get_trend(df):
        try:
            s50 = df["close"].rolling(50).mean().iloc[-1]
            pr = df["close"].iloc[-1]
            return 1 if pr > s50 else (-1 if pr < s50 else 0)
        except: return 0

    h1_tr = get_trend(df_h1)
    h4_tr = get_trend(df_h4)
    
    # Evolusi DNA berdasarkan keselarasan timeframe
    dna = evolve_dna(dna, engines_results, h1_tr, h4_tr)
    save_json(CONFIG["DNA_FILE"], dna)

    # Kalkulasi Konsensus
    total_w = buy_w + sell_w
    consensus = (max(buy_w, sell_w) / total_w * 100) if total_w > 0 else 50.0
    signal = "BUY" if buy_w > sell_w else "SELL"

    # Filter Konsensus Minimum
    if consensus < 60.0:
        log(f"⏸️ Konsensus pasar {consensus:.0f}% < 60%. Melewati eksekusi.")
        return 0

    # Kalkulasi Risk Management (ATR Based)
    now_hour = datetime.now(WIB).hour
    is_active_session = (13 <= now_hour <= 23) or (0 <= now_hour <= 2)
    session_mult = CONFIG["SESSION_MULT_ACTIVE"] if is_active_session else CONFIG["SESSION_MULT_LOW"]

    atr_m15 = calc_atr(df_m15, CONFIG["ATR_PERIOD"])
    sl_base = max(CONFIG["SL_MIN"], min(CONFIG["SL_MAX"], atr_m15 * session_mult)) + CONFIG["SL_BUFFER"]
    
    entry = price
    if signal == "BUY":
        sl = entry - sl_base
        t1 = entry + (sl_base * CONFIG["TP1_MULT"])
        t2 = entry + (sl_base * CONFIG["TP2_MULT"])
        t3 = entry + (sl_base * CONFIG["TP3_MULT"])
        t4 = entry + (sl_base * CONFIG["TP4_MULT"])
    else:
        sl = entry + sl_base
        t1 = entry - (sl_base * CONFIG["TP1_MULT"])
        t2 = entry - (sl_base * CONFIG["TP2_MULT"])
        t3 = entry - (sl_base * CONFIG["TP3_MULT"])
        t4 = entry - (sl_base * CONFIG["TP4_MULT"])

    # Simpan ke Journal
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
    journal.append(trade_entry)
    if len(journal) > 50: journal = journal[-50:] # Cleanup otomatis
    save_json(CONFIG["JOURNAL_FILE"], journal)

    # Format Pesan Telegram
    sumber_text = "\n".join([f"• {k}: {v:.2f}" for k, v in sources.items()])
    offset_info = f"\n⚙️ <b>MT5 Offset:</b> {CONFIG['MT5_OFFSET']:+.2f}" if CONFIG['MT5_OFFSET'] != 0 else ""
    session_info = "🔥 London/NY Overlap" if is_active_session else "🌙 Asian Session"

    caption = (
        f"💎 <b>V25.0 PERFECTED - {signal} ({consensus:.0f}%)</b>\n"
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
        f"✅ Synced & Validated | {datetime.now(WIB).strftime('%H:%M:%S WIB')}"
    )

    # Kirim Notifikasi
    chart_path = make_chart(df_m15, entry, sl, t1, t3, signal, consensus, atr_m15)
    if chart_path and os.path.exists(chart_path):
        send_photo(caption, chart_path)
    else:
        send_text(caption)
    
    log(f"✅ SINYAL BERHASIL DIKIRIM: {signal} @ {entry:.2f} (Consensus: {consensus:.0f}%)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
