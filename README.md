# AstralPool Micro UP Bridge

Bridge non officiel pour récupérer les données pH, Redox et température de l'eau depuis des pompes doseuses **AstralPool Micro UP WiFi Direct**, avec intégration **Home Assistant**.

> ⚠️ Projet personnel, sans lien avec AstralPool. Le protocole a été découvert par reverse engineering de l'APK Android MaPiscine sur du matériel personnel.

---

## Fonctionnalités

- Connexion simultanée à 2 pompes AstralPool Micro UP (WiFi Direct)
- Décodage du protocole binaire TCP:5050 propriétaire
- Lecture de température via sonde DS18B20 (1-Wire)
- Exposition des données via API HTTP JSON
- Dashboard web intégré
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
| Sonde DS18B20 étanche | Câble 3m recommandé + doigt de gant inox |
| Résistance 4.7 kΩ | Pull-up 1-Wire obligatoire |
| Fils Dupont femelle-femelle | Pour connexion GPIO |

---

## Architecture

```
[Pompe pH    SSID: MP1_xxxxx ] ←WiFi→ wlan1 ─┐
                                               ├─ [Raspberry Pi] ──eth0──▶ [Box / Home Assistant]
[Pompe Redox SSID: MP1_yyyyy ] ←WiFi→ wlan0 ─┘
[Sonde DS18B20] ──────────────────── GPIO4 ───┘
```

- `wlan0` (WiFi intégré) → pompe Redox
- `wlan1` (dongle USB)   → pompe pH
- `GPIO4` (pin 7)        → sonde DS18B20
- `eth0`                 → réseau local / Home Assistant

---

## Câblage DS18B20

```
Pin 1  (3.3V) ────┬──────────── Fil rouge  (VCC)
                  │
                [4.7kΩ]  ← résistance pull-up entre 3.3V et Data
                  │
Pin 7  (GPIO4) ───┴──────────── Fil jaune  (Data)

Pin 6  (GND)  ───────────────── Fil noir   (GND)
```

---

## Installation

### 1. Prérequis

```bash
sudo apt update && sudo apt upgrade -y
python3 --version  # Python 3.7+ requis
```

### 2. Activer le bus 1-Wire pour DS18B20

```bash
# Ajouter à /boot/firmware/config.txt
echo "dtoverlay=w1-gpio,gpiopin=4" | sudo tee -a /boot/firmware/config.txt
sudo reboot

# Vérifier que la sonde est détectée
ls /sys/bus/w1/devices/
# Doit afficher : 28-xxxxxxxxxxxx  w1_bus_master1

# Tester la lecture
cat /sys/bus/w1/devices/28-xxxxxxxxxxxx/temperature
# Retourne la température en millièmes de °C (ex: 25312 = 25.3°C)
```

### 3. Désactiver wpa_supplicant (évite les conflits au redémarrage)

```bash
sudo systemctl stop wpa_supplicant
sudo systemctl disable wpa_supplicant
sudo rm -f /var/run/wpa_supplicant/wlan0
sudo rm -f /var/run/wpa_supplicant/wlan1
```

### 4. Connecter les interfaces WiFi aux pompes

```bash
sudo nmcli device wifi connect <SSID_REDOX> ifname wlan0
sudo nmcli device wifi connect <SSID_PH> ifname wlan1

# Forcer l'interface et la reconnexion automatique
sudo nmcli connection modify <SSID_REDOX> connection.interface-name wlan0
sudo nmcli connection modify <SSID_PH> connection.interface-name wlan1
sudo nmcli connection modify <SSID_REDOX> connection.autoconnect-retries 0
sudo nmcli connection modify <SSID_PH> connection.autoconnect-retries 0

sudo systemctl restart NetworkManager
nmcli device status

# Désactiver wpa_supplicant pour éviter les conflits au redémarrage
sudo systemctl stop wpa_supplicant
sudo systemctl disable wpa_supplicant

# Nettoyer les fichiers socket
sudo rm -f /var/run/wpa_supplicant/wlan0
sudo rm -f /var/run/wpa_supplicant/wlan1

# Associer explicitement chaque connexion à son interface
sudo nmcli connection modify MP1_3691FA connection.interface-name wlan0
sudo nmcli connection modify MP1_A9470 connection.interface-name wlan1

# Réessayer indéfiniment en cas d'échec de connexion
sudo nmcli connection modify MP1_3691FA connection.autoconnect-retries 0
sudo nmcli connection modify MP1_A9470 connection.autoconnect-retries 0

# Redémarrer NetworkManager
sudo systemctl restart NetworkManager
```

### 5. Configurer le bridge

Éditez `bridge.py` et adaptez :

```python
# SSIDs de vos pompes
POMPES = {
    "ph": {
        "ssid":        "MP1_A9470",   # ← votre SSID pompe pH
        "local_iface": "wlan1",
    },
    "redox": {
        "ssid":        "MP1_3691FA",  # ← votre SSID pompe Redox
        "local_iface": "wlan0",
    },
}

# Identifiant de votre sonde DS18B20
W1_PATH = "/sys/bus/w1/devices/28-xxxxxxxxxxxx/temperature"  # ← adapter
```

### 6. Installer le service systemd

```bash
sudo mkdir -p /opt/astralpool
sudo cp bridge.py /opt/astralpool/
sudo cp astralpool-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable astralpool-bridge
sudo systemctl start astralpool-bridge
```

### 7. Vérifier

```bash
sudo systemctl status astralpool-bridge
curl http://127.0.0.1:8080/status
curl http://127.0.0.1:8080/temperature
```

---

## API

| Méthode | Endpoint | Description |
|---|---|---|
| GET | `/` | Dashboard HTML |
| GET | `/status` | Toutes les données JSON |
| GET | `/ph` | Données pH |
| GET | `/redox` | Données Redox |
| GET | `/temperature` | Température eau |
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
  },
  "temperature": {
    "valeur": 25.4,
    "unite": "°C"
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

### Alertes configurées

| Alerte | Condition |
|---|---|
| pH bas | pH < 6.9 pendant 5 min |
| pH élevé | pH > 7.6 pendant 5 min |
| Redox bas | Redox < 300 mV pendant 5 min |
| Redox élevé | Redox > 800 mV pendant 5 min |
| Alarme pompe pH | Bit alarme actif |
| Alarme pompe Redox | Bit alarme actif |
| Température élevée | Température > 30°C pendant 5 min |

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
| `Cannot assign requested address` | `nmcli device status` — vérifier wlan0/wlan1 connectés |
| Valeurs `null` | Pompes hors portée WiFi |
| wlan en `unavailable` au reboot | `sudo systemctl disable wpa_supplicant` + `nmcli connection modify ... connection.interface-name wlanX` |
| Sonde non détectée | Vérifier `dtoverlay=w1-gpio` dans `/boot/firmware/config.txt` et la résistance 4.7kΩ |
| Température à `null` | Vérifier le chemin `W1_PATH` dans `bridge.py` avec `ls /sys/bus/w1/devices/` |
| HA ne trouve pas les sensors | Utiliser la syntaxe `platform: rest` (pas le bloc `rest:`) |

---

## Licence

MIT — libre d'utilisation, modification et redistribution.
