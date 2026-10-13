import os
import io, json, time, requests, threading, sqlite3
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# AYARLAR
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
API_BASKETBALL_KEY = os.environ.get("API_BASKETBALL_KEY")

GEMINI_MODEL = "gemini-3.1-pro-preview"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

API_BASKETBALL_BASE = "https://v1.basketball.api-sports.io"
API_HEADERS = {"x-apisports-key": API_BASKETBALL_KEY}

ADMIN_ID = 5504006147

GEMINI_LOCK = threading.Lock()
SON_ISTEK_ZAMANI = [0.0]
MIN_ISTEK_ARASI = 3.0

# Ayarlar
MIN_TARANAN = 3   # En az 3 mac
MAX_TARANAN = 8   # En fazla 8 mac

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
# API-BASKETBALL
# ==========================================
def api_call(endpoint, params=None):
    try:
        r = requests.get(f"{API_BASKETBALL_BASE}/{endpoint}",
                         headers=API_HEADERS, params=params or {}, timeout=20)
        if r.status_code != 200:
            print(f"API hatasi: {r.status_code}", flush=True)
            return None
        return r.json()
    except Exception as e:
        print(f"API istek: {str(e)[:150]}", flush=True)
        return None

def bugunun_maclari_al():
    """Bugunun ABD basketbol maclari."""
    bugun = datetime.now().strftime("%Y-%m-%d")
    r = api_call("games", {"date": bugun})
    if not r: return []

    maclar = []
    for g in r.get("response", []):
        try:
            # ABD kontrolu
            country = ""
            if isinstance(g.get("country"), dict):
                country = g["country"].get("name", "")
            elif isinstance(g.get("country"), str):
                country = g["country"]
            elif isinstance(g.get("league"), dict) and isinstance(g["league"].get("country"), str):
                country = g["league"]["country"]

            league_name = ""
            if isinstance(g.get("league"), dict):
                league_name = g["league"].get("name", "")

            # ABD ligleri: NBA, WNBA, NCAA, G-League, hazirlik
            us_terms = ["usa", "united states", "nba", "wnba", "ncaa", "g-league", "gleague"]
            is_us = country.lower() in ["usa", "united states", "us"] or \
                    any(t in league_name.lower() for t in us_terms)

            if is_us:
                maclar.append(g)
        except: continue

    print(f"Bulunan ABD mac: {len(maclar)}", flush=True)
    return maclar

def mac_detay_al(game_id):
    r = api_call("games", {"id": game_id})
    if not r: return None
    resp = r.get("response", [])
    return resp[0] if resp else None

def mac_istatistik_al(game_id):
    r = api_call("statistics", {"id": game_id})
    if not r: return None
    return r.get("response", [])

def takim_sakatlik_al(team_id, season):
    r = api_call("injuries", {"team": team_id, "season": season})
    if not r: return None
    return r.get("response", [])

def h2h_al(team1_id, team2_id):
    r = api_call("games", {"h2h": f"{team1_id}-{team2_id}"})
    if not r: return None
    return r.get("response", [])[:5]

def puan_durumu_al(league_id, season):
    r = api_call("standings", {"league": league_id, "season": season})
    if not r: return None
    resp = r.get("response", [])
    if resp and isinstance(resp, list):
        return resp[0] if resp else None
    return None

def takim_sezon_istatistik_al(team_id, league_id, season):
    r = api_call("statistics", {"team": team_id, "league": league_id, "season": season})
    if not r: return None
    return r.get("response", {})

# ==========================================
# OZETLEME
# ==========================================
def puan_durumu_ozetle(pd, ev_id, dep_id):
    if not pd: return "Puan durumu yok"
    satirlar = []
    try:
        standings = pd.get("standings", [])
        if standings and isinstance(standings, list):
            for group in standings:
                if isinstance(group, list):
                    for s in group:
                        tid = s.get("team", {}).get("id")
                        if tid in (ev_id, dep_id):
                            isim = s.get("team", {}).get("name", "?")
                            poz = s.get("position", "?")
                            puan = s.get("points", "?")
                            satirlar.append(f"{isim}: {poz}. sira, {puan} puan")
    except Exception as e:
        print(f"pd ozet: {e}", flush=True)
    return "\n".join(satirlar) if satirlar else "Puan durumu yok"

def h2h_ozetle(h2h_list):
    if not h2h_list: return "H2H verisi yok"
    satirlar = []
    alt_sayisi = 0
    for m in h2h_list:
        try:
            ev = m["teams"]["home"]["name"]
            dep = m["teams"]["away"]["name"]
            ev_p = m["scores"]["home"].get("total") or 0
            dep_p = m["scores"]["away"].get("total") or 0
            toplam = ev_p + dep_p
            tarih = m["date"][:10]
            satirlar.append(f"{tarih}: {ev} {ev_p}-{dep_p} {dep} (TOP: {toplam})")
        except: continue
    return "\n".join(satirlar) if satirlar else "H2H verisi yok"

def sakatlik_ozetle(inj_list, team_id, team_name):
    if not inj_list: return f"{team_name}: Sakatlik bilgisi yok"
    satirlar = []
    for i in inj_list:
        try:
            if i.get("team", {}).get("id") != team_id: continue
            oyuncu = i.get("player", {}).get("name", "?")
            tip = i.get("type", "?")
            satirlar.append(f"{oyuncu} ({tip})")
        except: continue
    if satirlar:
        return f"{team_name} sakatlar: " + ", ".join(satirlar)
    return f"{team_name}: Sakat oyuncu yok"

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

MAC_PROMPT = """Sen ABD basketbol (NBA, WNBA, NCAA, hazirlik maclari) analiz uzmanisin. 15+ yillik deneyimli profesyonelsin.

=== MAC BILGILERI ===
Lig: {lig}
Mac: {ev} vs {dep}
Tarih: {tarih}
Durum: {durum}

=== PUAN DURUMU ===
{puan_durumu}

=== H2H (SON 5 MAC) ===
{h2h}

=== SAKATLIKLAR ===
{ev_sakat}
{dep_sakat}

=== TAKIM SEZON ISTATISTIKLERI ===
{ev_stats}

{dep_stats}

=== GOREV ===
Bu mac icin **TOPLAM SAYI ALT (under)** bahsi degerlendir.

SADECE SU TERCİHLERDEN BIRINI SEÇ:
- X.5 ALT (ornek: 220.5 ALT, 235.5 ALT, 278.5 ALT)

ALT ICIN ARANAN SARTLAR (kac tanesi uyuyor?):
1. Iki takimin sayi ortalamasi dusuk mu?
2. Savunma guclu mu (son maclarda)?
3. Sakat oyuncular var mi (ozellikle skorer)?
4. H2H'de ALT orani yuksek mi (3+/5)?
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
- Gerekce 2-3 cumle, EN ONEMLI sebepler
- Olasilik %55-95 arasi

=== CIKTI (SADECE JSON) ===
{{
  "tercih": "235.5 ALT",
  "olasilik": 75,
  "gerekce": "...",
  "guven": 78,
  "uyan_sart": 5
}}"""

def maci_analiz_et(mac, ist, ev_sak, dep_sak, h2h, pd, ev_stats, dep_stats):
    try:
        lid = mac["league"]["id"]
        lig = mac["league"]["name"]
        sezon = mac.get("season", str(datetime.now().year))
        ev_id = mac["teams"]["home"]["id"]
        dep_id = mac["teams"]["away"]["id"]
        ev = mac["teams"]["home"]["name"]
        dep = mac["teams"]["away"]["name"]
        tarih = mac.get("date", "")[:16].replace("T", " ")
        durum = mac.get("status", {}).get("long", "?")

        ev_sak_str = sakatlik_ozetle(ev_sak, ev_id, ev)
        dep_sak_str = sakatlik_ozetle(dep_sak, dep_id, dep)
        h2h_str = h2h_ozetle(h2h)
        pd_str = puan_durumu_ozetle(pd, ev_id, dep_id)
        ev_stats_str = f"{ev}: {json.dumps(ev_stats, ensure_ascii=False)[:400]}" if ev_stats else f"{ev}: istatistik yok"
        dep_stats_str = f"{dep}: {json.dumps(dep_stats, ensure_ascii=False)[:400]}" if dep_stats else f"{dep}: istatistik yok"

        prompt = MAC_PROMPT.format(
            lig=lig, ev=ev, dep=dep, tarih=tarih, durum=durum,
            puan_durumu=pd_str, h2h=h2h_str,
            ev_sakat=ev_sak_str, dep_sakat=dep_sak_str,
            ev_stats=ev_stats_str, dep_stats=dep_stats_str
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
    send_msg(cid, "🏀 ABD basketbol maçları taranıyor...")

    maclar = bugunun_maclari_al()
    toplam = len(maclar)

    if toplam == 0:
        send_msg(cid, "❌ Bugün ABD basketbol maçı yok.")
        return

    if toplam < MIN_TARANAN:
        send_msg(cid, f"ℹ️ Bugün sadece {toplam} maç var. Yine de analiz ediyorum...")

    # Max 8 maç
    secilenler = maclar[:MAX_TARANAN]

    send_msg(cid, f"📊 Toplam {toplam} maç bulundu. {len(secilenler)} maç analiz ediliyor... (3-5 dk)")

    adaylar = []
    for idx, mac in enumerate(secilenler, 1):
        try:
            ev = mac["teams"]["home"]["name"]
            dep = mac["teams"]["away"]["name"]
            gid = mac["id"]
            lid = mac["league"]["id"]
            sezon = mac.get("season", str(datetime.now().year))
            ev_id = mac["teams"]["home"]["id"]
            dep_id = mac["teams"]["away"]["id"]

            print(f"[{idx}/{len(secilenler)}] {ev} vs {dep}", flush=True)

            ist = mac_istatistik_al(gid)
            ev_sak = takim_sakatlik_al(ev_id, sezon)
            dep_sak = takim_sakatlik_al(dep_id, sezon)
            h2h = h2h_al(ev_id, dep_id)
            pd = puan_durumu_al(lid, sezon)
            ev_stats = takim_sezon_istatistik_al(ev_id, lid, sezon)
            dep_stats = takim_sezon_istatistik_al(dep_id, lid, sezon)

            analiz = maci_analiz_et(mac, ist, ev_sak, dep_sak, h2h, pd, ev_stats, dep_stats)
            if not analiz:
                continue

            tercih = analiz.get("tercih", "")
            if "ALT" not in tercih.upper():
                print(f"   ⏭️ ALT değil: {tercih}", flush=True)
                continue

            if analiz.get("olasilik", 0) < 55:
                print(f"   ⏭️ Olasılık düşük: %{analiz.get('olasilik')}", flush=True)
                continue

            adaylar.append({
                "mac": mac,
                "analiz": analiz
            })
            print(f"   ✅ ADAY: {tercih} | %{analiz['olasilik']}", flush=True)
        except Exception as e:
            print(f"   Hata: {e}", flush=True)
            continue

    if len(adaylar) < 1:
        send_msg(cid, "❌ Bugün ALT şartını geçen maç bulunamadı.")
        return

    # En yüksek olasılıklıyı seç
    adaylar.sort(key=lambda x: x["analiz"]["olasilik"], reverse=True)
    en_iyi = adaylar[0]

    mac = en_iyi["mac"]
    a = en_iyi["analiz"]
    ev = mac["teams"]["home"]["name"]
    dep = mac["teams"]["away"]["name"]
    tarih = mac.get("date", "")[:16].replace("T", " ")
    lig = mac["league"]["name"]

    t = []
    t.append("🏀 BASKETBOL ALT KUPONU")
    t.append(f"⏰ {tarih}")
    t.append(f"🏆 {lig}")
    t.append("━━━━━━━━━━━━━━━━━━")
    t.append("")
    t.append(f"⚡ {ev} - {dep}")
    t.append("")
    t.append(f"🎯 Tercih: *{a['tercih']}*")
    t.append(f"📊 Olasılık: %{a['olasilik']}")
    t.append(f"✅ Uyan şart: {a.get('uyan_sart', '?')}/7")
    t.append("")
    t.append(f"📝 {a.get('gerekce', '')}")
    t.append("")
    t.append("━━━━━━━━━━━━━━━━━━")
    t.append(f"📋 Toplam {len(adaylar)} maç ALT şartını geçti")
    t.append(f"⭐ En yüksek olasılıklı seçildi")
    t.append("")
    t.append("⚠️ Yatırım tavsiyesi değildir.")

    metin = "\n".join(t)

    aid = save_analiz(cid, f"{ev} - {dep}", a["tercih"], a["olasilik"])

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
    send_msg(cid, "🔧 ABD basketbol maçları taranıyor...")
    maclar = bugunun_maclari_al()
    if not maclar:
        send_msg(cid, "❌ Bugün ABD maçı yok.")
        return
    lines = [f"📊 Toplam {len(maclar)} ABD maçı", ""]
    for i, m in enumerate(maclar[:10], 1):
        try:
            lig = m["league"]["name"]
            ev = m["teams"]["home"]["name"]
            dep = m["teams"]["away"]["name"]
            saat = m.get("date", "")[11:16]
            lines.append(f"{i}. [{lig}] {ev} vs {dep} ({saat})")
        except: continue
    send_msg(cid, "\n".join(lines))

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
            "🔍 Sakatlık + Puan + H2H + İstatistik\n"
            "🧠 Gemini 3.1 Pro analizi\n\n"
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
            "📊 3-8 maç analiz eder\n"
            "🔍 Sakatlık + Puan + H2H + İstatistik\n"
            "🧠 Gemini 3.1 Pro\n\n"
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
    print("=== ABD BASKETBOL ALT BOTU BASLADI ===", flush=True)
    print(f"API-Basketball: {'VAR' if API_BASKETBALL_KEY else 'YOK'}", flush=True)
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
