# KadoHabbit · Proxmox LXC Installer

Lokaler Habit-Tracker (**Web-Nachbau** zu [scastiel/kado](https://github.com/scastiel/kado) — Kadō,
privacy-first habit tracker für iPhone/iPad) als **Proxmox-LXC** im Stil der
[Proxmox VE Community Scripts](https://community-scripts.github.io/ProxmoxVE).
Python/FastAPI + SQLite, **kein Cloud-Zwang**, Web-UI auf Port 8080.

> **Warum ein Nachbau?** Das Upstream-Repo `scastiel/kado` ist eine **native
> Swift/SwiftUI-iOS-App** und kann nicht in einem Linux-Container laufen.
> Dieser Installer erstellt den LXC **`kadoHabbit`** und installiert darin einen
> kompatiblen Web-Nachbau mit demselben Kern: **Habit-Score als EMA (α=0.05)**
> statt fragiler Streak (Specs: Upstream `docs/habit-score.md` + `docs/streak.md`).

## Schnellstart (Proxmox-Host als root)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/KadoHabbitProxmox/main/install/kado.sh)"
```

Das war's. Am Ende steht die URL im Log, z. B. `http://192.168.1.103:8080`.

Mit Debug-Log (`bash -x`, volle Ablaufverfolgung):

```bash
DEBUG=1 bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/KadoHabbitProxmox/main/install/kado.sh)"
```

## Was passiert

| Schritt | Details |
|---|---|
| Container | Name **`kadoHabbit`**, Start-ID **103** — falls vergeben, wird **automatisch die nächste freie ID** genommen (`CTID_AUTO=1`) |
| Ressourcen | **1 vCPU, 1024 MB RAM, 4 GB Disk**, Debian 12, DHCP auf `vmbr0`, `onboot=1` (reboot-sicher), unprivilegiert |
| App | Python 3 + venv + FastAPI/uvicorn, Code nach `/opt/kado`, Daten nach `/var/lib/kado/kado.db` |
| Dienst | `kado.service` (systemd, `enable`, `Restart=always`, `After=network-online.target`), bind `0.0.0.0:8080` |
| Check | Script prüft selbst: `systemctl is-active kado` + `curl localhost:8080/healthz`, gibt finale URL + Container-IP aus |
| Fehler | **Komplette Kette**: Exit-Code, Befehl, Zeile, Stacktrace, letzte 30 Logzeilen, `journalctl`-Auszug — nie nur die letzte Zeile |

## Anpassen (alle optional, Variablen oben im Script)

```bash
CTID=110 APP_PORT=8080 bash install/kado.sh        # andere ID / anderer Port
CT_HOSTNAME=kadoHabbit CPU=2 RAM=2048 DISK=8 bash install/kado.sh
CTID_AUTO=0 CTID=110 bash install/kado.sh          # strikt diese ID, kein Ausweichen
CTID_FORCE_UPDATE=1 CTID=103 bash install/kado.sh  # bestehenden CT 103 updaten statt auszuweichen
STORAGE=local-lvm BRIDGE=vmbr0 bash install/kado.sh
```

| Variable | Default | Bedeutung |
|---|---|---|
| `CT_HOSTNAME` | `kadoHabbit` | LXC-Name |
| `CTID` | `103` | Wunsch-ID, bei Belegung nächste freie |
| `CTID_AUTO` | `1` | `1` = nächste freie ID wählen, `0` = abbrechen wenn belegt |
| `CTID_FORCE_UPDATE` | `0` | `1` = existierenden CT updaten |
| `CPU` / `RAM` / `DISK` | `1` / `1024` / `4` | vCPU / MiB / GiB |
| `APP_PORT` | `8080` | Web-UI-Port |
| `STORAGE` / `BRIDGE` | `local-lvm` / `vmbr0` | Proxmox-Storage / Bridge |

## Update / Deinstall

```bash
# Update (idempotent):
CTID_FORCE_UPDATE=1 CTID=103 bash install/kado.sh
# oder einfach Einzeiler erneut laufen lassen (gleicher Hostname → Update-Pfad)

# Deinstall:
pct stop 103 && pct destroy 103
```

## Struktur

```
install/kado.sh        Host-Installer (Community-Scripts-Stil, Variablen oben)
src/app.py             FastAPI-App + Web-UI (bind 0.0.0.0:$PORT)
src/requirements.txt
systemd/kado.service   systemd-Unit (reboot-sicher)
tests/test_score.py    EMA-/Streak-Tests (7 Tests)
```

## Lokal testen (ohne Proxmox)

```bash
python3 -m venv .venv && .venv/bin/pip install -r src/requirements.txt
PORT=8080 KADO_DATA_DIR=/tmp/kado-data .venv/bin/uvicorn app:app --app-dir src --host 0.0.0.0 --port 8080
curl -fsS localhost:8080/healthz
python3 -m pytest tests/ -q
bash -n install/kado.sh
```

## Troubleshooting

- **CT-ID belegt?** Kein Problem — das Script nimmt automatisch die nächste freie
  und meldet z. B. `Freie CT-ID gefunden: 104`. Mit `CTID_AUTO=0` wird stattdessen abgebrochen.
  Hinweis: LXC und QEMU-VMs teilen sich den ID-Raum — eine belegte ID kann auch eine
  VM sein (`VM 103 already exists`). Das Script erkennt beides (`qm` + pmxcfs-Configs)
  und weicht aus; nur ein CT mit gleichem Hostnamen wird geupdatet.
  Falls du eine alte Script-Version erwischt hast (ohne diese Erkennung):
  Einzeiler erneut laden (aktuelle Version vom `main`-Branch) und erneut laufen lassen.
- **Service nicht aktiv?** Im Container schauen: `pct exec <ID> -- journalctl -u kado --no-pager -n 50`.
- **Keine IP?** `pct exec <ID> -- hostname -I`; Bridge/DHCP prüfen: `pct config <ID>`.
- **Installationsfehler?** Log auf dem Host unter `/tmp/kado-web-install-*.log`, oder mit `DEBUG=1` erneut laufen lassen.

## Erwartete Ausgabe (Erfolg)

```
[OK]    Freie CT-ID gefunden: 103 (Hostname: kadoHabbit).
[OK]    Container erstellt & gestartet (onboot=1).
[OK]    App installiert & Dienst gestartet.
  systemctl is-active kado → active
{"status":"ok","app":"kado-web"}
[OK]    Web-UI antwortet auf localhost:8080.
[OK]    Fertig! 🎉
  Web-UI : http://192.168.1.103:8080
```

## Lizenz & Herkunft (bitte lesen)

- **Dieser Installer + Web-Nachbau:** MIT — siehe `LICENSE` (Copyright 2026 HatchetMan111).
- **Upstream-Idee & Algorithmus:** [scastiel/kado](https://github.com/scastiel/kado) (MIT, © Sébastien Castiel).
  Es wurde **kein Upstream-Code und kein Branding** übernommen (keine SVGs, keine
  Screenshots, keine Swift-Dateien) — die Score-/Streak-Logik ist anhand der
  öffentlichen Specs (`docs/habit-score.md`, `docs/streak.md`) **eigenständig
  reimplementiert**. Die EMA-Formel selbst ist Standard-Mathematik, nicht schutzfähig.
- **Loop-Habit-Tracker-Algorithmus-Idee:** eigenständig reimplementiert, kein Copy
  (Loop steht unter GPLv3 — darum bewusst keine Code-Übernahme).
- **Inoffiziell:** Dieses Projekt ist *nicht* mit Sébastien Castiel / Kadō affiliiert.
  „Kadō" bleibt Marke des Upstream-Autors; der Container heißt bewusst `kadoHabbit`
  (eigener Name für den Nachbau), nicht „Kadō".
- **Stil-Anlehnung:** „im Stil der Community Scripts" bezieht sich nur auf das
  Bedienkonzept (Einzeiler, Variablen oben, Verifikation) — kein Code von dort kopiert.
