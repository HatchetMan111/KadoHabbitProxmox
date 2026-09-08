#!/usr/bin/env bash
# ============================================================================
# Kado-Web · Proxmox LXC Installer im Stil der Proxmox VE Community Scripts
# App: lokaler Habit-Tracker-Nachbau zu scastiel/kado (Kadō)
#      Score statt Streak (EMA α=0.05) · Python/FastAPI · SQLite · kein Cloud-Zwang
# Upstream-Idee: https://github.com/scastiel/kado (native iOS-App, läuft NICHT
#      in LXC — darum hostet dieses Script den kompatiblen Web-Nachbau hier.)
#
# Einzeiler (auf dem Proxmox-Host als root):
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/KadoHabbitProxmox/main/install/kado.sh)"
# Debug (volle Ablaufverfolgung):
#   DEBUG=1 bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/KadoHabbitProxmox/main/install/kado.sh)"
#   oder: bash -x install/kado.sh
#
# Getestet auf: Proxmox VE 8.x, Debian-12-Template
# License: MIT
# ============================================================================
set -euo pipefail
set -E  # ERR-Trap auch in Funktionen/Subshels vererben — sonst stille Abbrüche ohne Fehlerkette
trap fail ERR  # früh setzen (Funktion wird zur Laufzeit aufgelöst); schützt auch exec/Preflight
[[ "${DEBUG:-0}" == "1" ]] && set -x

# ----------------------------- Variablen (oben) -----------------------------
APP="kado-web"
APP_PORT="${APP_PORT:-8080}"
CTID="${CTID:-103}"
CT_HOSTNAME="${CT_HOSTNAME:-kadoHabbit}"  # NOTE: heißt bewusst CT_HOSTNAME — $HOSTNAME ist auf dem Host schon belegt!
CTID_AUTO="${CTID_AUTO:-1}"      # 1 = falls Nr. vergeben, nächste freie wählen; 0 = strikt diese ID
CTID_FORCE_UPDATE="${CTID_FORCE_UPDATE:-0}"  # 1 = existierenden CT updaten statt auszuweichen
CPU="${CPU:-1}"
RAM="${RAM:-1024}"            # MiB
DISK="${DISK:-4}"             # GiB
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
OS_TEMPLATE="${OS_TEMPLATE:-}"   # leer = auto (debian-12-standard_*)
UNPRIVILEGED="${UNPRIVILEGED:-1}"
ONBOOT="${ONBOOT:-1}"
PASSWORD="${PASSWORD:-}"         # leer = automatisch
# Woher kommt der App-Code:
GITHUB_REPO="${GITHUB_REPO:-HatchetMan111/KadoHabbitProxmox}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
REPO_URL="https://github.com/${GITHUB_REPO}.git"
RAW_BASE="https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}"
# ----------------------------------------------------------------------------

LOG_FILE="/tmp/${APP}-install-$(date +%F-%H%M%S).log"
# Fällt das Tee-Log aus (z. B. /tmp voll/ro), trotzdem weiter — aber laut melden.
exec > >(tee -a "$LOG_FILE") 2>&1 || { echo "WARN: Tee-Log nach $LOG_FILE nicht möglich, weiter ohne." >&2; }

# ------------------------------- Farben/Log -------------------------------
GN="\e[32m"; YW="\e[33m"; RD="\e[31m"; BL="\e[36m"; CL="\e[0m"
msg_info()  { echo -e "${BL}[INFO]${CL}  $*"; }
msg_ok()    { echo -e "${GN}[OK]${CL}    $*"; }
msg_warn()  { echo -e "${YW}[WARN]${CL}  $*"; }
msg_error() { echo -e "${RD}[FEHLER]${CL} $*" >&2; }

# Volle Fehlerkette: Exit-Code, Befehl, Zeile, Stack, Logs, Repro-Hinweis.
fail() {
  local rc=$? cmd="${BASH_COMMAND:-?}" line="${BASH_LINENO[0]:-?}"
  msg_error "Installation abgebrochen."
  echo "  Exit-Code : $rc" >&2
  echo "  Befehl    : $cmd" >&2
  echo "  Zeile     : $line" >&2
  echo "  Stacktrace:" >&2
  local i=0
  while caller $i >&2 2>/dev/null; do i=$((i + 1)); done || true
  echo "  Logdatei  : $LOG_FILE" >&2
  echo "  Repro     : DEBUG=1 bash -x install/kado.sh (oder Einzeiler mit DEBUG=1)" >&2
  echo "  Letzte 30 Logzeilen:" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  exit "$rc"
}
# (trap fail ERR steht oben bei set -E, damit auch frühe Fehler abgefangen werden)

usage() {
  cat <<EOF
$APP Installer — erstellt LXC "$CT_HOSTNAME" (CT-ID ab $CTID, Auto-Next bei Belegung)

Env-Variablen (alle optional):
  CTID=$CTID CTID_AUTO=1 CTID_FORCE_UPDATE=0 CT_HOSTNAME=$CT_HOSTNAME
  CPU=$CPU RAM=$RAM DISK=$DISK
  STORAGE=$STORAGE TEMPLATE_STORAGE=$TEMPLATE_STORAGE BRIDGE=$BRIDGE
  APP_PORT=$APP_PORT GITHUB_REPO=$GITHUB_REPO GITHUB_BRANCH=$GITHUB_BRANCH
  PASSWORD=(leer=auto) DEBUG=1

Beispiele:
  bash install/kado.sh                                  # nimmt 103 oder nächste freie
  CTID=110 APP_PORT=8080 bash install/kado.sh
  CTID_FORCE_UPDATE=1 CTID=103 bash install/kado.sh     # bestehenden CT 103 updaten
  DEBUG=1 bash -x install/kado.sh
EOF
}
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }

header() {
  echo -e "${GN}  _  __         _       ${CL}"
  echo -e "${GN} | |/ /__ _  __| | ___  ${CL}  $APP · Proxmox LXC Installer"
  echo -e "${GN} | ' // _\` |/ _\` |/ _ \\ ${CL}  CPU=$CPU RAM=${RAM}MB Disk=${DISK}GB Port=$APP_PORT"
  echo -e "${GN} | . \\ (_| | (_| | (_) |${CL}  CT=$CTID ($CT_HOSTNAME) Repo=$GITHUB_REPO"
  echo -e "${GN} |_|\\_\\__,_|\\__,_|\\___/ ${CL}"
}

need_host_tool() { command -v "$1" >/dev/null 2>&1 || { msg_error "Host-Tool fehlt: $1 (auf Proxmox-Host als root ausführen)"; exit 1; }; }
pct_exec() { pct exec "$CTID" -- bash -c "$*"; }

pick_template() {
  if [[ -n "$OS_TEMPLATE" ]]; then echo "$OS_TEMPLATE"; return; fi
  local tpl
  tpl=$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '/debian-12-standard.*amd64.*tar/ {print $1}' | sort -V | tail -n1 || true)
  if [[ -z "$tpl" ]]; then
    msg_info "Debian-12-Template wird heruntergeladen (pveam)…"
    pveam update >/dev/null
    # Paketname ist das LETZTE Feld von `pveam available` (Spaltenzahl variiert!) — nie $2.
    pveam download "$TEMPLATE_STORAGE" "$(pveam available 2>/dev/null | awk '/debian-12-standard.*amd64/ {print $NF}' | sort -V | tail -n1)"
    tpl=$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '/debian-12-standard.*amd64.*tar/ {print $1}' | sort -V | tail -n1 || true)
  fi
  [[ -z "$tpl" ]] && { msg_error "Kein Debian-Template gefunden. Setze OS_TEMPLATE manuell."; exit 1; }
  echo "${TEMPLATE_STORAGE}:vztmpl/${tpl##*/}"
}

PVE_CONF_DIR="${PVE_CONF_DIR:-/etc/pve}"  # nur für Tests überschreibbar
ctid_in_use() {
  # pct sieht keine QEMU-VMs und qm keine Container — darum zusätzlich die
  # Config-Dateien in pmxcfs prüfen (gilt für LXC *und* QEMU, alle Nodes).
  # Echte Pfade: /etc/pve/nodes/<node>/{lxc,qemu-server}/<id>.conf
  # (NICHT /etc/pve/lxc/ — das Verzeichnis existiert nicht.)
  local id="$1" f
  for f in "${PVE_CONF_DIR}"/nodes/*/lxc/"${id}.conf" "${PVE_CONF_DIR}"/nodes/*/qemu-server/"${id}.conf"; do
    [[ -e "$f" ]] && return 0
  done
  pct status "$id" >/dev/null 2>&1 && return 0
  qm status "$id" >/dev/null 2>&1 && return 0
  return 1
}
ctid_hostname() {
  # Muss für QEMU-VM-IDs (pct config scheitert) LEER und status-0 liefern:
  # ohne `|| true` schlägt die Pipe via pipefail durch und set -E bricht ab.
  pct config "$1" 2>/dev/null | awk -F': ' '/^hostname:/ {print $2}' || true
}
container_exists() { ctid_in_use "$CTID"; }
container_ip() { pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}'; }

# Falls Nr. vergeben: nächste freie wählen — außer der CT gehört schon zu uns (Update).
# WICHTIG: LXC und QEMU-VMs teilen sich den ID-Raum. `pct status` allein sieht
# keine QEMU-VMs — darum prüft ctid_in_use zusätzlich qm + pmxcfs-Configs.
# Genau das war der Bug hinter "VM 103 already exists on node ...".
is_qemu_vm() { ! pct config "$1" >/dev/null 2>&1 && qm status "$1" >/dev/null 2>&1; }
resolve_ctid() {
  local wanted="$CTID"
  if [[ "$CTID_FORCE_UPDATE" == "1" ]]; then
    if is_qemu_vm "$CTID"; then
      msg_error "CTID_FORCE_UPDATE=1, aber ID $CTID ist eine QEMU-VM (kein Container) — Update unmöglich. Andere CTID wählen."
      exit 1
    fi
    msg_info "CTID_FORCE_UPDATE=1 → verwende CT $CTID (Update)."
    return
  fi
  if ! ctid_in_use "$CTID"; then return; fi
  local owner; owner=$(ctid_hostname "$CTID")
  if is_qemu_vm "$CTID"; then owner="QEMU-VM (kein Container)"; fi
  if [[ "$owner" == "$CT_HOSTNAME" ]]; then
    msg_warn "CT $CTID existiert bereits und heißt '$owner' → Update-Pfad (idempotent)."
    return
  fi
  if [[ "$CTID_AUTO" != "1" ]]; then
    msg_error "CT $CTID ist bereits vergeben (${owner:-unbekannt}). Freie ID wählen oder CTID_AUTO=1 setzen."
    exit 1
  fi
  msg_warn "CT $wanted vergeben (${owner:-unbekannt}) → suche nächste freie ID…"
  while ctid_in_use "$CTID"; do
    CTID=$((CTID + 1))
    [[ "$CTID" -gt 999999 ]] && { msg_error "Keine freie CT-ID gefunden."; exit 1; }
  done
  msg_ok "Freie CT-ID gefunden: $CTID (Hostname: $CT_HOSTNAME)."
}

create_container() {
  local tpl; tpl=$(pick_template)
  msg_info "Erstelle LXC $CTID ($CT_HOSTNAME) aus $tpl …"
  local pw_args=()
  # Hex statt Base64: keine Sonderzeichen (/, +, =) im pct-Passwort.
  if [[ -n "$PASSWORD" ]]; then pw_args=(--password "$PASSWORD"); else pw_args=(--password "$(openssl rand -hex 12)"); fi
  pct create "$CTID" "$tpl" \
    --hostname "$CT_HOSTNAME" --cores "$CPU" --memory "$RAM" \
    --rootfs "${STORAGE}:${DISK}" --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
    --onboot "$ONBOOT" --unprivileged "$UNPRIVILEGED" \
    --features nesting=1 "${pw_args[@]}"
  pct start "$CTID"
  msg_ok "Container erstellt & gestartet (onboot=$ONBOOT)."
}

wait_for_container() {
  msg_info "Warte auf Container-Netzwerk (max 90s)…"
  for i in $(seq 1 45); do
    sleep 2
    if pct_exec "getent hosts deb.debian.org >/dev/null 2>&1 || ping -c1 -W1 8.8.8.8 >/dev/null 2>&1"; then
      msg_ok "Container ist online."; return
    fi
  done
  msg_error "Container kam nicht online. Prüfe Bridge/DHCP: pct config $CTID"
  exit 1
}

# Läuft IM Container (via pct push + pct exec): idempotent, mit eigenem Trap,
# damit Container-Fehler Zeile + Kommando melden statt nur einen Exit-Code.
install_in_container() {
  msg_info "Installiere $APP im Container (idempotent)…"
  # Über eine echte Temp-Datei pushen — /dev/stdin als pct-push-Quelle ist fragil
  # (hängt am stdin des pct-Prozesses und bricht bei manchen Versionen/Terminals).
  local setup_tmp
  setup_tmp="$(mktemp /tmp/kado-setup.XXXXXX.sh)"
  cat > "$setup_tmp" <<SETUP_EOF
set -euo pipefail
set -E
kado_err() {
  local code=\$?
  echo "[kado-setup][FEHLER] Exit-Code: \${code}, Kommando: \${BASH_COMMAND:-?}, Zeile: \${BASH_LINENO[0]:-?}" >&2
}
trap kado_err ERR
export DEBIAN_FRONTEND=noninteractive APP_PORT="$APP_PORT" REPO_URL="$REPO_URL" RAW_BASE="$RAW_BASE"
echo "[kado-setup] apt…"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates openssl >/dev/null
id kado >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin kado
mkdir -p /opt/kado /var/lib/kado
if [[ -d /opt/kado/.git ]]; then
  echo "[kado-setup] update via git pull…"
  git -C /opt/kado pull --ff-only || true
else
  if git ls-remote "\$REPO_URL" HEAD >/dev/null 2>&1; then
    echo "[kado-setup] klone \$REPO_URL…"
    rm -rf /opt/kado && git clone --depth 1 "\$REPO_URL" /opt/kado
  else
    echo "[kado-setup] Fallback: lade app.py + requirements + unit via RAW (\$RAW_BASE)…"
    curl -fsSL "\$RAW_BASE/src/app.py" -o /opt/kado/app.py
    curl -fsSL "\$RAW_BASE/src/requirements.txt" -o /opt/kado/requirements.txt
    curl -fsSL "\$RAW_BASE/systemd/kado.service" -o /etc/systemd/system/kado.service
  fi
fi
# Falls Repo-Checkout: Unit aus Repo übernehmen
if [[ -f /opt/kado/systemd/kado.service ]]; then cp /opt/kado/systemd/kado.service /etc/systemd/system/kado.service; fi
if [[ -f /opt/kado/src/app.py && ! -f /opt/kado/app.py ]]; then cp /opt/kado/src/app.py /opt/kado/app.py; fi
if [[ -f /opt/kado/src/requirements.txt && ! -f /opt/kado/requirements.txt ]]; then cp /opt/kado/src/requirements.txt /opt/kado/requirements.txt; fi
[[ -f /opt/kado/app.py ]] || { echo "[kado-setup] FEHLER: /opt/kado/app.py fehlt" >&2; exit 1; }
# venv-Fehler NICHT verschlucken (kein 2>/dev/null, kein || true): sonst scheitert
# später pip kryptisch. Re-Run auf existierender venv ist ok (exit 0).
python3 -m venv /opt/kado/.venv
/opt/kado/.venv/bin/pip install -q --upgrade pip
/opt/kado/.venv/bin/pip install -q -r /opt/kado/requirements.txt
chown -R kado:kado /opt/kado /var/lib/kado
# PORT setzen — mit Fallback, falls die Unit-Zeile mal fehlt (dann append statt wirkungslos).
if grep -q '^Environment=PORT=' /etc/systemd/system/kado.service; then
  sed -i "s/^Environment=PORT=.*/Environment=PORT=\$APP_PORT/" /etc/systemd/system/kado.service
else
  echo "Environment=PORT=\$APP_PORT" >> /etc/systemd/system/kado.service
fi
systemctl daemon-reload
systemctl enable -q kado
systemctl restart kado
# Firewall-Port best-effort öffnen
(iptables -C INPUT -p tcp --dport \$APP_PORT -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport \$APP_PORT -j ACCEPT 2>/dev/null) || true
echo "[kado-setup] fertig."
SETUP_EOF
  pct push "$CTID" "$setup_tmp" /tmp/kado-setup.sh
  rm -f "$setup_tmp"
  pct exec "$CTID" -- bash /tmp/kado-setup.sh
  msg_ok "App installiert & Dienst gestartet."
}

verify() {
  msg_info "Verifiziere Installation…"
  local state ip
  state=$(pct exec "$CTID" -- systemctl is-active kado 2>&1 | tr -d '[:space:]')
  echo "  systemctl is-active kado → $state"
  [[ "$state" == "active" ]] || {
    msg_error "Service nicht aktiv. Log im Container:"
    pct exec "$CTID" -- journalctl -u kado --no-pager -n 50 || true
    exit 1
  }
  pct exec "$CTID" -- curl -fsS "http://localhost:${APP_PORT}/healthz"
  echo ""
  msg_ok "Web-UI antwortet auf localhost:$APP_PORT."
  ip=$(container_ip)
  echo ""
  msg_ok "Fertig! 🎉"
  echo -e "  Web-UI : ${GN}http://${ip:-<CT-IP>}:${APP_PORT}${CL}"
  echo -e "  Health : http://${ip:-<CT-IP>}:${APP_PORT}/healthz"
  echo "  Container-IP: ${ip:-unbekannt (pct exec $CTID -- hostname -I)}"
  echo "  Update    : CTID_FORCE_UPDATE=1 CTID=$CTID bash install/kado.sh   (idempotent)"
  echo "  Deinstall : pct stop $CTID && pct destroy $CTID"
  echo "  Log       : $LOG_FILE (Host) · journalctl -u kado (im CT $CTID)"
}

main() {
  header
  msg_info "Preflight: Root-Check + Tool-Check (pct, pveam, wget)…"
  [[ $EUID -eq 0 ]] || { msg_error "Bitte als root auf dem Proxmox-Host ausführen."; exit 1; }
  need_host_tool pct; need_host_tool pveam; need_host_tool wget
  command -v qm >/dev/null 2>&1 || msg_warn "'qm' nicht gefunden — QEMU-Belegung wird nur via pmxcfs-Configs erkannt."
  msg_info "Tool-Check ok."
  resolve_ctid
  msg_info "Verwende CT $CTID ($CT_HOSTNAME)."
  if container_exists; then
    msg_warn "CT $CTID existiert bereits → Update-Pfad (idempotent, kein Re-Create)."
    pct start "$CTID" 2>/dev/null || true
  else
    create_container
  fi
  pct set "$CTID" --onboot "$ONBOOT" 2>/dev/null || true   # reboot-sicher
  wait_for_container
  install_in_container
  verify
}
main "$@"
