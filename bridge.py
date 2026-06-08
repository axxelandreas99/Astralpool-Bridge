#!/usr/bin/env python3
"""
Bridge AstralPool Micro UP → Réseau local
Protocole binaire TCP port 5050 — décodé depuis sources smali MaPiscine

Endpoints HTTP exposés :
  GET  /              → dashboard HTML
  GET  /status        → toutes les données JSON
  GET  /ph            → données pH JSON
  GET  /redox         → données redox JSON
  GET  /temperature   → température eau JSON
  POST /ph/setpoint   → changer consigne pH    {"value": 7.2}
  POST /redox/setpoint→ changer consigne redox {"value": 720}
"""

import glob
import socket
import threading
import time
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ─── CONFIG ───────────────────────────────────────────────
POMPES = {
    "ph": {
        "ssid":        "MP1_A9470",   # pompe pH
        "pompe_ip":    "192.168.4.1",
        "pompe_port":  5050,
        "local_iface": "wlan1",       # dongle USB
    },
    "redox": {
        "ssid":        "MP1_3691FA",  # pompe Redox
        "pompe_ip":    "192.168.4.1",
        "pompe_port":  5050,
        "local_iface": "wlan0",       # WiFi intégré Pi 3
    },
}
BRIDGE_PORT = 8080

# Limites de sécurité pour les consignes
SETPOINT_LIMITS = {
    "ph":    {"min": 6.5,  "max": 7.8},   # pH raisonnable pour piscine
    "redox": {"min": 500,  "max": 900},   # mV raisonnable
}

# ─── SONDE DS18B20 ────────────────────────────────────────────────────────
W1_PATH = "/sys/bus/w1/devices/28-b51f720a6461/temperature"

def lire_temperature():
    """Lit la température depuis la sonde DS18B20."""
    try:
        with open(W1_PATH, "r") as f:
            raw = int(f.read().strip())
            return round(raw / 1000, 1)
    except Exception as e:
        return None

# ─── CONSTANTES PROTOCOLE (depuis CMD.smali + SRC_DEST.smali) ─────────────
class SRC_DEST:
    PHPLUS     = 0x01
    PHPMINUS   = 0x02
    REDOX      = 0x04
    APP        = 0x08
    BROADCAST  = 0xFF
    PHPUNIFIED = 0x99  # -0x67 en signed byte

class CMD:
    DATE_NOW        = 0x01
    INFO            = 0x02
    SETPOINT        = 0x03
    READ            = 0x04
    DATE_LAST_CALIB = 0x05
    DATE_START_END  = 0x06
    CHART           = 0x07
    ACK             = 0xAA  # -0x56
    NAK             = 0xBB  # -0x45

PH_SOURCES    = {SRC_DEST.PHPLUS, SRC_DEST.PHPMINUS, SRC_DEST.PHPUNIFIED}
REDOX_SOURCES = {SRC_DEST.REDOX}

# ─── ÉTAT PARTAGÉ ──────────────────────────────────────────
donnees = {
    "ph": {
        "ssid":             "MP1_A9470",
        "type":             None,
        "current":          None,
        "tPoint":           None,
        "min":              None,
        "max":              None,
        "alarm":            None,
        "pump_status":      None,
        "calibDate":        None,
        "derniere_lecture": None,
        "erreur":           None,
        "unite":            "pH",
        "decimals":         2,
    },
    "redox": {
        "ssid":             "MP1_3691FA",
        "type":             "REDOX",
        "current":          None,
        "tPoint":           None,
        "min":              None,
        "max":              None,
        "alarm":            None,
        "pump_status":      None,
        "calibDate":        None,
        "derniere_lecture": None,
        "erreur":           None,
        "unite":            "mV",
        "decimals":         0,
    },
    "temperature": {
        "valeur":           None,
        "unite":            "°C",
        "derniere_lecture": None,
        "erreur":           None,
    },
}
lock = threading.Lock()

# Sockets actifs par canal (pour l'envoi de commandes)
sockets_actifs = {"ph": None, "redox": None}
sockets_lock   = threading.Lock()


# ─── PROTOCOLE ────────────────────────────────────────────

def src_to_pompe(src_byte):
    if src_byte in PH_SOURCES:    return "ph"
    if src_byte in REDOX_SOURCES: return "redox"
    return None

def src_name(src_byte):
    return {
        SRC_DEST.PHPLUS:     "PHPLUS",
        SRC_DEST.PHPMINUS:   "PHPMINUS",
        SRC_DEST.REDOX:      "REDOX",
        SRC_DEST.APP:        "APP",
        SRC_DEST.BROADCAST:  "BROADCAST",
        SRC_DEST.PHPUNIFIED: "PHPUNIFIED",
    }.get(src_byte, f"0x{src_byte:02X}")

def lire_le16(data, offset):
    if offset + 1 >= len(data): return None
    return data[offset] | (data[offset + 1] << 8)

def lire_le32(data, offset):
    if offset + 3 >= len(data): return None
    return (data[offset] | (data[offset+1]<<8) |
            (data[offset+2]<<16) | (data[offset+3]<<24))

def construire_msg(src, dest, cmd, mots=None):
    """Construit un message binaire AstralPool."""
    mots = mots or []
    nb   = len(mots)
    data = bytes([src & 0xFF, dest & 0xFF, cmd & 0xFF, nb])
    for m in mots:
        data += bytes([m & 0xFF, (m >> 8) & 0xFF])
    checksum = sum(data) & 0xFF
    return data + bytes([checksum])

def dest_pour(canal):
    return SRC_DEST.REDOX if canal == "redox" else SRC_DEST.PHPUNIFIED

def traiter_message(data, canal):
    """
    Décode un message AstralPool selon la structure réelle (Device$Companion.fromByteArray).
    Offsets FIXES — data[3] n'est PAS un nb_mots :
      [0]=SRC  [1]=DEST  [2]=CMD  [3]=ignoré
      INFO (0x02)           : [4:5]=min  [6:7]=max  [8]=flags(bit0=alarm, bit7=pump_status)
      SETPOINT (0x03)       : [4:5]=tPoint
      READ (0x04)           : [4:5]=current
      DATE_LAST_CALIB (0x05): [4:7]=timestamp Unix LE32 (secondes)
    """
    if len(data) < 5:
        return

    src = data[0] & 0xFF
    cmd = data[2] & 0xFF

    pompe = src_to_pompe(src)
    if pompe is None:
        return

    now = datetime.now().isoformat()

    def to_val(raw):
        return round(raw / 100, 2) if pompe == "ph" else raw

    with lock:
        donnees[pompe]["derniere_lecture"] = now
        donnees[pompe]["erreur"]           = None
        donnees[pompe]["type"]             = src_name(src)

        nb = data[3]  # nb_bytes de données après le header

        if cmd == CMD.INFO and nb >= 5:
            # max=[4:5], min=[6:7], flags=[8]
            donnees[pompe]["max"]         = to_val(lire_le16(data, 4))
            donnees[pompe]["min"]         = to_val(lire_le16(data, 6))
            flags = data[8]
            donnees[pompe]["alarm"]       = bool(flags & 0x01)   # bit 0
            donnees[pompe]["pump_status"] = bool(flags & 0x80)   # bit 7

        elif cmd == CMD.SETPOINT and nb >= 2:
            donnees[pompe]["tPoint"] = to_val(lire_le16(data, 4))

        elif cmd == CMD.READ and nb >= 2:
            donnees[pompe]["current"] = to_val(lire_le16(data, 4))

        elif cmd == CMD.DATE_LAST_CALIB and nb >= 4:
            ts = lire_le32(data, 4)
            if ts:
                try:
                    donnees[pompe]["calibDate"] = datetime.fromtimestamp(ts).isoformat()
                except Exception:
                    pass

        elif cmd == CMD.ACK:
            pass  # acquittement normal

        elif cmd == CMD.NAK:
            donnees[pompe]["erreur"] = "NAK reçu de la pompe"


# ─── TEMPÉRATURE DS18B20 ─────────────────────────────────────

def boucle_temperature():
    """Thread de lecture température toutes les 30s."""
    while True:
        val = lire_temperature()
        now = datetime.now().isoformat()
        with lock:
            if val is not None:
                donnees["temperature"]["valeur"]           = val
                donnees["temperature"]["derniere_lecture"] = now
                donnees["temperature"]["erreur"]           = None
            else:
                donnees["temperature"]["erreur"]           = "Lecture impossible"
                donnees["temperature"]["derniere_lecture"] = now
        time.sleep(30)

# ─── CONNEXION ET RÉCEPTION ────────────────────────────────

def recevoir_pompe(cfg, label):
    """Thread dédié à une pompe, bindé sur une interface réseau précise."""
    pompe_ip   = cfg["pompe_ip"]
    pompe_port = cfg["pompe_port"]
    iface      = cfg["local_iface"]
    dest       = dest_pour(label)

    while True:
        s = None
        try:
            print(f"[{label}] {cfg['ssid']} — connexion via {iface} → {pompe_ip}:{pompe_port}")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # SO_BINDTODEVICE force le trafic sur l'interface sans dépendre de l'IP
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                         iface.encode() + b'\0')
            s.settimeout(15)
            s.connect((pompe_ip, pompe_port))

            with sockets_lock:
                sockets_actifs[label] = s

            print(f"[{label}] Connecté ! Demande INFO + READ initiales...")
            s.sendall(construire_msg(SRC_DEST.APP, dest, CMD.INFO))
            s.sendall(construire_msg(SRC_DEST.APP, dest, CMD.READ))

            buf = b""
            while True:
                chunk = s.recv(256)
                if not chunk:
                    raise ConnectionResetError("Connexion fermée par la pompe")
                buf += chunk

                while len(buf) >= 4:
                    # data[3] = nb_bytes_données, msg_len = header(4) + data(n) + checksum(1)
                    nb_data  = buf[3]
                    msg_len  = 4 + nb_data + 1
                    if len(buf) >= msg_len:
                        traiter_message(buf[:msg_len], label)
                        buf = buf[msg_len:]
                    else:
                        break

        except Exception as e:
            with lock:
                donnees[label]["erreur"] = str(e)
            print(f"[{label}] Erreur: {e} — reconnexion dans 5s")
            time.sleep(5)
        finally:
            with sockets_lock:
                sockets_actifs[label] = None
            if s:
                try:
                    s.close()
                except Exception:
                    pass


def envoyer_setpoint(canal, valeur_float):
    """
    Envoie une commande SETPOINT à la pompe.
    Retourne (True, "") ou (False, "message d'erreur").
    """
    limites = SETPOINT_LIMITS[canal]
    if not (limites["min"] <= valeur_float <= limites["max"]):
        return False, (f"Valeur hors limites : {valeur_float} "
                       f"(autorisé : {limites['min']} – {limites['max']})")

    # Encodage : pH ×100 → entier, Redox → entier brut
    raw = int(round(valeur_float * 100)) if canal == "ph" else int(round(valeur_float))

    dest = dest_pour(canal)
    msg  = construire_msg(SRC_DEST.APP, dest, CMD.SETPOINT, [raw])

    with sockets_lock:
        s = sockets_actifs.get(canal)

    if s is None:
        return False, "Pompe non connectée"

    try:
        s.sendall(msg)
        print(f"[{canal}] SETPOINT envoyé : {valeur_float} (raw={raw})")
        return True, ""
    except Exception as e:
        return False, str(e)


# ─── DASHBOARD HTML ────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstralPool Bridge</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');

  :root {
    --bg:      #0a0e14;
    --surface: #111720;
    --border:  #1e2d3d;
    --text:    #c9d1d9;
    --muted:   #4a5568;
    --ph:      #00e5a0;
    --redox:   #ff6b35;
    --alarm:   #ff3e3e;
    --ok:      #00e5a0;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    min-height: 100vh;
    padding: 2rem;
  }

  header {
    display: flex;
    align-items: baseline;
    gap: 1rem;
    margin-bottom: 2.5rem;
    border-bottom: 1px solid var(--border);
    padding-bottom: 1.2rem;
  }

  header h1 {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 1.6rem;
    letter-spacing: -0.02em;
    color: #fff;
  }

  .badge {
    font-size: 0.7rem;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    background: var(--border);
    color: var(--muted);
    letter-spacing: 0.08em;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 1.5rem;
    margin-bottom: 2rem;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    position: relative;
    overflow: hidden;
  }

  .card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    border-radius: 12px 12px 0 0;
  }

  .card.ph::before    { background: var(--ph); }
  .card.redox::before { background: var(--redox); }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.5rem;
  }

  .card-title {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 0.9rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .card.ph    .card-title { color: var(--ph); }
  .card.redox .card-title { color: var(--redox); }

  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--muted);
    transition: background 0.3s;
  }
  .status-dot.online  { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .status-dot.alarm   { background: var(--alarm); box-shadow: 0 0 8px var(--alarm); }

  .value-main {
    font-family: 'Syne', sans-serif;
    font-size: 3.5rem;
    font-weight: 800;
    line-height: 1;
    letter-spacing: -0.03em;
    margin-bottom: 0.3rem;
  }

  .card.ph    .value-main { color: var(--ph); }
  .card.redox .value-main { color: var(--redox); }

  .value-unit {
    font-size: 1rem;
    color: var(--muted);
    margin-left: 0.3rem;
  }

  .meta-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.8rem;
    margin: 1.2rem 0;
    padding: 1rem;
    background: rgba(0,0,0,0.3);
    border-radius: 8px;
  }

  .meta-item label {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--muted);
    display: block;
    margin-bottom: 0.2rem;
  }

  .meta-item span {
    font-size: 0.85rem;
    color: var(--text);
  }

  .setpoint-form {
    margin-top: 1.2rem;
    display: flex;
    gap: 0.6rem;
    align-items: center;
  }

  .setpoint-form label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    white-space: nowrap;
  }

  .setpoint-form input {
    flex: 1;
    background: rgba(0,0,0,0.4);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 0.9rem;
    padding: 0.45rem 0.7rem;
    outline: none;
    transition: border-color 0.2s;
  }

  .setpoint-form input:focus { border-color: var(--muted); }

  .btn {
    padding: 0.45rem 1rem;
    border: none;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 0.8rem;
    cursor: pointer;
    transition: opacity 0.2s, transform 0.1s;
    white-space: nowrap;
    font-weight: 500;
  }

  .btn:active { transform: scale(0.97); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .card.ph    .btn { background: var(--ph);    color: #000; }
  .card.redox .btn { background: var(--redox); color: #000; }

  .feedback {
    font-size: 0.72rem;
    margin-top: 0.5rem;
    min-height: 1em;
    padding: 0.3rem 0;
  }

  .feedback.ok    { color: var(--ok); }
  .feedback.error { color: var(--alarm); }

  .last-update {
    text-align: center;
    font-size: 0.65rem;
    color: var(--muted);
    margin-top: 2rem;
    letter-spacing: 0.08em;
  }

  .error-bar {
    background: rgba(255,62,62,0.1);
    border: 1px solid rgba(255,62,62,0.3);
    border-radius: 6px;
    padding: 0.5rem 0.8rem;
    font-size: 0.75rem;
    color: var(--alarm);
    margin-top: 0.8rem;
    display: none;
  }
  .error-bar.visible { display: block; }
</style>
</head>
<body>

<header>
  <h1>AstralPool Bridge</h1>
  <span class="badge">port 5050 · TCP</span>
</header>

<div class="grid">

  <!-- CARTE pH -->
  <div class="card ph">
    <div class="card-header">
      <span class="card-title">pH</span>
      <div class="status-dot" id="dot-ph"></div>
    </div>

    <div class="value-main" id="val-ph">
      —<span class="value-unit">pH</span>
    </div>

    <div class="meta-grid">
      <div class="meta-item">
        <label>Consigne</label>
        <span id="tp-ph">—</span>
      </div>
      <div class="meta-item">
        <label>Alarme</label>
        <span id="alarm-ph">—</span>
      </div>
      <div class="meta-item">
        <label>Min config</label>
        <span id="min-ph">—</span>
      </div>
      <div class="meta-item">
        <label>Max config</label>
        <span id="max-ph">—</span>
      </div>
      <div class="meta-item">
        <label>Pompe</label>
        <span id="pump-ph">—</span>
      </div>
      <div class="meta-item">
        <label>Calibration</label>
        <span id="calib-ph">—</span>
      </div>
    </div>

    <div class="setpoint-form">
      <label>Consigne :</label>
      <input type="number" id="sp-ph" step="0.1" min="6.5" max="7.8"
             placeholder="ex: 7.20">
      <button class="btn" onclick="sendSetpoint('ph')">Envoyer</button>
    </div>
    <div class="feedback" id="fb-ph"></div>
    <div class="error-bar" id="err-ph"></div>
  </div>

  <!-- CARTE Redox -->
  <div class="card redox">
    <div class="card-header">
      <span class="card-title">Redox</span>
      <div class="status-dot" id="dot-redox"></div>
    </div>

    <div class="value-main" id="val-redox">
      —<span class="value-unit">mV</span>
    </div>

    <div class="meta-grid">
      <div class="meta-item">
        <label>Consigne</label>
        <span id="tp-redox">—</span>
      </div>
      <div class="meta-item">
        <label>Alarme</label>
        <span id="alarm-redox">—</span>
      </div>
      <div class="meta-item">
        <label>Min config</label>
        <span id="min-redox">—</span>
      </div>
      <div class="meta-item">
        <label>Max config</label>
        <span id="max-redox">—</span>
      </div>
      <div class="meta-item">
        <label>Pompe</label>
        <span id="pump-redox">—</span>
      </div>
      <div class="meta-item">
        <label>Calibration</label>
        <span id="calib-redox">—</span>
      </div>
    </div>

    <div class="setpoint-form">
      <label>Consigne :</label>
      <input type="number" id="sp-redox" step="10" min="500" max="900"
             placeholder="ex: 700">
      <button class="btn" onclick="sendSetpoint('redox')">Envoyer</button>
    </div>
    <div class="feedback" id="fb-redox"></div>
    <div class="error-bar" id="err-redox"></div>
  </div>

</div>

<div class="last-update" id="last-update">Chargement…</div>

<script>
function fmt(v, unit, dec) {
  if (v === null || v === undefined) return '—';
  return (dec > 0 ? Number(v).toFixed(dec) : v) + ' ' + unit;
}

function fmtDate(s) {
  if (!s) return '—';
  try { return new Date(s).toLocaleString('fr-FR'); } catch { return s; }
}

async function refresh() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    updateCard('ph',    d.ph,    'pH', 2);
    updateCard('redox', d.redox, 'mV', 0);
    document.getElementById('last-update').textContent =
      'Mis à jour : ' + new Date().toLocaleTimeString('fr-FR');
  } catch(e) {
    document.getElementById('last-update').textContent = 'Erreur de connexion';
  }
}

function updateCard(id, data, unit, dec) {
  const dot   = document.getElementById('dot-' + id);
  const val   = document.getElementById('val-' + id);
  const errEl = document.getElementById('err-' + id);

  // Valeur principale
  if (data.current !== null) {
    val.innerHTML = (dec > 0 ? Number(data.current).toFixed(dec) : data.current)
                  + '<span class="value-unit">' + unit + '</span>';
  }

  // Statut dot
  dot.className = 'status-dot';
  if (data.erreur)          dot.classList.add('alarm');
  else if (data.alarm)      dot.classList.add('alarm');
  else if (data.current !== null) dot.classList.add('online');

  // Metas
  document.getElementById('tp-'    + id).textContent = fmt(data.tPoint,     unit, dec);
  document.getElementById('alarm-' + id).textContent = data.alarm ? '⚠ OUI' : (data.alarm === false ? 'Non' : '—');
  document.getElementById('min-'   + id).textContent = fmt(data.min,        unit, dec);
  document.getElementById('max-'   + id).textContent = fmt(data.max,        unit, dec);
  document.getElementById('pump-'  + id).textContent = data.pump_status ? 'En marche' : (data.pump_status === false ? 'Arrêtée' : '—');
  document.getElementById('calib-' + id).textContent = fmtDate(data.calibDate);

  // Barre d'erreur
  if (data.erreur) {
    errEl.textContent = '⚠ ' + data.erreur;
    errEl.classList.add('visible');
  } else {
    errEl.classList.remove('visible');
  }
}

async function sendSetpoint(canal) {
  const input = document.getElementById('sp-' + canal);
  const fb    = document.getElementById('fb-' + canal);
  const val   = parseFloat(input.value);

  if (isNaN(val)) {
    fb.textContent = 'Valeur invalide';
    fb.className = 'feedback error';
    return;
  }

  fb.textContent = 'Envoi…';
  fb.className = 'feedback';

  try {
    const r = await fetch('/' + canal + '/setpoint', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({value: val})
    });
    const d = await r.json();
    if (d.ok) {
      fb.textContent = '✓ Consigne envoyée : ' + val;
      fb.className = 'feedback ok';
      input.value = '';
      setTimeout(refresh, 1500);
    } else {
      fb.textContent = '✗ ' + d.erreur;
      fb.className = 'feedback error';
    }
  } catch(e) {
    fb.textContent = '✗ Erreur réseau';
    fb.className = 'feedback error';
  }
}

// Rafraîchissement automatique toutes les 5s
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


# ─── SERVEUR HTTP ──────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            self._send(200, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")

        elif self.path == "/status":
            with lock:
                snap = json.loads(json.dumps(donnees))
            self._json(200, snap)

        elif self.path == "/ph":
            with lock:
                snap = json.loads(json.dumps(donnees["ph"]))
            self._json(200, snap)

        elif self.path == "/redox":
            with lock:
                snap = json.loads(json.dumps(donnees["redox"]))
            self._json(200, snap)
        elif self.path == "/temperature":
            with lock:
                snap = json.loads(json.dumps(donnees["temperature"]))
            self._json(200, snap)

        else:
            self._json(404, {"erreur": "Route inconnue"})

    def do_POST(self):
        canal = None
        if self.path == "/ph/setpoint":
            canal = "ph"
        elif self.path == "/redox/setpoint":
            canal = "redox"

        if canal is None:
            self._json(404, {"erreur": "Route inconnue"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        try:
            payload = json.loads(body)
            valeur  = float(payload["value"])
        except Exception:
            self._json(400, {"ok": False, "erreur": "JSON invalide ou champ 'value' manquant"})
            return

        ok, msg = envoyer_setpoint(canal, valeur)
        if ok:
            self._json(200, {"ok": True, "value": valeur})
        else:
            self._json(400, {"ok": False, "erreur": msg})

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self._send(code, body, "application/json; charset=utf-8")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type",   content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[HTTP] {args[0]}")


# ─── POINT D'ENTRÉE ────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("  AstralPool Micro UP Bridge")
    print("=" * 50)
    for label, cfg in POMPES.items():
        print(f"  {label.upper():6} → {cfg['ssid']} via {cfg['local_iface']}")
    print(f"  Dashboard → http://0.0.0.0:{BRIDGE_PORT}/")
    print(f"  API JSON  → http://0.0.0.0:{BRIDGE_PORT}/status")
    print("=" * 50)

    threading.Thread(target=boucle_temperature, daemon=True).start()

    for label, cfg in POMPES.items():
        threading.Thread(
            target=recevoir_pompe,
            args=(cfg, label),
            daemon=True
        ).start()
        time.sleep(1)

    HTTPServer(("0.0.0.0", BRIDGE_PORT), BridgeHandler).serve_forever()
