#!/bin/bash
# Standalone OpenSim Hypergrid. Configuration is regenerated from environment variables at each startup.
# No interactive console: OpenSim reads commands from a FIFO populated by the web console.
set -euo pipefail
BIN=/opt/opensim/bin
DATA=/data
cd "$BIN"

# Remove a trailing \r (for .env files edited on Windows).
for v in PUBLIC_HOST PUBLIC_PORT ESTATE_NAME ADMIN_FIRST ADMIN_LAST ADMIN_EMAIL ADMIN_PASS REGION_NAME REGION_LOCATION GRID_NAME GRID_NICK WEB_USER WEB_PASS; do
  [ -n "${!v:-}" ] && export "$v=${!v%$'\r'}"
done

: "${PUBLIC_HOST:?PUBLIC_HOST is required}" "${PUBLIC_PORT:?PUBLIC_PORT is required}"

# --- Configuration ---
VARS='${PUBLIC_HOST} ${PUBLIC_PORT} ${ESTATE_NAME} ${ADMIN_FIRST} ${ADMIN_LAST} ${ADMIN_EMAIL} ${ADMIN_PASS}'
envsubst "$VARS" < /opt/config/OpenSim.ini.tmpl > OpenSim.ini
# StandaloneCommon.ini is loaded after OpenSim.ini and takes precedence, so set the grid name there.
cp -f config-include/StandaloneCommon.ini.example config-include/StandaloneCommon.ini
GRID_NAME="${GRID_NAME:-Oasis}"; GRID_NICK="${GRID_NICK:-oasis}"
sed -i -E "s|^[[:space:];]*gridname[[:space:]]*=.*|    gridname = \"${GRID_NAME}\"|; s|^[[:space:];]*gridnick[[:space:]]*=.*|    gridnick = \"${GRID_NICK}\"|" config-include/StandaloneCommon.ini
grep -nE '^[[:space:]]*grid(name|nick)' config-include/StandaloneCommon.ini | sed 's/^/[entrypoint] /'

# --- Initial region (created once, with a stable UUID stored in /data) ---
mkdir -p "$DATA/Regions" "$DATA/db" "$DATA/logs"

# --- Apply a restore scheduled by the web console (/data/restore.pending) ---
if [ -f "$DATA/restore.pending" ]; then
  RNAME="$(tr -d '\r\n' < "$DATA/restore.pending")"
  RSRC="$DATA/backups/$RNAME"
  if [[ "$RNAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] && ls "$RSRC"/db/*.db >/dev/null 2>&1; then
    SAVE="$DATA/backups/avant-restauration-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$SAVE/db"
    mv "$DATA"/db/*.db "$SAVE/db/" 2>/dev/null || true
    cp -a "$RSRC"/db/*.db "$DATA/db/"
    for region_file in "$RSRC"/Regions/*.ini "$RSRC"/Regions/*.inactive; do
      [ -f "$region_file" ] || continue
      region_name="$(basename "$region_file")"
      region_base="${region_name%.*}"
      if [[ "$region_name" == *.ini ]]; then opposite="$region_base.inactive"; else opposite="$region_base.ini"; fi
      mkdir -p "$SAVE/Regions"
      for existing in "$DATA/Regions/$region_name" "$DATA/Regions/$opposite"; do
        if [ -e "$existing" ]; then cp -a "$existing" "$SAVE/Regions/"; fi
      done
      rm -f "$DATA/Regions/$opposite"
      cp -a "$region_file" "$DATA/Regions/$region_name"
    done
    echo "[entrypoint] RESTAURATION appliquée depuis $RNAME (état précédent conservé dans $(basename "$SAVE"))"
  else
    echo "[entrypoint] restauration ignorée : sauvegarde '$RNAME' introuvable ou invalide"
  fi
  rm -f "$DATA/restore.pending"
fi
if ! ls "$DATA"/Regions/*.ini >/dev/null 2>&1 && ! ls "$DATA"/Regions/*.inactive >/dev/null 2>&1; then
  export REGION_UUID="$(cat /proc/sys/kernel/random/uuid)"
  envsubst '${REGION_NAME} ${REGION_UUID} ${REGION_LOCATION} ${PUBLIC_HOST} ${PUBLIC_PORT}' \
    < /opt/config/Region.ini.tmpl > "$DATA/Regions/${REGION_NAME}.ini"
fi
if [ -e Regions ] && [ ! -L Regions ]; then rm -rf Regions; fi
ln -sfn "$DATA/Regions" Regions

# --- SQLite persistence: redirect each database referenced in config-include (or already in /data/db) to /data/db ---
mapfile -t DBS < <( { grep -rhoE '(file:|Data Source=)[A-Za-z0-9_./-]+\.db' config-include 2>/dev/null | sed -E 's/^(file:|Data Source=)//'; \
                      for f in "$DATA"/db/*.db; do [ -e "$f" ] && basename "$f"; done; } | sort -u )
for p in "${DBS[@]}"; do
  [ -z "$p" ] && continue
  mkdir -p "$(dirname "$p")"
  ln -sfn "$DATA/db/$(basename "$p")" "$p"
done
echo "[entrypoint] bases SQLite redirigées vers /data/db : ${DBS[*]:-aucune}"

# --- Other persistent state directories ---
for d in ScriptEngines maptiles; do
  mkdir -p "$DATA/$d"
  if [ -e "$d" ] && [ ! -L "$d" ]; then rm -rf "$d"; fi
  ln -sfn "$DATA/$d" "$d"
done

# --- Log and command FIFO ---
OUT="$DATA/logs/console.log"
python3 /opt/webconsole/console_log.py "$OUT" --initialize
FIFO=/tmp/os.in
rm -f "$FIFO"; mkfifo "$FIFO"
LOGFIFO=/tmp/os.log
rm -f "$LOGFIFO"; mkfifo "$LOGFIFO"
exec 3<>"$FIFO"        # Keep the FIFO open so OpenSim does not receive end-of-file.

export CONSOLE_FIFO="$FIFO" CONSOLE_LOG="$OUT"
# Supervise the web console: exit code 42 requests a console-only restart; unexpected failures are retried.
# Exit code 0 means the console is disabled or misconfigured, so do not retry.
console_supervisor() {
  local rc
  while true; do
    rc=0; python3 /opt/webconsole/webconsole.py || rc=$?
    case "$rc" in
      0)  return 0 ;;
      42) echo "[entrypoint] console web relancée (demande de l'interface)" ;;
      *)  echo "[entrypoint] console web arrêtée (code $rc), nouvel essai dans ${CONSOLE_RESTART_DELAY:-5} s"
          sleep "${CONSOLE_RESTART_DELAY:-5}" ;;
    esac
  done
}
console_supervisor &

echo "OpenSim démarre : http://${PUBLIC_HOST}:${PUBLIC_PORT}/  (région ${REGION_NAME:-?})"
STOPPING=0
# docker stop (SIGTERM) triggers a clean shutdown command without restarting OpenSim.
on_term() { STOPPING=1; echo "[entrypoint] arrêt demandé : shutdown"; echo "shutdown" > "$FIFO"; }
trap on_term TERM INT

# Restart OpenSim on a console request (/data/restart.flag after a clean shutdown) or after an unexpected
# non-zero exit, up to OPENSIM_MAX_CRASHES consecutive times. A normal exit stops the container.
rm -f "$DATA/restart.flag"
CRASHES=0
while true; do
  STARTED=$(date +%s)
  python3 /opt/webconsole/console_log.py "$OUT" < "$LOGFIFO" &
  LOGPID=$!
  dotnet OpenSim.dll -console=basic < "$FIFO" > "$LOGFIFO" 2>&1 &
  OSPID=$!
  RC=0; wait "$OSPID" || RC=$?
  while kill -0 "$OSPID" 2>/dev/null; do wait "$OSPID" || RC=$?; done   # A signal can interrupt the first wait.
  LOGRC=0; wait "$LOGPID" || LOGRC=$?
  while kill -0 "$LOGPID" 2>/dev/null; do wait "$LOGPID" || LOGRC=$?; done
  if [ "$LOGRC" -ne 0 ]; then
    echo "[entrypoint] erreur du journal de console (code $LOGRC) : arrêt" >&2
    exit "$LOGRC"
  fi
  [ "$STOPPING" = 1 ] && break
  if [ -f "$DATA/restart.flag" ]; then rm -f "$DATA/restart.flag"; CRASHES=0; echo "[entrypoint] relance d'OpenSim demandée"; continue; fi
  if [ "$RC" -ne 0 ]; then
    [ $(( $(date +%s) - STARTED )) -gt 600 ] && CRASHES=0
    CRASHES=$((CRASHES + 1))
    if [ "$CRASHES" -le "${OPENSIM_MAX_CRASHES:-5}" ]; then
      echo "[entrypoint] OpenSim s'est arrêté (code $RC) : relance $CRASHES dans ${OPENSIM_RESTART_DELAY:-5} s"
      sleep "${OPENSIM_RESTART_DELAY:-5}"; continue
    fi
    echo "[entrypoint] trop d'arrêts inattendus : abandon"
  fi
  break
done

# Safety net: move any database created outside the symlinks into /data/db so it is preserved.
for f in "$BIN"/*.db; do
  [ -e "$f" ] && [ ! -L "$f" ] && { echo "[entrypoint] ATTENTION: $(basename "$f") n'était pas persistée, copiée dans /data/db"; mv "$f" "$DATA/db/"; }
done
exit 0
