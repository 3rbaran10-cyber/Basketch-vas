import os
import io, json, time, requests, threading, sqlite3
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# AYARLAR
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

GEMINI_MODEL = "gemini-3.1-pro-preview"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# ESPN API (ucretsiz, anahtar gerekmez)
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_LEAGUES = {
    "NBA": "nba",
    "WNBA": "wnba",
    "NCAA": "mens-college-basketball",
}

ADMIN_ID = 5504006147

GEMINI_LOCK = threading.Lock()
SON_ISTEK_ZAMANI = [0.0]
MIN_ISTEK_ARASI = 3.0

MIN_TARANAN = 3
MAX_TARANAN = 8

USER_COOLDOWN = {}
RATE_LIMIT_SECONDS = 300

# ==========================================
# HEALTH SERVER
# ==========================================
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()

# ==========================================
# SQLITE
# ==========================================
DB_PATH = "/tmp/bot.db"
_DB_LOCK = threading.Lock()

def db_conn():
    return sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)

def init_db():
    try:
        with _DB_LOCK:
            conn = db_conn()
            conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)")
            conn.execute("""CREATE TABLE IF NOT EXISTS analizler (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cid INTEGER, tarih TEXT, mac TEXT,
                tercih TEXT, olasilik INTEGER,
                sonuc TEXT DEFAULT NULL, ts INTEGER)""")
            conn.commit(); conn.close()
        print("DB hazir", flush=True)
    except Exception as e:
        print(f"DB hatasi: {e}", flush=True)

def get_offset():
    try:
        with _DB_LOCK:
            conn = db_conn()
            cur = conn.execute("SELECT value FROM state WHERE key='offset'")
            row = cur.fetchone(); conn.close()
            return row[0] if row else 0
    except: return 0

def save_offset(o):
    try:
        with _DB_LOCK:
            conn = db_conn()
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('offset', ?)", (o,))
            conn.commit(); conn.close()
    except: pass

def save_analiz(cid, mac, tercih, olasilik):
    try:
        with _DB_LOCK:
            conn = db_conn()
            cur = conn.execute("""INSERT INTO analizler
                (cid, tarih, mac, tercih, olasilik, ts)
                VALUES (?,?,?,?,?,?)""",
                (cid, datetime.now().strftime("%d.%m.%Y %H:%M"),
                 mac, tercih, olasilik, int(time.time())))
            conn.commit()
            rid = cur.lastrowid; conn.close()
            return rid
    except Exception as e:
        print(f"save hatasi: {e}", flush=True); return None

def get_gecmis(cid, limit=10):
    try:
        with _DB_LOCK:
            conn = db_conn()
            cur = conn.execute("""SELECT id, tarih, mac, tercih, olasilik, sonuc
                FROM analizler WHERE cid=? ORDER BY id DESC LIMIT ?""", (cid, limit))
            rows = cur.fetchall(); conn.close()
            return rows
    except: return []

# ==========================================
# ESPN API
# ==========================================
def espn_call(league_code, date_str):
    """ESPN scoreboard API. date_str: YYYYMMDD"""
    url = f"{ESPN_BASE}/{league_code}/scoreboard"
    try:
        r = requests.get(url, params={"dates": date_str}, timeout=15)
        if r.status_code != 200:
            print(f"ESPN hata ({league_code}): {r.status_code}", flush=True)
            return None
        return r.json()
    except Exception as e:
        print(f"ESPN istek hatasi ({league_code}): {str(e)[:150]}", flush=True)
        return None

def _mac_parse(event, lig_adi):
    """ESPN event'ini standart formata cevir."""
    try:
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])

        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            return None

        ev_adi = home.get("team", {}).get("displayName", "?")
        dep_adi = away.get("team", {}).get("displayName", "?")
        ev_skor = home.get("score", "0")
        dep_skor = away.get("score", "0")

        durum = comp.get("status", {}).get("type", {}).get("description", "Scheduled")
        durum_short = comp.get("status", {}).get("type", {}).get("state", "pre")
        dakika = comp.get("status", {}).get("displayClock", "")
        periyot = comp.get("status", {}).get("period", 0)

        # Odds
        odds = comp.get("odds", [])
        odds_metni = "Odds bilgisi yok"
        if odds:
            o = odds[0]
            ou = o.get("overUnder")
            detay = o.get("details", "")
            odds_metni = f"O/U: {ou} | Spread: {detay}" if ou else detay

        # Takim kayitlari
        ev_kayit = ""
        dep_kayit = ""
        for rec in home.get("records", []):
            if rec.get("type") == "total":
                ev_kayit = rec.get("summary", "")
        for rec in away.get("records", []):
            if rec.get("type") == "total":
                dep_kayit = rec.get("summary", "")

        return {
            "lig": lig_adi,
            "ev": ev_adi,
            "dep": dep_adi,
            "ev_skor": ev_skor,
            "dep_skor": dep_skor,
            "ev_kayit": ev_kayit,
            "dep_kayit": dep_kayit,
            "tarih": event.get("date", ""),
            "durum": durum,
            "durum_short": durum_short,
            "dakika": dakika,
            "periyot": periyot,
            "odds": odds_metni,
            "venue": comp.get("venue", {}).get("fullName", ""),
        }
    except Exception as e:
        print(f"mac_parse hata: {e}", flush=True)
        return None

def bugunun_maclari_al():
    """Son 1 gun + bugun + sonraki 1 gun ABD basketbol maclari."""
    tr_now = datetime.utcnow() + timedelta(hours=3)
    tarihler = []
    for offset in [-1, 0, 1]:
        t = (tr_now + timedelta(days=offset)).strftime("%Y%m%d")
        tarihler.append(t)

    print(f"TR: {tr_now.strftime('%Y-%m-%d %H:%M')} | Taranan: {tarihler}", flush=True)

    maclar = []
    for lig_adi, lig_kod in ESPN_LEAGUES.items():
        for tarih in tarihler:
            r = espn_call(lig_kod, tarih)
            if not r:
                continue
            events = r.get("events", [])
            print(f"  {lig_adi} {tarih} -> {len(events)} mac", flush=True)
            for ev in events:
                parsed = _mac_parse(ev, lig_adi)
                if parsed:
                    maclar.append(parsed)

    # Tarih sirala
    maclar.sort(key=lambda x: x.get("tarih", ""))
    print(f"Toplam mac: {len(maclar)}", flush=True)
    return maclar

# ==========================================
# GEMINI
# ==========================================
def gemini_istek_at(payload):
    global SON_ISTEK_ZAMANI
    with GEMINI_LOCK:
        gecen = time.time() - SON_ISTEK_ZAMANI[0]
        if gecen < MIN_ISTEK_ARASI:
            time.sleep(MIN_ISTEK_ARASI - gecen)
        headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
        resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=300)
        SON_ISTEK_ZAMANI[0] = time.time()
        return resp

MAC_PROMPT = """Sen ABD basketbol (NBA, WNBA, NCAA) analiz uzmanisin. 15+ yillik deneyimli profesyonelsin.

=== MAC BILGILERI ===
Lig: {lig}
Mac: {ev} vs {dep}
Tarih: {tarih}
Durum: {durum} {dakika}
Skor: {ev} {ev_skor} - {dep_skor} {dep}
Ev sahibi kayit: {ev_kayit}
Deplasman kayit: {dep_kayit}
Salon: {venue}

=== BAHIS ORANLARI ===
{odds}

=== GOREV ===
Bu mac icin **TOPLAM SAYI ALT (under)** bahsi degerlendir.

SADECE SU TERCİHLERDEN BIRINI SEÇ:
- X.5 ALT (ornek: 220.5 ALT, 235.5 ALT, 278.5 ALT)

ALT ICIN ARANAN SARTLAR (kac tanesi uyuyor?):
1. Iki takimin sayi ortalamasi dusuk mu?
2. Savunma guclu mu?
3. Sakat oyuncular var mi (ozellikle skorer)?
4. H2H'de ALT orani yuksek mi?
5. Takimlar yorgun mu (back-to-back)?
6. Mac onemsiz mi (dostluk/hazirlik)?
7. Tempo dusuk mu?

KARAR:
- 4+ sart uyuyorsa -> "X.5 ALT" ver (yuksek olasilik)
- 2-3 sart uyuyorsa -> "BEKLE" ver
- 2'den az -> "BEKLE"

ONEMLI:
- SADECE ALT tercihi ver, UST ASLA VERME
- Toplam sayi cizgisi (ornek: 235.5) SEN belirle
- Gerekce 2-3 cumle
- Olasilik %55-95 arasi

=== CIKTI (SADECE JSON) ===
{{
  "tercih": "235.5 ALT",
  "olasilik": 75,
  "gerekce": "...",
  "guven": 78,
  "uyan_sart": 5
}}"""

def maci_analiz_et(m):
    try:
        prompt = MAC_PROMPT.format(
            lig=m["lig"], ev=m["ev"], dep=m["dep"],
            tarih=m["tarih"][:16].replace("T", " "),
            durum=m["durum"], dakika=m["dakika"],
            ev_skor=m["ev_skor"], dep_skor=m["dep_skor"],
            ev_kayit=m["ev_kayit"], dep_kayit=m["dep_kayit"],
            venue=m["venue"], odds=m["odds"]
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.8,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "object",
                    "properties": {
                        "tercih": {"type": "string"},
                        "olasilik": {"type": "integer"},
                        "gerekce": {"type": "string"},
                        "guven": {"type": "integer"},
                        "uyan_sart": {"type": "integer"}
                    },
                    "required": ["tercih", "olasilik", "gerekce", "guven", "uyan_sart"]
                }
            }
        }

        resp = gemini_istek_at(payload)
        if resp.status_code != 200:
            print(f"Gemini hata: {resp.status_code}", flush=True)
            return None
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        a = json.loads(text)
        return a
    except Exception as e:
        print(f"maci_analiz_et hata: {e}", flush=True)
        return None

# ==========================================
# TELEGRAM
# ==========================================
def send_msg(cid, text, parse_mode=None, reply_markup=None):
    try:
        p = {"chat_id": cid, "text": text}
        if parse_mode: p["parse_mode"] = parse_mode
        if reply_markup: p["reply_markup"] = reply_markup
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=p, timeout=15)
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"send_msg: {e}", flush=True); return None

def edit_reply_markup(cid, mid, rm=None):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
                      json={"chat_id": cid, "message_id": mid, "reply_markup": rm or {"inline_keyboard": []}},
                      timeout=10)
    except: pass

def edit_message_text(cid, mid, text, pm=None, rm=None):
    try:
        p = {"chat_id": cid, "message_id": mid, "text": text}
        if pm: p["parse_mode"] = pm
        if rm: p["reply_markup"] = rm
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json=p, timeout=10)
    except: pass

def check_rate_limit(cid):
    now = time.time()
    last = USER_COOLDOWN.get(cid, 0)
    if now - last < RATE_LIMIT_SECONDS:
        kalan = int(RATE_LIMIT_SECONDS - (now - last))
        send_msg(cid, f"⏳ Çok hızlı. {kalan // 60} dk {kalan % 60} sn bekleyin.")
        return False
    USER_COOLDOWN[cid] = now
    return True

# ==========================================
# ANALIZ AKISI
# ==========================================
def analiz_yap(cid):
    send_msg(cid, "🏀 ABD basketbol maçları taranıyor... (ESPN)")

    maclar = bugunun_maclari_al()
    toplam = len(maclar)

    if toplam == 0:
        tr_now = datetime.utcnow() + timedelta(hours=3)
        send_msg(cid, f"❌ ESPN'de ABD basketbol maçı bulunamadı.\n"
                      f"📅 Tarih: {tr_now.strftime('%d.%m.%Y %H:%M')} (TSİ)\n"
                      f"💡 /debug yazıp detay görebilirsin.")
        return

    secilenler = maclar[:MAX_TARANAN]
    send_msg(cid, f"📊 {toplam} maç bulundu. {len(secilenler)} maç analiz ediliyor... (3-5 dk)")

    adaylar = []
    for idx, m in enumerate(secilenler, 1):
        print(f"[{idx}/{len(secilenler)}] {m['ev']} vs {m['dep']}", flush=True)
        analiz = maci_analiz_et(m)
        if not analiz:
            continue

        tercih = analiz.get("tercih", "")
        if "ALT" not in tercih.upper():
            print(f"   ALT değil: {tercih}", flush=True)
            continue

        if analiz.get("olasilik", 0) < 55:
            print(f"   Olasılık düşük: %{analiz.get('olasilik')}", flush=True)
            continue

        adaylar.append({"mac": m, "analiz": analiz})
        print(f"   ✅ ADAY: {tercih} | %{analiz['olasilik']}", flush=True)

    if len(adaylar) < 1:
        send_msg(cid, "❌ ALT şartını geçen maç bulunamadı.")
        return

    adaylar.sort(key=lambda x: x["analiz"]["olasilik"], reverse=True)
    en_iyi = adaylar[0]
    m = en_iyi["mac"]
    a = en_iyi["analiz"]

    t = []
    t.append("🏀 BASKETBOL ALT KUPONU")
    t.append(f"⏰ {m['tarih'][:16].replace('T', ' ')}")
    t.append(f"🏆 {m['lig']}")
    t.append("━━━━━━━━━━━━━━━━━━")
    t.append("")
    t.append(f"⚡ {m['ev']} - {m['dep']}")
    t.append(f"📊 Kayıt: {m['ev_kayit']} vs {m['dep_kayit']}")
    t.append("")
    t.append(f"🎯 Tercih: *{a['tercih']}*")
    t.append(f"📊 Olasılık: %{a['olasilik']}")
    t.append(f"✅ Uyan şart: {a.get('uyan_sart', '?')}/7")
    t.append("")
    t.append(f"📝 {a.get('gerekce', '')}")
    t.append("")
    t.append("━━━━━━━━━━━━━━━━━━")
    t.append(f"📋 {len(adaylar)} maç ALT şartını geçti")
    t.append(f"⭐ En yüksek olasılıklı seçildi")
    t.append("")
    t.append("⚠️ Yatırım tavsiyesi değildir.")

    metin = "\n".join(t)
    aid = save_analiz(cid, f"{m['ev']} - {m['dep']}", a["tercih"], a["olasilik"])

    rm = None
    if aid:
        rm = {"inline_keyboard": [[
            {"text": "✅ Tuttu", "callback_data": f"sonuc:tuttu:{aid}"},
            {"text": "❌ Tutmadı", "callback_data": f"sonuc:tutmadi:{aid}"}
        ]]}

    send_msg(cid, metin, "Markdown", rm)

# ==========================================
# DEBUG
# ==========================================
def debug_basket(cid):
    tr_now = datetime.utcnow() + timedelta(hours=3)
    lines = [
        "🔧 *DEBUG - ESPN*",
        "",
        f"TR: `{tr_now.strftime('%d.%m.%Y %H:%M')}`",
        "",
        "📅 *3 Günlük Tarama:*",
    ]

    tarihler = []
    for offset in [-1, 0, 1]:
        t = (tr_now + timedelta(days=offset)).strftime("%Y%m%d")
        tarihler.append(t)

    toplam = 0
    for lig_adi, lig_kod in ESPN_LEAGUES.items():
        for tarih in tarihler:
            r = espn_call(lig_kod, tarih)
            if r:
                n = len(r.get("events", []))
                toplam += n
                if n > 0:
                    lines.append(f"• {lig_adi} `{tarih}` → *{n}* maç")

    lines.append("")
    lines.append(f"📊 *Toplam: {toplam} maç*")
    send_msg(cid, "\n".join(lines), "Markdown")

    maclar = bugunun_maclari_al()
    lines2 = [f"✅ *Bulunan: {len(maclar)} maç*", ""]
    for i, m in enumerate(maclar[:5], 1):
        lines2.append(f"{i}. [{m['lig']}] {m['ev']} vs {m['dep']}")
        lines2.append(f"   {m['tarih'][:16]} | {m['durum']}")
    send_msg(cid, "\n".join(lines2), "Markdown")

# ==========================================
# MENU
# ==========================================
def _ana_menu_buton():
    return {"inline_keyboard": [[{"text": "🔙 Ana Menü", "callback_data": "menu:ana"}]]}

def _menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "🏀 ALT Analiz Yap", "callback_data": "menu:analiz"}],
        [{"text": "📜 Geçmiş", "callback_data": "menu:gecmis"},
         {"text": "🔧 Debug", "callback_data": "menu:debug"}],
        [{"text": "❓ Yardım", "callback_data": "menu:yardim"}]
    ]}

def _menu_text():
    return ("🏀 *ABD BASKETBOL ALT BOTU*\n\n"
            "🎯 Sadece ALT (under) bahisleri\n"
            "📊 3-8 maç analiz eder\n"
            "🔍 ESPN verisi + Gemini analizi\n\n"
            "⬇️ Analiz için butona bas:")

def show_menu(cid, mid=None):
    if mid: edit_message_text(cid, mid, _menu_text(), "Markdown", _menu_keyboard())
    else: send_msg(cid, _menu_text(), "Markdown", _menu_keyboard())

def show_gecmis(cid, mid=None):
    rows = get_gecmis(cid, 10)
    if not rows: text = "📭 Geçmiş boş"
    else:
        s = {"tuttu": "✅", "tutmadi": "❌", None: "⏳"}
        sat = ["📜 SON 10 ANALİZ", ""]
        for i, (aid, tarih, mac, tercih, olasilik, sonuc) in enumerate(rows, 1):
            sat.append(f"{i}. {mac}")
            sat.append(f"   {tercih} | %{olasilik} | {s.get(sonuc,'?')} {tarih}")
        text = "\n".join(sat)
    if mid: edit_message_text(cid, mid, text, None, _ana_menu_buton())
    else: send_msg(cid, text, None, _ana_menu_buton())

def show_yardim(cid, mid=None):
    text = ("❓ YARDIM\n\n"
            "🏀 Sadece ABD basketbol (NBA/WNBA/NCAA)\n"
            "🎯 Sadece ALT bahisleri\n"
            "📊 ESPN verisi + Gemini analizi\n\n"
            "💡 NBA sezonu Ekim-Haziran\n"
            "⚠️ Yatırım tavsiyesi değildir.")
    if mid: edit_message_text(cid, mid, text, None, _ana_menu_buton())
    else: send_msg(cid, text, None, _ana_menu_buton())

# ==========================================
# KOMUTLAR
# ==========================================
def handle_command(cid, text):
    text = (text or "").strip()
    cmd = text.split()[0].lower() if text else ""

    if cmd in ("/menu", "/start"): show_menu(cid); return
    if cmd == "/yardim": show_yardim(cid); return
    if cmd == "/gecmis": show_gecmis(cid); return
    if cmd == "/debug":
        threading.Thread(target=debug_basket, args=(cid,), daemon=True).start(); return
    if cmd == "/analiz":
        if not check_rate_limit(cid): return
        threading.Thread(target=analiz_yap, args=(cid,), daemon=True).start()
        return
    send_msg(cid, "ℹ️ /start yazarak başlayın.")

def handle_callback(cq):
    try:
        cid = cq["message"]["chat"]["id"]
        mid = cq["message"]["message_id"]
        data = cq.get("data", "")
        cb_id = cq["id"]
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id}, timeout=10)

        if int(cid) != int(ADMIN_ID):
            send_msg(cid, "🔒 Bu bot özel kullanımdadır."); return

        if data == "menu:ana": show_menu(cid, mid); return
        if data == "menu:gecmis": show_gecmis(cid, mid); return
        if data == "menu:yardim": show_yardim(cid, mid); return
        if data == "menu:debug":
            threading.Thread(target=debug_basket, args=(cid,), daemon=True).start(); return
        if data == "menu:analiz":
            if not check_rate_limit(cid): return
            edit_reply_markup(cid, mid, _ana_menu_buton())
            threading.Thread(target=analiz_yap, args=(cid,), daemon=True).start()
            return
        if data.startswith("sonuc:"):
            _, sonuc, aid_str = data.split(":")
            try:
                with _DB_LOCK:
                    conn = db_conn()
                    conn.execute("UPDATE analizler SET sonuc=? WHERE id=? AND sonuc IS NULL", (sonuc, int(aid_str)))
                    conn.commit(); conn.close()
                edit_reply_markup(cid, mid, None)
                e = "✅" if sonuc == "tuttu" else "❌"
                send_msg(cid, f"{e} Analiz #{aid_str} kaydedildi.")
            except Exception as e:
                print(f"sonuc hata: {e}", flush=True)
    except Exception as e:
        print(f"callback: {e}", flush=True)

# ==========================================
# ANA DONGU
# ==========================================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    init_db()
    print("=== ABD BASKETBOL ALT BOTU (ESPN) BASLADI ===", flush=True)
    print(f"Gemini: {'VAR' if GEMINI_API_KEY else 'YOK'}", flush=True)
    print(f"Telegram: {'VAR' if TELEGRAM_TOKEN else 'YOK'}", flush=True)

    offset = get_offset()

    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                             params={"offset": offset, "timeout": 30}, timeout=40).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                save_offset(offset)
                if "callback_query" in u:
                    handle_callback(u["callback_query"]); continue
                msg = u.get("message", {})
                cid = msg.get("chat", {}).get("id")
                if not cid: continue
                if int(cid) != int(ADMIN_ID):
                    try: send_msg(cid, "🔒 Bu bot özel kullanımdadır.")
                    except: pass
                    continue
                text = msg.get("text", "")
                if text: handle_command(cid, text); continue
                send_msg(cid, "ℹ️ /start yazarak başlayın.")
        except Exception as e:
            print(f"=== LOOP: {e} ===", flush=True)
            time.sleep(3)

if __name__ == "__main__":
    main()
