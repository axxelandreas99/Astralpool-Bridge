# AstralPool Micro UP Bridge

Bridge non officiel pour récupérer les données pH et Redox des pompes doseuses **AstralPool Micro UP WiFi Direct** sur un réseau local standard, avec intégration **Home Assistant**.

> ⚠️ Projet personnel, sans lien avec AstralPool. Le protocole a été découvert par reverse engineering de l'APK Android MaPiscine sur du matériel personnel.

---

## Fonctionnalités

- Connexion simultanée à 2 pompes AstralPool Micro UP (WiFi Direct)
- Décodage du protocole binaire TCP:5050 propriétaire
- Exposition des données via API HTTP JSON
- Dashboard web intégré (accessible depuis n'importe quel navigateur)
- Modification des consignes pH et Redox
- Intégration Home Assistant avec alertes automatiques

---

## Matériel nécessaire

| Élément | Détail |
|---|---|
| Raspberry Pi 3B/3B+/4 | Avec alimentation et carte SD |
| Dongle USB WiFi | Compatible Linux (ex: TP-Link TL-WN725N) |
| Câble Ethernet | Pour la connexion au réseau local |
| 2× AstralPool Micro UP | 1 pH + 1 Redox avec WiFi Direct |

---

## Architecture

```
[Pompe pH    SSID: MP1_xxxxx ] ←WiFi→ wlan1 ─┐
                                               ├─ [Raspberry Pi] ──eth0──▶ [Box / Home Assistant]
[Pompe Redox SSID: MP1_yyyyy ] ←WiFi→ wlan0 ─┘
```

- `wlan0` (WiFi intégré) → pompe Redox
- `wlan1` (dongle USB)   → pompe pH
- `eth0`                 → réseau local / Home Assistant

---

## Installation

### 1. Prérequis

```bash
sudo apt update && sudo apt upgrade -y
python3 --version  # Python 3.7+ requis
```

### 2. Connexion WiFi aux pompes

Identifiez les SSIDs de vos pompes (format `MP1_xxxxxx`) dans les paramètres WiFi de votre téléphone, puis :

```bash
# Connecter wlan0 à la pompe Redox
sudo nmcli device wifi connect <SSID_REDOX> ifname wlan0

# Connecter wlan1 à la pompe pH
sudo nmcli device wifi connect <SSID_PH> ifname wlan1

# Vérifier
nmcli device status
```

### 3. Configurer le bridge

Éditez `bridge.py` et adaptez la section `CONFIG` :

```python
POMPES = {
    "ph": {
        "ssid":        "MP1_A9470",   # ← votre SSID pompe pH
        "pompe_ip":    "192.168.4.1",
        "pompe_port":  5050,
        "local_iface": "wlan1",
    },
    "redox": {
        "ssid":        "MP1_3691FA",  # ← votre SSID pompe Redox
        "pompe_ip":    "192.168.4.1",
        "pompe_port":  5050,
        "local_iface": "wlan0",
    },
}
```

### 4. Installer le service systemd

```bash
sudo mkdir -p /opt/astralpool
sudo cp bridge.py /opt/astralpool/
sudo cp astralpool-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable astralpool-bridge
sudo systemctl start astralpool-bridge
```

### 5. Vérifier

```bash
sudo systemctl status astralpool-bridge
curl http://127.0.0.1:8080/status
```

---

## API

| Méthode | Endpoint | Description |
|---|---|---|
| GET | `/` | Dashboard HTML |
| GET | `/status` | Toutes les données JSON |
| GET | `/ph` | Données pH |
| GET | `/redox` | Données Redox |
| POST | `/ph/setpoint` | Changer consigne pH `{"value": 7.2}` |
| POST | `/redox/setpoint` | Changer consigne Redox `{"value": 700}` |

### Exemple de réponse `/status`

```json
{
  "ph": {
    "type": "PHPMINUS",
    "current": 7.33,
    "tPoint": 7.3,
    "min": 6.9,
    "max": 7.6,
    "alarm": false,
    "pump_status": false,
    "calibDate": "2025-06-28T16:50:02",
    "unite": "pH"
  },
  "redox": {
    "type": "REDOX",
    "current": 570,
    "tPoint": 650,
    "min": 300,
    "max": 800,
    "alarm": false,
    "pump_status": true,
    "calibDate": "2025-06-28T16:52:54",
    "unite": "mV"
  }
}
```

---

## Home Assistant

Copiez les fichiers du dossier `homeassistant/` :

- `configuration.yaml` → à ajouter dans votre `configuration.yaml` HA
- `automations.yaml` → à ajouter dans votre `automations.yaml` HA
- `lovelace.yaml` → dashboard à créer via l'éditeur YAML Lovelace

Remplacez `192.168.1.180` par l'IP de votre Raspberry Pi.

---

## Protocole binaire AstralPool

```
Format : [SRC][DEST][CMD][NB_BYTES][data...][checksum]

SRC / DEST :
  0x01 = PHPLUS    0x02 = PHPMINUS   0x04 = REDOX
  0x08 = APP       0xFF = BROADCAST  0x99 = PHPUNIFIED

CMD :
  0x01 = DATE_NOW        0x02 = INFO       0x03 = SETPOINT
  0x04 = READ            0x05 = DATE_LAST_CALIB
  0x06 = DATE_START_END  0x07 = CHART
  0xAA = ACK             0xBB = NAK

INFO (cmd=0x02, nb=5) :
  [4:5] max   LE16  (/100 pour pH)
  [6:7] min   LE16  (/100 pour pH)
  [8]   flags bit0=alarm, bit7=pump_status

READ (cmd=0x04, nb=2) :
  [4:5] valeur mesurée LE16 (/100 pour pH, brut mV pour Redox)

SETPOINT (cmd=0x03, nb=2) :
  [4:5] consigne LE16 (/100 pour pH, brut mV pour Redox)
```

---

## Dépannage

| Symptôme | Solution |
|---|---|
| `Cannot assign requested address` | `nmcli device status` — vérifier que wlan0/wlan1 sont connectés |
| Valeurs `null` | Pompes hors portée WiFi |
| HA ne trouve pas les sensors | Utiliser la syntaxe `platform: rest` (pas le bloc `rest:`) |
| `wlan1` absent | Vérifier le dongle avec `ip link show` après branchement |

---

## Licence

MIT — libre d'utilisation, modification et redistribution.
