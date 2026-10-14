import os
import json, time, requests, threading, sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# AYARLAR
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

GEMINI_MODEL = "gemini-3.1-pro-preview"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_CORE_BASE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues"
ESPN_WEB_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/basketball"

ESPN_LEAGUES = {
    "NBA": "nba",
    "WNBA": "wnba",
    "NCAA": "mens-college-basketball",
}

ADMIN_ID = 5504006147

GEMINI_LOCK = threading.Lock()
SON_ISTEK_ZAMANI = [0.0]
MIN_ISTEK_ARASI = 3.0

_ESPN_LOCK = threading.Lock()
_SON_ESPN = [0.0]
_ESPN_MIN_ARASI = 0.4

EMNIYET_MARJI = 10

_CACHE = {}
_CACHE_TTL = 300
_CACHE_MISS = object()

def _cache_get(key):
    now = time.time()
    if key in _CACHE:
        val, ts = _CACHE[key]
        if now - ts < _CACHE_TTL:
            return val
        else:
            del _CACHE[key]
    return _CACHE_MISS

def _cache_set(key, val):
    _CACHE[key] = (val, time.time())

MIN_TARANAN = 3
MAX_TARANAN = 8
AYNI_MAC_ENGELLE_DK = 30

SONUC_KONTROL_ARASI = 300  # 5 dk

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
                cid INTEGER, tarih TEXT, mac TEXT, event_id TEXT,
                lig TEXT, tercih TEXT, olasilik INTEGER,
                kontrol_edildi INTEGER DEFAULT 0,
                sonuc TEXT DEFAULT NULL, ts INTEGER)""")
            mevcut = [row[1] for row in conn.execute("PRAGMA table_info(analizler)").fetchall()]
            if "event_id" not in mevcut:
                conn.execute("ALTER TABLE analizler ADD COLUMN event_id TEXT")
            if "lig" not in mevcut:
                conn.execute("ALTER TABLE analizler ADD COLUMN lig TEXT")
            if "kontrol_edildi" not in mevcut:
                conn.execute("ALTER TABLE analizler ADD COLUMN kontrol_edildi INTEGER DEFAULT 0")
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

def save_analiz(cid, mac, event_id, lig, tercih, olasilik):
    try:
        with _DB_LOCK:
            conn = db_conn()
            cur = conn.execute("""INSERT INTO analizler
                (cid, tarih, mac, event_id, lig, tercih, olasilik, kontrol_edildi, ts)
                VALUES (?,?,?,?,?,?,?,0,?)""",
                (cid, datetime.now().strftime("%d.%m.%Y %H:%M"),
                 mac, event_id, lig, tercih, olasilik, int(time.time())))
            conn.commit()
            rid = cur.lastrowid; conn.close()
            return rid
    except Exception as e:
        print(f"save hatasi: {e}", flush=True); return None

def son_onekli_event_idleri():
    try:
        esik = int(time.time()) - (AYNI_MAC_ENGELLE_DK * 60)
        with _DB_LOCK:
            conn = db_conn()
            cur = conn.execute("""SELECT event_id FROM analizler
                WHERE ts >= ? AND event_id IS NOT NULL""", (esik,))
            rows = cur.fetchall(); conn.close()
            return set(r[0] for r in rows)
    except: return set()

def bekleyen_kuponlar():
    try:
        with _DB_LOCK:
            conn = db_conn()
            cur = conn.execute("""SELECT id, cid, event_id, lig, tercih
                FROM analizler
                WHERE kontrol_edildi=0 AND event_id IS NOT NULL AND sonuc IS NULL""")
            rows = cur.fetchall(); conn.close()
            return rows
    except: return []

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
# ESPN
# ==========================================
def espn_get(url, params=None, timeout=15):
    with _ESPN_LOCK:
        gecen = time.time() - _SON_ESPN[0]
        if gecen < _ESPN_MIN_ARASI:
            time.sleep(_ESPN_MIN_ARASI - gecen)
        try:
            r = requests.get(url, params=params or {}, timeout=timeout)
            _SON_ESPN[0] = time.time()
            if r.status_code != 200:
                return None
            return r.json()
        except:
            return None

def espn_season_yili():
    simdi = datetime.now()
    if simdi.month >= 9:
        return simdi.year + 1
    return simdi.year

# ==========================================
# TAKIM ISTATISTIKLERI
# ==========================================
def takim_sezon_istatistik_al(lig_kod, team_id):
    key = f"stats_{lig_kod}_{team_id}"
    cached = _cache_get(key)
    if cached is not _CACHE_MISS:
        return cached
    try:
        yil = espn_season_yili()
        url = f"{ESPN_CORE_BASE}/{lig_kod}/seasons/{yil}/types/2/teams/{team_id}/statistics"
        data = espn_get(url)
        if not data:
            _cache_set(key, None); return None
        sonuc = {"ppg": None, "papg": None, "pace": None, "fg_pct": None}
        splits = data.get("splits", {})
        for cat in splits.get("categories", []):
            for s in cat.get("stats", []):
                name = s.get("name", "")
                pg = s.get("perGameValue")
                val = s.get("value")
                if name in ("avgPointsFor", "points"):
                    if pg is not None: sonuc["ppg"] = pg
                elif name == "avgPointsAgainst":
                    if pg is not None: sonuc["papg"] = pg
                elif name in ("paceFactor", "possessions"):
                    if pg is not None: sonuc["pace"] = pg
                elif name == "fieldGoalPct":
                    sonuc["fg_pct"] = val
        _cache_set(key, sonuc)
        return sonuc
    except: return None

def takim_istatistik_ozetle(s, team_name):
    if not s: return f"{team_name}: sezon istatistigi yok"
    satirlar = [f"{team_name}:"]
    if s.get("ppg") is not None: satirlar.append(f"  • PPG: {round(s['ppg'], 1)}")
    if s.get("papg") is not None: satirlar.append(f"  • PAPG: {round(s['papg'], 1)}")
    if s.get("pace") is not None: satirlar.append(f"  • Pace: {round(s['pace'], 1)}")
    if s.get("fg_pct") is not None: satirlar.append(f"  • FG%: {round(s['fg_pct']*100, 1)}")
    return "\n".join(satirlar)

# ==========================================
# SCHEDULE / FORM / B2B
# ==========================================
def takim_schedule_al(lig_kod, team_id):
    key = f"sch_{lig_kod}_{team_id}"
    cached = _cache_get(key)
    if cached is not _CACHE_MISS: return cached
    try:
        url = f"{ESPN_SITE_BASE}/{lig_kod}/teams/{team_id}/schedule"
        data = espn_get(url)
        if not data:
            _cache_set(key, []); return []
        events = data.get("events", [])
        _cache_set(key, events)
        return events
    except: return []

def form_ve_b2b(schedule_events, mac_tarih_iso, limit=5):
    try:
        mac_tarihi = datetime.fromisoformat(mac_tarih_iso.replace("Z", "+00:00"))
    except: return [], None
    bitmis = []
    onceki_tarihler = []
    for ev in schedule_events:
        try:
            ev_tarih_iso = ev.get("date", "")
            ev_tarih = datetime.fromisoformat(ev_tarih_iso.replace("Z", "+00:00"))
            if ev_tarih >= mac_tarihi: continue
            onceki_tarihler.append(ev_tarih)
            comp = ev.get("competitions", [{}])[0]
            state = comp.get("status", {}).get("type", {}).get("state", "")
            if state != "post": continue
            competitors = comp.get("competitors", [])
            ev_t = next((c for c in competitors if c.get("homeAway") == "home"), None)
            dep_t = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not ev_t or not dep_t: continue
            ev_skor = int(ev_t.get("score", 0) or 0)
            dep_skor = int(dep_t.get("score", 0) or 0)
            bitmis.append({
                "tarih": ev_tarih_iso[:10],
                "ev": ev_t.get("team", {}).get("displayName", "?"),
                "dep": dep_t.get("team", {}).get("displayName", "?"),
                "ev_skor": ev_skor, "dep_skor": dep_skor,
                "toplam": ev_skor + dep_skor,
            })
        except: continue
    bitmis.sort(key=lambda x: x["tarih"], reverse=True)
    bitmis = bitmis[:limit]
    b2b = None
    if onceki_tarihler:
        onceki_tarihler.sort(reverse=True)
        b2b = (mac_tarihi - onceki_tarihler[0]).days
    return bitmis, b2b

def form_ozetle(maclar, team_name):
    if not maclar: return f"{team_name}: son mac yok"
    satirlar = [f"{team_name} son {len(maclar)} mac:"]
    toplamlar = []
    for m in maclar:
        satirlar.append(f"  • {m['tarih']}: TOP {m['toplam']}")
        toplamlar.append(m["toplam"])
    if toplamlar:
        ort = sum(toplamlar) / len(toplamlar)
        satirlar.append(f"  • Ort: {round(ort, 1)}")
    return "\n".join(satirlar)

def b2b_ozetle(gun, team_name):
    if gun is None: return f"{team_name}: bilinmiyor"
    if gun == 0: return f"⚠️ {team_name}: BACK-TO-BACK"
    elif gun == 1: return f"{team_name}: 1 gun (yorgun)"
    elif gun >= 3: return f"{team_name}: {gun} gun (iyi)"
    return f"{team_name}: {gun} gun"

# ==========================================
# EV/DEPLASMAN
# ==========================================
def ev_deplasman_performans(lig_kod, team_id, ev_mi=True):
    key = f"perf_{lig_kod}_{team_id}_{ev_mi}"
    cached = _cache_get(key)
    if cached is not _CACHE_MISS: return cached
    try:
        yil = espn_season_yili()
        url = f"{ESPN_CORE_BASE}/{lig_kod}/seasons/{yil}/types/2/teams/{team_id}/record"
        data = espn_get(url)
        if not data:
            _cache_set(key, None); return None
        for it in data.get("items", []):
            rec_type = it.get("type", "")
            if ev_mi and rec_type == "home":
                s = it.get("summary", "")
                _cache_set(key, s); return s
            if not ev_mi and rec_type == "road":
                s = it.get("summary", "")
                _cache_set(key, s); return s
        _cache_set(key, None); return None
    except: return None

# ==========================================
# SAKATLIKLAR
# ==========================================
def takim_sakatliklari_al(lig_kod):
    key = f"inj_{lig_kod}"
    cached = _cache_get(key)
    if cached is not _CACHE_MISS: return cached
    try:
        url = f"{ESPN_SITE_BASE}/{lig_kod}/injuries"
        data = espn_get(url)
        if not data:
            _cache_set(key, {}); return {}
        inj_map = {}
        for team in data.get("injuries", []):
            team_name = team.get("displayName", "")
            injuries = team.get("injuries", [])
            if injuries: inj_map[team_name] = injuries
        _cache_set(key, inj_map)
        return inj_map
    except: return {}

# ==========================================
# MAC PARSE
# ==========================================
def _mac_parse(event, lig_adi):
    try:
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away: return None
        durum_state = comp.get("status", {}).get("type", {}).get("state", "pre")
        odds = comp.get("odds", [])
        odds_metni = ""
        if odds:
            o = odds[0]
            ou = o.get("overUnder")
            detay = o.get("details", "")
            odds_metni = f"O/U: {ou} | Spread: {detay}" if ou else detay
        if not odds_metni: odds_metni = "Odds bilgisi yok"
        return {
            "lig": lig_adi, "event_id": event.get("id", ""),
            "ev_id": home.get("team", {}).get("id", ""),
            "dep_id": away.get("team", {}).get("id", ""),
            "ev": home.get("team", {}).get("displayName", "?"),
            "dep": away.get("team", {}).get("displayName", "?"),
            "ev_kayit": next((r.get("summary") for r in home.get("records", []) if r.get("type") == "total"), ""),
            "dep_kayit": next((r.get("summary") for r in away.get("records", []) if r.get("type") == "total"), ""),
            "tarih": event.get("date", ""),
            "durum": comp.get("status", {}).get("type", {}).get("description", "Scheduled"),
            "durum_state": durum_state,
            "odds": odds_metni,
            "venue": comp.get("venue", {}).get("fullName", ""),
        }
    except: return None

def bugunun_maclari_al():
    tr_now = datetime.now(timezone.utc) + timedelta(hours=3)
    tarihler = []
    for offset in [0, 1]:
        t = (tr_now + timedelta(days=offset)).strftime("%Y%m%d")
        tarihler.append(t)
    maclar = []
    for lig_adi, lig_kod in ESPN_LEAGUES.items():
        for tarih in tarihler:
            data = espn_get(f"{ESPN_SITE_BASE}/{lig_kod}/scoreboard", {"dates": tarih})
            if not data: continue
            for ev in data.get("events", []):
                parsed = _mac_parse(ev, lig_adi)
                if parsed and parsed["durum_state"] == "pre":
                    maclar.append(parsed)
    maclar.sort(key=lambda x: x.get("tarih", ""))
    return maclar

def mac_final_mi(lig_kod, event_id):
    try:
        data = espn_get(f"{ESPN_WEB_BASE}/{lig_kod}/summary", {"event": event_id})
        if not data: return None
        comp = data.get("header", {}).get("competitions", [{}])[0]
        state = comp.get("status", {}).get("type", {}).get("state", "")
        if state != "post": return None
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away: return None
        return int(home.get("score", 0) or 0) + int(away.get("score", 0) or 0)
    except: return None

def tercih_tuttumu(tercih, toplam_sayi):
    if not tercih: return None
    try:
        temiz = tercih.upper().replace("ALT", "").strip()
        cizgi = float(temiz)
        return toplam_sayi < cizgi
    except: return None

# ==========================================
# EMNIYET MARJI
# ==========================================
def emniyet_marji_uygula(tercih, marj):
    if not tercih or "ALT" not in tercih.upper():
        return tercih
    try:
        temiz = tercih.upper().replace("ALT", "").strip()
        sayi = float(temiz)
        yeni = sayi + marj
        return f"{yeni} ALT"
    except: return tercih

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
Salon: {venue}

=== EV SAHIBI: {ev} ===
Kayit: {ev_kayit}
{ev_stats}
Dinlenme: {ev_b2b}
Ev performansi: {ev_ev_perf}

=== DEPLASMAN: {dep} ===
Kayit: {dep_kayit}
{dep_stats}
Dinlenme: {dep_b2b}
Deplasman performansi: {dep_dep_perf}

=== BAHIS ORANLARI ===
{odds}

=== SAKATLIKLAR ===
{ev_sakat}
{dep_sakat}

=== SON MACLAR / FORM ===
{ev_form}

{dep_form}

=== H2H ===
{h2h}

=== GOREV ===
Bu mac icin **TOPLAM SAYI ALT (under)** bahsi degerlendir.

SADECE SU TERCİHLERDEN BIRINI SEÇ:
- X.5 ALT (ornek: 220.5 ALT, 235.5 ALT)

ALT ICIN ARANAN SARTLAR:
1. PPG/PAPG ortalamasi dusuk mu?
2. Savunma guclu mu?
3. Sakat oyuncular var mi?
4. H2H'de ALT orani yuksek mi?
5. Takimlar back-to-back mi?
6. Mac onemsiz mi (preseason)?
7. Tempo (Pace) dusuk mu?

KARAR:
- 4+ sart uyuyorsa -> "X.5 ALT" ver
- 2-3 sart uyuyorsa -> "BEKLE" ver
- 2'den az -> "BEKLE"

ONEMLI:
- SADECE ALT tercihi ver, UST ASLA VERME
- Toplam sayi cizgisi SEN belirle
- Gercekci bir cizgi sec
- Gerekce 2-3 cumle
- Olasilik %55-95

=== CIKTI (SADECE JSON) ===
{{
  "tercih": "225.5 ALT",
  "olasilik": 75,
  "gerekce": "...",
  "guven": 78,
  "uyan_sart": 5
}}"""

def maci_analiz_et(m, sakatliklar, h2h_verisi, ev_stats, dep_stats,
                  ev_form, dep_form, ev_b2b, dep_b2b,
                  ev_ev_perf, dep_dep_perf):
    try:
        ev_sakat = f"{m['ev']} sakat: "
        dep_sakat = f"{m['dep']} sakat: "
        if sakatliklar.get(m["ev"]):
            ev_sakat += ", ".join([f"{i.get('athlete',{}).get('displayName','?')} ({i.get('status','?')})" for i in sakatliklar[m["ev"]]])
        else: ev_sakat += "Yok"
        if sakatliklar.get(m["dep"]):
            dep_sakat += ", ".join([f"{i.get('athlete',{}).get('displayName','?')} ({i.get('status','?')})" for i in sakatliklar[m["dep"]]])
        else: dep_sakat += "Yok"

        h2h_metni = "H2H verisi yok"
        if h2h_verisi:
            satirlar = []
            for g in h2h_verisi[:5]:
                try:
                    comp = g["competitions"][0]
                    t1 = comp["competitors"][0]
                    t2 = comp["competitors"][1]
                    toplam = int(t1.get("score", 0) or 0) + int(t2.get("score", 0) or 0)
                    tarih = g.get("date", "")[:10]
                    satirlar.append(f"{tarih}: {t1['team']['displayName']} {t1.get('score','0')}-{t2.get('score','0')} {t2['team']['displayName']} (TOP: {toplam})")
                except: continue
            if satirlar: h2h_metni = "\n".join(satirlar)

        prompt = MAC_PROMPT.format(
            lig=m["lig"], ev=m["ev"], dep=m["dep"],
            tarih=m["tarih"][:16].replace("T", " "),
            venue=m["venue"], odds=m["odds"],
            ev_kayit=m["ev_kayit"], dep_kayit=m["dep_kayit"],
            ev_stats=takim_istatistik_ozetle(ev_stats, m["ev"]),
            dep_stats=takim_istatistik_ozetle(dep_stats, m["dep"]),
            ev_b2b=b2b_ozetle(ev_b2b, m["ev"]),
            dep_b2b=b2b_ozetle(dep_b2b, m["dep"]),
            ev_ev_perf=ev_ev_perf or "Bilinmiyor",
            dep_dep_perf=dep_dep_perf or "Bilinmiyor",
            ev_sakat=ev_sakat, dep_sakat=dep_sakat,
            ev_form=form_ozetle(ev_form, m["ev"]),
            dep_form=form_ozetle(dep_form, m["dep"]),
            h2h=h2h_metni
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
        if "```json" in text: text = text.split("```json")[1].split("```")[0]
        return json.loads(text)
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
# OTOMATIK SONUC
# ==========================================
def sonuc_kontrol_worker():
    print("Sonuc kontrol worker basladi (5 dk aralik)", flush=True)
    time.sleep(60)
    while True:
        try:
            bekleyenler = bekleyen_kuponlar()
            if bekleyenler:
                print(f"Bekleyen kupon: {len(bekleyenler)}", flush=True)
            for (kid, cid, event_id, lig, tercih) in bekleyenler:
                try:
                    lig_kod = ESPN_LEAGUES.get(lig, "nba")
                    toplam = mac_final_mi(lig_kod, event_id)
                    if toplam is None: continue
                    tuttu = tercih_tuttumu(tercih, toplam)
                    if tuttu is None: continue
                    sonuc = "tuttu" if tuttu else "tutmadi"
                    with _DB_LOCK:
                        conn = db_conn()
                        conn.execute("UPDATE analizler SET sonuc=?, kontrol_edildi=1 WHERE id=?", (sonuc, kid))
                        conn.commit(); conn.close()
                    e = "✅" if tuttu else "❌"
                    send_msg(cid, f"{e} *OTOMATIK SONUC*\n\n"
                                  f"🎯 Kupon #{kid}\n"
                                  f"⚡ {tercih}\n"
                                  f"📊 Toplam sayı: *{toplam}*\n"
                                  f"🏆 Sonuç: *{sonuc.upper()}*", "Markdown")
                except Exception as e:
                    print(f"Kupon kontrol hatasi: {str(e)[:150]}", flush=True)
        except Exception as e:
            print(f"Worker hatasi: {str(e)[:150]}", flush=True)
        time.sleep(SONUC_KONTROL_ARASI)

# ==========================================
# ANALIZ AKISI
# ==========================================
def analiz_yap(cid):
    send_msg(cid, "🏀 Yaklaşan ABD basketbol maçları taranıyor...")
    maclar = bugunun_maclari_al()
    toplam = len(maclar)
    if toplam == 0:
        send_msg(cid, "❌ Yaklaşan ABD basketbol maçı bulunamadı.")
        return
    onerilen_idler = son_onekli_event_idleri()
    if onerilen_idler:
        maclar = [m for m in maclar if m["event_id"] not in onerilen_idler]
    if len(maclar) == 0:
        send_msg(cid, "❌ Tüm yaklaşan maçlar son 30 dk içinde önerildi.")
        return
    secilenler = maclar[:MAX_TARANAN]
    send_msg(cid, f"📊 {toplam} yaklaşan maç var. {len(secilenler)} tanesi analiz ediliyor... (4-6 dk)")

    tum_sakatliklar = {}
    for lig_kod in ESPN_LEAGUES.values():
        tum_sakatliklar.update(takim_sakatliklari_al(lig_kod))

    adaylar = []
    for idx, m in enumerate(secilenler, 1):
        print(f"[{idx}/{len(secilenler)}] {m['ev']} vs {m['dep']}", flush=True)
        lig_kod = ESPN_LEAGUES[m["lig"]]
        h2h_data = espn_get(f"{ESPN_WEB_BASE}/{lig_kod}/summary", {"event": m["event_id"]})
        h2h_verisi = h2h_data.get("headToHeadGames", []) if h2h_data else []
        ev_stats = takim_sezon_istatistik_al(lig_kod, m["ev_id"])
        dep_stats = takim_sezon_istatistik_al(lig_kod, m["dep_id"])
        ev_sch = takim_schedule_al(lig_kod, m["ev_id"])
        dep_sch = takim_schedule_al(lig_kod, m["dep_id"])
        ev_form, ev_b2b = form_ve_b2b(ev_sch, m["tarih"], 5)
        dep_form, dep_b2b = form_ve_b2b(dep_sch, m["tarih"], 5)
        ev_ev_perf = ev_deplasman_performans(lig_kod, m["ev_id"], ev_mi=True)
        dep_dep_perf = ev_deplasman_performans(lig_kod, m["dep_id"], ev_mi=False)

        analiz = maci_analiz_et(m, tum_sakatliklar, h2h_verisi,
                                ev_stats, dep_stats, ev_form, dep_form,
                                ev_b2b, dep_b2b, ev_ev_perf, dep_dep_perf)
        if not analiz: continue
        tercih = analiz.get("tercih", "")
        if "ALT" not in tercih.upper(): continue
        if analiz.get("olasilik", 0) < 55: continue
        adaylar.append({"mac": m, "analiz": analiz})
        print(f"   ✅ ADAY: {tercih} | %{analiz['olasilik']}", flush=True)

    if len(adaylar) < 1:
        send_msg(cid, "❌ ALT şartını geçen maç bulunamadı.")
        return

    adaylar.sort(key=lambda x: x["analiz"]["olasilik"], reverse=True)
    en_iyi = adaylar[0]
    m = en_iyi["mac"]
    a = en_iyi["analiz"]

    orijinal_tercih = a["tercih"]
    emniyetli_tercih = emniyet_marji_uygula(orijinal_tercih, EMNIYET_MARJI)
    a["tercih"] = emniyetli_tercih
    print(f"Tercih: {orijinal_tercih} -> {emniyetli_tercih} (+{EMNIYET_MARJI})", flush=True)

    t = []
    t.append("🏀 BASKETBOL ALT KUPONU")
    t.append(f"⏰ {m['tarih'][:16].replace('T', ' ')}")
    t.append(f"🏆 {m['lig']}")
    t.append("━━━━━━━━━━━━━━━━━━")
    t.append("")
    t.append(f"⚡ {m['ev']} - {m['dep']}")
    t.append(f"📊 Kayıt: {m['ev_kayit']} vs {m['dep_kayit']}")
    t.append("")
    t.append(f"🎯 Tercih: *{emniyetli_tercih}*")
    t.append(f"📊 Olasılık: %{a['olasilik']}")
    t.append(f"✅ Uyan şart: {a.get('uyan_sart', '?')}/7")
    t.append(f"🛡️ Emniyet marjı: +{EMNIYET_MARJI} sayı")
    t.append("")
    t.append(f"📝 {a.get('gerekce', '')}")
    t.append("")
    t.append("━━━━━━━━━━━━━━━━━━")
    t.append(f"🤖 Maç bitince OTOMATIK sonuçlanacak")
    t.append("")
    t.append("⚠️ Yatırım tavsiyesi değildir.")

    metin = "\n".join(t)
    aid = save_analiz(cid, f"{m['ev']} - {m['dep']}", m["event_id"], m["lig"], emniyetli_tercih, a["olasilik"])
    send_msg(cid, metin, "Markdown")

# ==========================================
# DEBUG
# ==========================================
def debug_basket(cid):
    tr_now = datetime.now(timezone.utc) + timedelta(hours=3)
    lines = ["🔧 *DEBUG*", "", f"TR: `{tr_now.strftime('%d.%m.%Y %H:%M')}`", ""]
    sakatliklar = takim_sakatliklari_al("nba")
    lines.append(f"✅ NBA sakatlık: *{len(sakatliklar)}* takım")
    maclar = bugunun_maclari_al()
    lines.append(f"🏀 Yaklaşan maç: *{len(maclar)}*")
    send_msg(cid, "\n".join(lines), "Markdown")
    if maclar:
        m = maclar[0]
        lig_kod = ESPN_LEAGUES[m["lig"]]
        s = takim_sezon_istatistik_al(lig_kod, m["ev_id"])
        lines2 = [f"🔍 *Test: {m['ev']}*", ""]
        if s:
            if s.get("ppg") is not None: lines2.append(f"• PPG: {round(s['ppg'], 1)}")
            if s.get("papg") is not None: lines2.append(f"• PAPG: {round(s['papg'], 1)}")
            if s.get("pace") is not None: lines2.append(f"• Pace: {round(s['pace'], 1)}")
        sch = takim_schedule_al(lig_kod, m["ev_id"])
        form, b2b = form_ve_b2b(sch, m["tarih"], 3)
        lines2.append(f"• Dinlenme: {b2b} gün")
        for f in form:
            lines2.append(f"  - {f['tarih']}: TOP {f['toplam']}")
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
    return ("🏀 *ABD BASKETBOL ALT BOTU v10*\n\n"
            "🎯 Sadece ALT (under)\n"
            "🛡️ +10 sayı emniyet marjı\n"
            "📊 PPG + PAPG + Pace + Form + B2B\n"
            "🤖 Otomatik sonuç (5 dk kontrol)\n\n"
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
    text = ("❓ YARDIM - v10\n\n"
            "🏀 Yaklaşan ABD basketbol maçları\n"
            "🎯 Sadece ALT bahisleri\n"
            "🛡️ +10 emniyet marjı\n"
            "🤖 Maç bitince otomatik sonuç (5 dk)\n\n"
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
            edit_message_text(cid, mid, "⏳ Analiz başlatılıyor...", None)
            threading.Thread(target=analiz_yap, args=(cid,), daemon=True).start()
            return
    except Exception as e:
        print(f"callback: {e}", flush=True)

# ==========================================
# ANA DONGU
# ==========================================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    init_db()
    threading.Thread(target=sonuc_kontrol_worker, daemon=True).start()
    print("=== ABD BASKETBOL ALT BOTU v10 BASLADI ===", flush=True)
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
