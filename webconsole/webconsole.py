#!/usr/bin/env python3
"""Console web OpenSim v2 (bibliothèque standard uniquement).

- connexion par formulaire + session (cookie HttpOnly/SameSite=Strict), blocage anti-force-brute,
  TOTP optionnel, compte "lecteur" optionnel (lecture seule)
- commandes envoyées à OpenSim via un FIFO ; journal lu depuis un fichier (lecture incrémentale)
- tableau de bord (processus, mémoire, disque, grille, régions), utilisateurs (lecture SQLite),
  sauvegardes à chaud (API de sauvegarde SQLite + OAR), restauration différée, journal d'audit
"""
import base64, configparser, glob, hashlib, hmac, http.cookies, json, os, re, secrets, shutil
import sqlite3, struct, sys, tempfile, threading, time, uuid, zipfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

E = os.environ.get
DATA = E("DATA_DIR", "/data")
FIFO = E("CONSOLE_FIFO", "/tmp/os.in")
LOG = E("CONSOLE_LOG", f"{DATA}/logs/console.log")
ACTIONS = E("ACTIONS_LOG", f"{DATA}/logs/web-actions.log")
DBDIR, REGDIR, BACKUPS = f"{DATA}/db", f"{DATA}/Regions", f"{DATA}/backups"
USER, PASSWORD = E("WEB_USER", "admin"), E("WEB_PASS", "")
VIEWER_USER, VIEWER_PASS = E("WEB_VIEWER_USER", ""), E("WEB_VIEWER_PASS", "")
TOTP_SECRET = E("WEB_TOTP_SECRET", "").strip()
PORT = int(E("WEB_PORT") or E("WEB_PORT_INTERNAL") or "8090")
BIND = E("WEB_BIND") or E("HOST_LAN_IP") or "0.0.0.0"
PUBLIC_HOST, PUBLIC_PORT = E("PUBLIC_HOST", "localhost"), E("PUBLIC_PORT", "9000")
GRID_NAME = E("GRID_NAME", "OpenSim")
BACKUP_KEEP = int(E("BACKUP_KEEP", "10") or "10")
BACKUP_EVERY_H = float(E("BACKUP_EVERY_HOURS", "0") or "0")
STATS_URL = E("STATS_BASE_URL", f"http://127.0.0.1:{PUBLIC_PORT}")
TRANSFER = f"{DATA}/transfer"
MAX_UPLOAD = int(E("MAX_UPLOAD_MB", "2048") or "2048") * 1048576
XFER = {"oar": (".oar", ".tgz"), "iar": (".iar",)}
FNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

TAIL_BYTES, MAX_CHUNK = 60000, 400000
SESSION_TTL = 12 * 3600
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
AUTO_RE = re.compile(r"^\d{8}-\d{6}")
WORD = re.compile(r"^\S{1,64}$")

if len(PASSWORD) < 8 or PASSWORD == "CHANGE-MOI":
    print("[webconsole] WEB_PASS absent, trop court (8 car. min) ou par défaut : interface web désactivée", flush=True)
    sys.exit(0)


# ---------------------------------------------------------------- utilitaires
def ct_eq(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def totp_ok(secret, code, digits=6, window=1, now=None):
    try:
        s = secret.replace(" ", "").upper()
        key = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        return False
    code = (code or "").strip()
    if not code.isdigit():
        return False
    t = int((now if now is not None else time.time()) // 30)
    for d in range(-window, window + 1):
        h = hmac.new(key, struct.pack(">Q", t + d), hashlib.sha1).digest()
        o = h[-1] & 15
        n = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
        if hmac.compare_digest(str(n).zfill(digits), code):
            return True
    return False


_alock = threading.Lock()


def audit(who, text):
    try:
        with _alock, open(ACTIONS, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {who} {text}\n")
    except OSError:
        pass


def mask_cmd(cmd):
    low = cmd.lower()
    if low.startswith("create user"):
        return " ".join(cmd.split()[:4]) + " ***"
    if low.startswith(("save iar", "load iar")):
        return " ".join(cmd.split()[:2]) + " ***"
    if "password" in low:
        return " ".join(cmd.split()[:4]) + " ***"
    return cmd


class CmdError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def send_cmd(cmd):
    cmd = (cmd or "").strip()   # une commande vide est autorisée : simple retour chariot (accepte la valeur par défaut d'une question)
    if len(cmd) > 500 or any(c in cmd for c in "\r\n\x00"):
        raise CmdError("commande invalide")
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        raise CmdError("OpenSim ne lit pas la console (arrêté ?)", 503)
    try:
        os.write(fd, (cmd + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def console_ok():
    try:
        os.close(os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK))
        return True
    except OSError:
        return False


def iso(v):
    try:
        if v in (None, "", 0, "0"):
            return None
        return datetime.fromtimestamp(int(v), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------- journal
def read_log(pos):
    try:
        size = os.path.getsize(LOG)
    except OSError:
        return {"pos": 0, "text": "", "reset": True}
    reset = False
    if pos is None or pos < 0 or pos > size:
        pos, reset = max(0, size - TAIL_BYTES), True
    elif size - pos > MAX_CHUNK:
        pos, reset = size - MAX_CHUNK, True
    with open(LOG, "rb") as f:
        f.seek(pos)
        data = f.read(size - pos)
    return {"pos": pos + len(data), "text": ANSI.sub("", data.decode("utf-8", "replace")), "reset": reset}


def tail_file(path, n=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


# ---------------------------------------------------------------- système / processus
CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_cpu = {}


def find_pid():
    try:
        for p in os.listdir("/proc"):
            if p.isdigit():
                try:
                    with open(f"/proc/{p}/cmdline", "rb") as f:
                        c = f.read()
                except OSError:
                    continue
                if b"OpenSim.dll" in c and b"dotnet" in c:
                    return int(p)
    except OSError:
        pass
    return None


def proc_info(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        rest = s[s.rindex(")") + 2:].split()
        used = (int(rest[11]) + int(rest[12])) / CLK
        start = int(rest[19]) / CLK
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        rss = None
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
        now, cpu = time.time(), None
        last = _cpu.get(pid)
        if last and now - last[0] > 0.5:
            cpu = round(max(0.0, (used - last[1]) / (now - last[0]) * 100), 1)
        _cpu[pid] = (now, used)
        return {"uptime": int(up - start), "rss": rss, "cpu": cpu}
    except (OSError, ValueError, IndexError):
        return {}


def sysinfo():
    out = {}
    try:
        out["load"] = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
    except OSError:
        pass
    try:
        m = {}
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            m[k] = int(v.split()[0]) * 1024
        out["mem_total"], out["mem_avail"] = m.get("MemTotal"), m.get("MemAvailable")
    except (OSError, ValueError):
        pass
    try:
        du = shutil.disk_usage(DATA)
        out["disk_total"], out["disk_free"] = du.total, du.free
    except OSError:
        pass
    return out


_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    v = _cache.get(key)
    if v and now - v[0] < ttl:
        return v[1]
    try:
        r = fn()
    except Exception:
        r = None
    _cache[key] = (now, r)
    return r


def fetch(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(200000)


def grid_info():
    def go():
        root = ET.fromstring(fetch(f"{STATS_URL}/get_grid_info"))
        return {c.tag: (c.text or "") for c in root}
    return cached("grid", 10, go)


def sim_stats():
    def go():
        d = json.loads(fetch(f"{STATS_URL}/jsonSimStats/"))
        flat = {}
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, (str, int, float, bool)):
                    flat[k] = v
        return flat or None
    return cached("stats", 5, go)


# ---------------------------------------------------------------- régions / liens
def regions():
    out = []
    for f in sorted(glob.glob(os.path.join(REGDIR, "*.ini"))):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        try:
            cp.read(f, encoding="utf-8")
        except configparser.Error:
            continue
        for sec in cp.sections():
            d = cp[sec]
            out.append({"name": sec, "file": os.path.basename(f), "uuid": d.get("RegionUUID"),
                        "location": d.get("Location"),
                        "size": f'{d.get("SizeX", "256")}x{d.get("SizeY", "256")}',
                        "port": d.get("InternalPort"), "host": d.get("ExternalHostName")})
    return out


def links():
    base = f"http://{PUBLIC_HOST}:{PUBLIC_PORT}"
    regs = regions()
    return {"login": base + "/", "gridinfo": base + "/get_grid_info", "gatekeeper": base,
            "teleports": [f"{PUBLIC_HOST}:{PUBLIC_PORT}:{r['name']}" for r in regs],
            "hops": [f"hop://{PUBLIC_HOST}:{PUBLIC_PORT}/{r['name']}" for r in regs]}


def status():
    pid = find_pid()
    os_info = {"running": pid is not None, "pid": pid}
    if pid:
        os_info.update(proc_info(pid))
    return {"opensim": os_info, "console_ok": console_ok(), "system": sysinfo(),
            "grid": grid_info(), "stats": sim_stats(), "regions": regions(), "links": links(),
            "grid_name": GRID_NAME, "host": PUBLIC_HOST, "port": PUBLIC_PORT,
            "avatars": online_avatars(), "restart_job": restart_info(),
            "alerts": {"channels": channels(), "logins": ALERT_LOGINS, "daily": DAILY_AT or None},
            "dbs": [{"name": os.path.basename(f), "size": os.path.getsize(f)}
                    for f in sorted(glob.glob(os.path.join(DBDIR, "*.db")))]}


# ---------------------------------------------------------------- utilisateurs (lecture SQLite)
def open_table(table):
    for f in sorted(glob.glob(os.path.join(DBDIR, "*.db"))):
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return con
            con.close()
        except sqlite3.Error:
            continue
    return None


def list_users(show_email):
    con = open_table("UserAccounts")
    if not con:
        return {"available": False, "users": []}
    try:
        rows = [{k.lower(): r[k] for k in r.keys()} for r in con.execute("SELECT * FROM UserAccounts")]
    except sqlite3.Error:
        return {"available": False, "users": []}
    finally:
        con.close()
    gu = {}
    con = open_table("GridUser")
    if con:
        try:
            for r in con.execute("SELECT * FROM GridUser"):
                d = {k.lower(): r[k] for k in r.keys()}
                gu[str(d.get("userid", "")).split(";")[0].lower()] = d
        except sqlite3.Error:
            pass
        finally:
            con.close()
    users = []
    for r in rows:
        pid = str(r.get("principalid", ""))
        g = gu.get(pid.lower(), {})
        users.append({"id": pid, "first": r.get("firstname"), "last": r.get("lastname"),
                      "email": r.get("email") if show_email else None,
                      "created": iso(r.get("created")), "level": r.get("userlevel"),
                      "last_login": iso(g.get("login")), "last_logout": iso(g.get("logout")),
                      "online": str(g.get("online", "")).lower() == "true"})
    users.sort(key=lambda u: ((u["first"] or "").lower(), (u["last"] or "").lower()))
    return {"available": True, "users": users}


def update_user_account(user_id, email, level=None):
    if not isinstance(user_id, str):
        raise ValueError("identifiant utilisateur invalide")
    try:
        uuid.UUID(user_id)
    except (ValueError, AttributeError):
        raise ValueError("identifiant utilisateur invalide")
    if not isinstance(email, str):
        raise ValueError("e-mail invalide")
    email = email.strip()
    if len(email) > 254 or (email and (email.count("@") != 1 or any(c.isspace() for c in email))):
        raise ValueError("e-mail invalide")
    if email:
        local, domain = email.split("@")
        if not local or len(local) > 64 or not domain or len(domain) > 253:
            raise ValueError("e-mail invalide")
    if level is not None and (type(level) is not int or level not in (-1, 0, 200, 250)):
        raise ValueError("niveau invalide (valeurs autorisées : -1, 0, 200, 250)")

    for path in sorted(glob.glob(os.path.join(DBDIR, "*.db"))):
        con = sqlite3.connect(path, timeout=5)
        try:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='UserAccounts'").fetchone()
            if not exists:
                continue
            columns = {row[1].lower() for row in con.execute("PRAGMA table_info(UserAccounts)")}
            if "principalid" not in columns or "email" not in columns or (level is not None and "userlevel" not in columns):
                raise RuntimeError("schéma UserAccounts incomplet : modification impossible")
            if level is None:
                cur = con.execute("UPDATE UserAccounts SET Email=? WHERE PrincipalID=?", (email, user_id))
            else:
                cur = con.execute("UPDATE UserAccounts SET Email=?, UserLevel=? WHERE PrincipalID=?", (email, level, user_id))
            if cur.rowcount != 1:
                raise ValueError("compte introuvable")
            con.commit()
            return
        finally:
            con.close()
    raise ValueError("base des comptes introuvable")


# ---------------------------------------------------------------- sauvegardes
def bdir(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("nom de sauvegarde invalide")
    root = os.path.realpath(BACKUPS)
    p = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(p) != root or not os.path.isdir(p):
        raise ValueError("sauvegarde introuvable")
    return p


def list_backups():
    items = []
    if os.path.isdir(BACKUPS):
        for n in sorted(os.listdir(BACKUPS), reverse=True):
            d = os.path.join(BACKUPS, n)
            if not NAME_RE.match(n) or not os.path.isdir(d):
                continue
            files, total = [], 0
            for root, _, fs in os.walk(d):
                for fn in fs:
                    p = os.path.join(root, fn)
                    try:
                        sz = os.path.getsize(p)
                    except OSError:
                        continue
                    total += sz
                    files.append({"path": os.path.relpath(p, d), "size": sz})
            items.append({"name": n, "size": total, "files": len(files),
                          "has_db": os.path.isdir(os.path.join(d, "db")),
                          "oar": [f["path"] for f in files if f["path"].endswith(".oar")],
                          "mtime": int(os.path.getmtime(d))})
    return {"items": items, "keep": BACKUP_KEEP, "every_h": BACKUP_EVERY_H}


_blk = threading.Lock()


def make_backup(kind, label="", prefix=""):
    if kind not in ("db", "oar", "both"):
        raise ValueError("type de sauvegarde inconnu")
    with _blk:
        name = (f"{prefix}-" if prefix else "") + time.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
        d = os.path.join(BACKUPS, name)
        os.makedirs(d)
        regs = regions()
        if kind in ("oar", "both") and not regs:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError("aucune région à exporter")
        if kind in ("db", "both"):
            os.makedirs(os.path.join(d, "db"))
            for f in sorted(glob.glob(os.path.join(DBDIR, "*.db"))):
                src = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=30)
                dst = sqlite3.connect(os.path.join(d, "db", os.path.basename(f)))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
            os.makedirs(os.path.join(d, "Regions"))
            for f in glob.glob(os.path.join(REGDIR, "*.ini")):
                shutil.copy2(f, os.path.join(d, "Regions"))
        oar_regions = []
        if kind in ("oar", "both"):
            for index, configured_region in enumerate(regs, 1):
                region = pick_region(configured_region["name"])
                slug = re.sub(r"[^A-Za-z0-9_-]", "_", region).strip("_") or "region"
                filename = f"{index:03d}-{slug}.oar"
                send_cmd(f"save oar {qarg(os.path.join(d, filename))}")
                oar_regions.append({"file": filename, "region": region})
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"created": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
                       "oar_regions": oar_regions}, f)
    prune()
    return name


def prune():
    if BACKUP_KEEP <= 0 or not os.path.isdir(BACKUPS):
        return
    auto = sorted([n for n in os.listdir(BACKUPS) if AUTO_RE.match(n) and os.path.isdir(os.path.join(BACKUPS, n))])
    for n in auto[:-BACKUP_KEEP]:
        shutil.rmtree(os.path.join(BACKUPS, n), ignore_errors=True)


def scheduler():
    if BACKUP_EVERY_H <= 0:
        return
    while True:
        time.sleep(BACKUP_EVERY_H * 3600)
        try:
            n = make_backup("db", "auto")
            audit("planificateur", f"sauvegarde auto {n}")
        except Exception as e:
            audit("planificateur", f"échec sauvegarde auto: {e}")
            notify("Sauvegarde automatique échouée", str(e))


# ---------------------------------------------------------------- import / export (OAR, IAR)
def xfer_dir(kind):
    if kind not in XFER:
        raise ValueError("type inconnu")
    d = os.path.join(TRANSFER, kind)
    os.makedirs(d, exist_ok=True)
    return d


def xfer_name_ok(kind, name):
    return isinstance(name, str) and FNAME_RE.match(name) and name.lower().endswith(XFER[kind])


def xfer_file(kind, name):
    d = os.path.realpath(xfer_dir(kind))
    if not xfer_name_ok(kind, name):
        raise ValueError("nom de fichier invalide")
    p = os.path.realpath(os.path.join(d, name))
    if os.path.dirname(p) != d or not os.path.isfile(p):
        raise ValueError("fichier introuvable")
    return p


def list_transfer(kind):
    d = xfer_dir(kind)
    items = []
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if xfer_name_ok(kind, n) and os.path.isfile(p):
            st = os.stat(p)
            items.append({"name": n, "size": st.st_size, "mtime": int(st.st_mtime)})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"items": items, "max_mb": MAX_UPLOAD // 1048576}


def clean_upload_name(raw, kind):
    base = os.path.basename(str(raw).replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip("._-")[:100]
    if not xfer_name_ok(kind, base):
        raise ValueError("extension non acceptée (" + ", ".join(XFER[kind]) + ")")
    return base


def qarg(v):
    return f'"{v}"' if re.search(r"\s", v) else v


def inv_arg(v):
    v = str(v or "/*").strip()
    if not v.startswith("/") or len(v) > 200 or any(c in v for c in '"\r\n\x00'):
        raise ValueError("chemin d'inventaire invalide (doit commencer par /)")
    return qarg(v)


def pick_region(name):
    regs = [r["name"] for r in regions()]
    if not regs:
        raise ValueError("aucune région configurée")
    name = str(name or regs[0])
    if name not in regs:
        raise ValueError("région inconnue")
    send_cmd(f"change region {qarg(name)}")
    return name


# ---------------------------------------------------------------- gestion des régions
REGNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


def _cells(r):
    x, y = (int(v) for v in str(r["location"]).split(","))
    return x, y, max(1, int(str(r["size"]).split("x")[0]) // 256)


def _overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[2] and b[1] < a[1] + a[2]


def region_suggest(size=256):
    regs = regions()
    used = [_cells(r) for r in regs if r["location"]]
    x = 1000
    while any(_overlap((x, 1000, size // 256), u) for u in used):
        x += 1
    ports = {int(r["port"]) for r in regs if r["port"]} | {int(PUBLIC_PORT)}
    p = int(PUBLIC_PORT) + 1
    while p in ports:
        p += 1
    return {"location": f"{x},1000", "port": p}


def region_create(b):
    name = str(b.get("name", "")).strip()
    if not REGNAME_RE.match(name):
        raise ValueError("nom invalide (lettres, chiffres, espaces, - et _ ; 40 caractères max)")
    regs = regions()
    if any(r["name"].lower() == name.lower() for r in regs):
        raise ValueError("une région porte déjà ce nom")
    size = int(b.get("size") or 256)
    if size not in (256, 512, 768, 1024):
        raise ValueError("taille non prise en charge")
    sug = region_suggest(size)
    loc = str(b.get("location") or sug["location"]).strip()
    if not re.match(r"^\d{1,5},\d{1,5}$", loc):
        raise ValueError("position invalide (format x,y)")
    port = int(b.get("port") or sug["port"])
    if not 1024 <= port <= 65535:
        raise ValueError("port invalide")
    if port == int(PUBLIC_PORT) or any(r["port"] and int(r["port"]) == port for r in regs):
        raise ValueError("ce port est déjà utilisé")
    x, y = (int(v) for v in loc.split(","))
    if any(_overlap((x, y, size // 256), _cells(r)) for r in regs if r["location"]):
        raise ValueError("cet emplacement est déjà occupé")
    fn = re.sub(r"[^A-Za-z0-9_-]", "_", name) + ".ini"
    txt = (f"[{name}]\nRegionUUID = {uuid.uuid4()}\nLocation = {loc}\nSizeX = {size}\nSizeY = {size}\nSizeZ = 256\n"
           f"InternalAddress = 0.0.0.0\nInternalPort = {port}\nAllowAlternatePorts = False\nExternalHostName = {PUBLIC_HOST}\n")
    os.makedirs(REGDIR, exist_ok=True)
    try:
        with open(os.path.join(REGDIR, fn), "x", encoding="utf-8") as f:
            f.write(txt)
    except FileExistsError:
        raise ValueError("un fichier de ce nom existe déjà")
    return {"name": name, "file": fn, "port": port, "location": loc}


def region_delete(name, export):
    regs = regions()
    r = next((x for x in regs if x["name"] == name), None)
    if not r:
        raise ValueError("région inconnue")
    if len(regs) < 2:
        raise ValueError("impossible de retirer la dernière région")
    if len([x for x in regs if x["file"] == r["file"]]) > 1:
        raise ValueError("ce fichier contient plusieurs régions : à retirer à la main")
    if export:
        fn = f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}-{time.strftime('%Y%m%d-%H%M%S')}.oar"
        pick_region(name)
        send_cmd("save oar " + os.path.join(xfer_dir("oar"), fn))
    os.makedirs(f"{DATA}/regions-removed", exist_ok=True)
    shutil.move(os.path.join(REGDIR, r["file"]), f"{DATA}/regions-removed/{time.strftime('%Y%m%d-%H%M%S')}-{r['file']}")


# ---------------------------------------------------------------- suppression de comptes
# (table, colonne, préfixe) : données propres à un avatar. Tables/colonnes absentes : ignorées. Objets, parcelles et assets sont conservés.
USER_COLS = (("UserAccounts", "PrincipalID", 0), ("auth", "UUID", 0), ("tokens", "UUID", 0), ("GridUser", "UserID", 1), ("Presence", "UserID", 0),
             ("Avatars", "PrincipalID", 0), ("AgentPrefs", "PrincipalID", 0), ("Friends", "PrincipalID", 0), ("Friends", "Friend", 1),
             ("inventoryfolders", "agentID", 0), ("inventoryitems", "avatarID", 0), ("userprofile", "useruuid", 0), ("usersettings", "useruuid", 0),
             ("usernotes", "useruuid", 0), ("userpicks", "creatoruuid", 0), ("classifieds", "creatoruuid", 0), ("MuteList", "AgentID", 0),
             ("hg_traveling_data", "UserID", 0))


def estate_owners():
    ids, found = set(), False
    for f in glob.glob(os.path.join(DBDIR, "*.db")):
        con = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=5)
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='estate_settings'").fetchone():
                found = True
                ids |= {str(r[0]).lower() for r in con.execute("SELECT EstateOwner FROM estate_settings")}
        finally:
            con.close()
    if not found:
        raise ValueError("impossible de vérifier le propriétaire des domaines : suppression refusée")
    return ids


def delete_user(first, last, confirm):
    if confirm.strip().lower() != f"{first} {last}".lower():
        raise ValueError("confirmation incorrecte : retaper le nom exact de l'avatar")
    con = open_table("UserAccounts")
    uid = None
    if con:
        try:
            r = con.execute("SELECT PrincipalID FROM UserAccounts WHERE FirstName=? COLLATE NOCASE AND LastName=? COLLATE NOCASE", (first, last)).fetchone()
            uid = str(r[0]) if r else None
        finally:
            con.close()
    if not uid:
        raise ValueError("compte introuvable")
    if any(a["name"].lower() == f"{first} {last}".lower() for a in online_avatars()):
        raise ValueError("cet avatar est connecté : le désactiver ou l'expulser d'abord")
    if uid.lower() in estate_owners():
        raise ValueError("cet avatar est propriétaire d'un domaine (estate) : suppression refusée")
    backup = make_backup("db", "", prefix="avant-suppression")
    removed = {}
    for f in sorted(glob.glob(os.path.join(DBDIR, "*.db"))):
        con = sqlite3.connect(f, timeout=15)
        try:
            tables = {r[0].lower(): r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for t, c, pre in USER_COLS:
                real = tables.get(t.lower())
                if not real:
                    continue
                cols = {r[1].lower(): r[1] for r in con.execute(f'PRAGMA table_info("{real}")')}
                if c.lower() not in cols:
                    continue
                cond = f'"{cols[c.lower()]}" LIKE ?' if pre else f'"{cols[c.lower()]}" = ? COLLATE NOCASE'
                n = con.execute(f'DELETE FROM "{real}" WHERE {cond}', (uid + "%" if pre else uid,)).rowcount
                if n:
                    removed[real] = removed.get(real, 0) + n
            con.commit()
        finally:
            con.close()
    return {"backup": backup, "removed": removed}


# ---------------------------------------------------------------- avatars, redémarrage, alertes
NTFY, DISCORD = E("ALERT_NTFY_URL", ""), E("ALERT_DISCORD_WEBHOOK", "")
TG_TOKEN, TG_CHAT = E("ALERT_TELEGRAM_TOKEN", ""), E("ALERT_TELEGRAM_CHAT", "")
ALERT_LOGINS = E("ALERT_LOGINS", "0") == "1"
DISK_PCT = int(E("ALERT_DISK_PCT", "90") or "90")
DAILY_AT, NOTICE_MIN = E("RESTART_DAILY_AT", "").strip(), int(E("RESTART_NOTICE_MIN", "10") or "10")
FLAG = f"{DATA}/restart.flag"
THRESH = (10, 5, 2, 1)
RJOB = {"job": None, "daily_done": ""}
_rl = threading.Lock()


def channels():
    return [n for n, v in (("ntfy", NTFY), ("Discord", DISCORD), ("Telegram", TG_TOKEN and TG_CHAT)) if v]


def _post(url, data, headers):
    h = {"User-Agent": "opensim-webconsole/2", **headers}
    urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method="POST"), timeout=10).read()


def _ntfy(title, text):
    u = urlparse(NTFY)   # publication JSON : conserve les accents du titre (un en-tête HTTP ne le permet pas)
    _post(f"{u.scheme}://{u.netloc}", json.dumps({"topic": u.path.strip("/"), "title": title, "message": text}).encode("utf-8"),
          {"Content-Type": "application/json"})


def notify(title, text):
    """Envoie une notification sur les canaux configurés, sans jamais bloquer ni échouer."""
    def run():
        sends = (
            ("ntfy", NTFY, lambda: _ntfy(title, text)),
            ("Discord", DISCORD, lambda: _post(DISCORD, json.dumps({"content": f"**{title}**\n{text}"}).encode(), {"Content-Type": "application/json"})),
            ("Telegram", TG_TOKEN and TG_CHAT, lambda: _post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                                             json.dumps({"chat_id": TG_CHAT, "text": f"{title}\n{text}"}).encode(), {"Content-Type": "application/json"})),
        )
        for name, on, fn in sends:
            if on:
                try:
                    fn()
                except Exception as e:
                    print(f"[webconsole] notification {name} échouée: {e!r}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def online_avatars():
    """Avatars en ligne d'après la table GridUser, en ignorant les sessions antérieures au démarrage d'OpenSim."""
    pid = find_pid()
    if not pid:
        return []
    start = time.time() - (proc_info(pid).get("uptime") or 0)
    names = {}
    c = open_table("UserAccounts")
    if c:
        try:
            names = {str(r["PrincipalID"]).lower(): (r["FirstName"], r["LastName"]) for r in c.execute("SELECT PrincipalID, FirstName, LastName FROM UserAccounts")}
        except sqlite3.Error:
            pass
        finally:
            c.close()
    regs = {str(r["uuid"]).lower(): r["name"] for r in regions() if r["uuid"]}
    out, c = [], open_table("GridUser")
    if not c:
        return []
    try:
        for r in c.execute("SELECT * FROM GridUser"):
            d = {k.lower(): r[k] for k in r.keys()}
            if str(d.get("online", "")).lower() != "true":
                continue
            try:
                login = int(d.get("login") or 0)
            except ValueError:
                login = 0
            if login < start - 120:
                continue
            parts = str(d.get("userid", "")).split(";")
            fl = names.get(parts[0].lower())
            hg = fl is None
            first, last = fl if fl else (parts[2] if len(parts) > 2 else parts[0][:8], "")
            out.append({"name": f"{first} {last}".strip(), "first": first, "last": last, "hg": hg,
                        "region": regs.get(str(d.get("lastregionid", "")).lower(), "?"), "since": login or None})
    except sqlite3.Error:
        return []
    finally:
        c.close()
    out.sort(key=lambda a: a["name"].lower())
    return out


def announce(text):
    try:
        send_cmd(f"alert {text}")
    except CmdError:
        pass


def schedule_restart(minutes, text, who):
    minutes = int(minutes)
    if not 1 <= minutes <= 120:
        raise ValueError("la durée doit être comprise entre 1 et 120 minutes")
    text = " ".join(str(text or "Redémarrage de la grille").split())[:120]
    with _rl:
        if RJOB["job"]:
            raise ValueError("un redémarrage est déjà programmé")
        RJOB["job"] = {"at": time.time() + minutes * 60, "text": text, "who": who, "sent": {x for x in THRESH if x >= minutes}}
    announce(f"{text} dans {minutes} minute{'s' if minutes > 1 else ''}")
    notify("Redémarrage programmé", f"{text} dans {minutes} min (par {who})")


def cancel_restart():
    with _rl:
        j, RJOB["job"] = RJOB["job"], None
    if j:
        announce("Redémarrage annulé")
    return bool(j)


def request_restart():
    """Demande à entrypoint.sh de relancer OpenSim après l'arrêt propre (fichier-drapeau + shutdown)."""
    open(FLAG, "w").close()
    try:
        send_cmd("shutdown")
    except CmdError:
        os.remove(FLAG)
        raise


def restart_info():
    j = RJOB["job"]
    return {"remaining": max(0, int(j["at"] - time.time())), "text": j["text"], "who": j["who"]} if j else None


def restart_tick(now=None):
    now = now or time.time()
    fire, say = False, None
    with _rl:
        j = RJOB["job"]
        if not j:
            return
        left = j["at"] - now
        if left <= 0:
            RJOB["job"], fire = None, True
        else:
            for m in sorted(THRESH):
                if left <= m * 60 and m not in j["sent"]:
                    j["sent"].update(x for x in THRESH if x >= m)
                    say = f"{j['text']} dans {m} minute{'s' if m > 1 else ''}"
                    break
    if fire:
        audit("planificateur", "redémarrage de la grille")
        try:
            request_restart()
        except CmdError as e:
            audit("planificateur", f"échec du redémarrage: {e}")
            notify("Redémarrage échoué", str(e))
    elif say:
        announce(say)


def daily_tick(now=None):
    if not DAILY_AT:
        return
    try:
        hh, mm = (int(x) for x in DAILY_AT.split(":"))
    except ValueError:
        return
    n = now or datetime.now()
    target = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target - timedelta(minutes=NOTICE_MIN) <= n < target and RJOB["daily_done"] != n.strftime("%Y%m%d") and not RJOB["job"]:
        RJOB["daily_done"] = n.strftime("%Y%m%d")
        schedule_restart(max(1, -(-int((target - n).total_seconds()) // 60)), "Redémarrage quotidien", "planificateur")


def ticker():
    i = 0
    while True:
        time.sleep(1)
        i += 1
        try:
            restart_tick()
            if i % 20 == 0:
                daily_tick()
        except Exception as e:
            print(f"[webconsole] planificateur: {e!r}", flush=True)


def watcher():
    """Alertes : démarrage, arrêt/relance d'OpenSim, disque presque plein, et (en option) connexions."""
    time.sleep(20)
    state, last_disk, known = "init", 0.0, None
    while True:
        try:
            pid = find_pid()
            if state == "init":
                if pid:
                    notify("Grille démarrée", f"{GRID_NAME} est en ligne.")
            elif state and not pid:
                notify("OpenSim arrêté", "Le processus OpenSim s'est arrêté (relance automatique si l'arrêt est inattendu).")
            elif not state and pid:
                notify("OpenSim relancé", f"{GRID_NAME} est de nouveau en ligne.")
            state = bool(pid)
            du = shutil.disk_usage(DATA)
            if (du.total - du.free) * 100 // du.total >= DISK_PCT and time.time() - last_disk > 6 * 3600:
                last_disk = time.time()
                notify("Disque presque plein", f"{(du.total - du.free) * 100 // du.total} % utilisés, {du.free // 2**30} Go libres.")
            if ALERT_LOGINS:
                now_on = {a["name"] for a in online_avatars()}
                if known is not None:
                    for n in sorted(now_on - known):
                        notify("Connexion", f"{n} est connecté.")
                    for n in sorted(known - now_on):
                        notify("Déconnexion", f"{n} s'est déconnecté.")
                known = now_on
        except Exception as e:
            print(f"[webconsole] surveillance: {e!r}", flush=True)
        time.sleep(15)


# ---------------------------------------------------------------- HTTP
SESSIONS, FAILS = {}, {}
_slock = threading.Lock()


def locked(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    return len(FAILS[ip]) >= 5


STATIC = {}
for _p, _ct in (("/", "index.html"), ("/app.css", "app.css"), ("/i18n.js", "i18n.js"), ("/app.js", "app.js")):
    try:
        with open(os.path.join(UI_DIR, _ct), "rb") as _f:
            STATIC[_p] = (_f.read(), {"html": "text/html", "css": "text/css", "js": "text/javascript"}[_ct.rsplit(".", 1)[1]] + "; charset=utf-8")
    except OSError:
        pass

ADMIN_ONLY_GET = ("/api/backups/download", "/api/audit", "/api/transfer", "/api/transfer/download", "/api/regions")


class Handler(BaseHTTPRequestHandler):
    server_version = "webconsole"

    def log_message(self, *a):
        pass

    # -- réponses
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, extra=()):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    # -- session
    def _session(self):
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            tok = c["wc"].value
        except (KeyError, http.cookies.CookieError):
            return None
        with _slock:
            s = SESSIONS.get(tok)
            if s and s["exp"] > time.time():
                return s
            SESSIONS.pop(tok, None)
        return None

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > 4000:
                raise ValueError
            d = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(d, dict):
                raise ValueError
            return d
        except Exception:
            return None

    def _csrf_ok(self):
        if self.headers.get("X-Requested-With") != "webconsole":
            return False
        o = self.headers.get("Origin")
        return not o or urlparse(o).netloc == self.headers.get("Host")

    # -- GET
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path in STATIC:
            body, ct = STATIC[path]
            return self._send(200, body, ct)
        if path == "/api/config":
            return self._json(200, {"totp": bool(TOTP_SECRET), "grid": GRID_NAME})
        s = self._session()
        if not s:
            return self._err(401, "non authentifié")
        admin = s["role"] == "admin"
        if path in ADMIN_ONLY_GET and not admin:
            return self._err(403, "réservé à l'administrateur")
        try:
            if path == "/api/me":
                return self._json(200, {"user": s["user"], "role": s["role"], "grid": GRID_NAME})
            if path == "/api/status":
                return self._json(200, status())
            if path == "/api/log":
                p = q.get("pos", [None])[0]
                return self._json(200, read_log(int(p) if p and p.lstrip("-").isdigit() else None))
            if path == "/api/users":
                return self._json(200, list_users(admin))
            if path == "/api/backups":
                return self._json(200, list_backups())
            if path == "/api/audit":
                return self._json(200, {"lines": tail_file(ACTIONS, 300)})
            if path == "/api/backups/download":
                return self._download(q.get("name", [""])[0], s)
            if path == "/api/regions":
                sz = q.get("size", ["256"])[0]
                return self._json(200, {"items": regions(), "suggest": region_suggest(int(sz) if sz.isdigit() else 256)})
            if path == "/api/transfer":
                return self._json(200, list_transfer(q.get("kind", [""])[0]))
            if path == "/api/transfer/download":
                return self._send_file(xfer_file(q.get("kind", [""])[0], q.get("name", [""])[0]), s)
        except ValueError as e:
            return self._err(400, str(e))
        except Exception as e:
            print(f"[webconsole] erreur interne GET {path}: {e!r}", flush=True)
            return self._err(500, "erreur interne")
        self._err(404, "introuvable")

    def _download(self, name, s):
        d = bdir(name)
        tmp = tempfile.TemporaryFile()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, fs in os.walk(d):
                for fn in fs:
                    p = os.path.join(root, fn)
                    z.write(p, os.path.relpath(p, d))
        size = tmp.tell()
        tmp.seek(0)
        audit(s["user"], f"téléchargement sauvegarde {name}")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}.zip"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        while True:
            chunk = tmp.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
        tmp.close()

    def _send_file(self, p, s):
        size = os.path.getsize(p)
        audit(s["user"], f"téléchargement {os.path.basename(p)}")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(p)}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _upload(self):
        s = self._session()
        if not s:
            return self._err(401, "non authentifié")
        if s["role"] != "admin":
            return self._err(403, "réservé à l'administrateur")
        q = parse_qs(urlparse(self.path).query)
        kind = q.get("kind", [""])[0]
        try:
            name = clean_upload_name(q.get("name", [""])[0], kind)
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError as e:
            self.close_connection = True
            return self._err(400, str(e))
        if n <= 0 or n > MAX_UPLOAD:
            self.close_connection = True
            return self._err(413, f"fichier trop gros (limite {MAX_UPLOAD // 1048576} Mo)")
        d = xfer_dir(kind)
        tmp = os.path.join(d, ".upload-" + secrets.token_hex(6))
        left = n
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                os.remove(tmp)
                self.close_connection = True
                return self._err(400, "envoi interrompu")
            final = os.path.join(d, name)
            if os.path.exists(final):
                name = time.strftime("%Y%m%d-%H%M%S-") + name
                final = os.path.join(d, name)
            os.replace(tmp, final)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._err(500, f"erreur d'écriture: {e}")
        audit(s["user"], f"import fichier {kind} {name} ({n} octets)")
        self._json(200, {"ok": True, "name": name})

    # -- POST
    def do_POST(self):
        path = urlparse(self.path).path
        if not self._csrf_ok():
            return self._err(403, "requête refusée")
        if path == "/api/transfer/upload":
            return self._upload()
        body = self._body()
        if body is None:
            return self._err(400, "requête invalide")
        if path == "/api/login":
            return self._login(body)
        s = self._session()
        if not s:
            return self._err(401, "non authentifié")
        if path == "/api/logout":
            with _slock:
                for t, v in list(SESSIONS.items()):
                    if v is s:
                        SESSIONS.pop(t, None)
            return self._json(200, {"ok": True}, extra=[("Set-Cookie", "wc=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")])
        if s["role"] != "admin":
            return self._err(403, "réservé à l'administrateur")
        who = s["user"]
        try:
            if path == "/api/cmd":
                cmd = body.get("cmd")
                if not isinstance(cmd, str):
                    return self._err(400, "commande invalide")
                send_cmd(cmd)
                audit(who, mask_cmd(cmd.strip()) or "(Entrée)")
                return self._json(200, {"ok": True})
            if path == "/api/users/create":
                f, l, p, e = (str(body.get(k, "")).strip() for k in ("first", "last", "pass", "email"))
                if not all(WORD.match(x) for x in (f, l, p, e)):
                    return self._err(400, "quatre champs requis, sans espaces")
                send_cmd(f"create user {f} {l} {p} {e}")
                audit(who, f"create user {f} {l} ***")
                return self._json(200, {"ok": True})
            if path == "/api/users/password":
                f, l, p = (str(body.get(k, "")).strip() for k in ("first", "last", "pass"))
                if not all(WORD.match(x) for x in (f, l, p)):
                    return self._err(400, "champs requis, sans espaces")
                send_cmd(f"reset user password {f} {l} {p}")
                audit(who, f"reset user password {f} {l} ***")
                return self._json(200, {"ok": True})
            if path == "/api/users/update":
                if "level" in body and type(body["level"]) is not int:
                    return self._err(400, "niveau invalide (valeurs autorisées : -1, 0, 200, 250)")
                update_user_account(body.get("id"), body.get("email"), body.get("level"))
                audit(who, f"modification du compte {body.get('id')} (niveau {body.get('level', 'inchangé')})")
                return self._json(200, {"ok": True})
            if path == "/api/transfer/delete":
                p = xfer_file(body.get("kind"), body.get("name"))
                os.remove(p)
                audit(who, f"suppression {os.path.basename(p)}")
                return self._json(200, {"ok": True})
            if path == "/api/oar/export":
                region = pick_region(body.get("region"))
                fn = f"{re.sub(r'[^A-Za-z0-9_-]', '_', region)}-{time.strftime('%Y%m%d-%H%M%S')}.oar"
                send_cmd("save oar" + (" --noassets" if body.get("noassets") else "") + f" {os.path.join(xfer_dir('oar'), fn)}")
                audit(who, f"export OAR {region} -> {fn}")
                return self._json(200, {"ok": True, "name": fn})
            if path == "/api/oar/load":
                p = xfer_file("oar", body.get("name"))
                region = pick_region(body.get("region"))
                send_cmd("load oar" + (" -m" if body.get("merge") else "") + (" -s" if body.get("skip_assets") else "") + f" {p}")
                audit(who, f"import OAR {os.path.basename(p)} -> {region}")
                return self._json(200, {"ok": True})
            if path in ("/api/iar/export", "/api/iar/load"):
                f, l, pw = (str(body.get(k, "")).strip() for k in ("first", "last", "pass"))
                if not all(WORD.match(x) for x in (f, l, pw)):
                    return self._err(400, "avatar et mot de passe requis (sans espaces)")
                inv = inv_arg(body.get("inventory"))
                if path == "/api/iar/export":
                    fn = f"{re.sub(r'[^A-Za-z0-9_-]', '_', f + '_' + l)}-{time.strftime('%Y%m%d-%H%M%S')}.iar"
                    send_cmd("save iar" + (" --noassets" if body.get("noassets") else "") + f" {f} {l} {inv} {pw} {os.path.join(xfer_dir('iar'), fn)}")
                    audit(who, f"export IAR {f} {l} {inv} -> {fn}")
                    return self._json(200, {"ok": True, "name": fn})
                p = xfer_file("iar", body.get("name"))
                send_cmd("load iar" + (" -m" if body.get("merge") else "") + f" {f} {l} {inv} {pw} {p}")
                audit(who, f"import IAR {os.path.basename(p)} -> {f} {l} {inv}")
                return self._json(200, {"ok": True})
            if path == "/api/regions/create":
                r = region_create(body)
                audit(who, f"région créée {r['name']} ({r['location']}, port {r['port']})")
                return self._json(200, {"ok": True, **r})
            if path == "/api/regions/change":
                region = pick_region(body.get("region"))
                audit(who, f"change region {region}")
                return self._json(200, {"ok": True, "region": region})
            if path == "/api/regions/delete":
                region_delete(str(body.get("name", "")), bool(body.get("export")))
                audit(who, f"région retirée {body.get('name')}" + (" (export OAR demandé)" if body.get("export") else ""))
                return self._json(200, {"ok": True})
            if path == "/api/console/restart":
                audit(who, "redémarrage de la console web")
                threading.Timer(0.5, lambda: os._exit(42)).start()   # 42 = relance demandée (voir entrypoint.sh)
                return self._json(200, {"ok": True})
            if path == "/api/users/disable":
                f, l = (str(body.get(k, "")).strip() for k in ("first", "last"))
                if not (WORD.match(f) and WORD.match(l)):
                    return self._err(400, "avatar invalide")
                dis = bool(body.get("disabled"))
                send_cmd(f"set user level {f} {l} {-1 if dis else 0}")
                if dis:
                    try:
                        send_cmd(f"kick user {f} {l} Compte désactivé")
                    except CmdError:
                        pass
                audit(who, f"compte {'désactivé' if dis else 'réactivé'} {f} {l}")
                return self._json(200, {"ok": True})
            if path == "/api/users/delete":
                f, l = (str(body.get(k, "")).strip() for k in ("first", "last"))
                if not (WORD.match(f) and WORD.match(l)):
                    return self._err(400, "avatar invalide")
                r = delete_user(f, l, str(body.get("confirm", "")))
                audit(who, f"suppression définitive du compte {f} {l} {r['removed']} (sauvegarde {r['backup']})")
                return self._json(200, {"ok": True, **r})
            if path == "/api/grid/restart":
                request_restart()
                audit(who, "redémarrage immédiat de la grille")
                return self._json(200, {"ok": True})
            if path == "/api/restart/schedule":
                schedule_restart(body.get("minutes") or 0, body.get("text"), who)
                audit(who, f"redémarrage programmé dans {body.get('minutes')} min")
                return self._json(200, {"ok": True})
            if path == "/api/restart/cancel":
                audit(who, "redémarrage annulé")
                return self._json(200, {"ok": cancel_restart()})
            if path in ("/api/avatars/message", "/api/avatars/kick"):
                f, l = (str(body.get(k, "")).strip() for k in ("first", "last"))
                t = " ".join(str(body.get("text", "")).split())[:200]
                if not (WORD.match(f) and WORD.match(l)):
                    return self._err(400, "avatar invalide")
                if path.endswith("message"):
                    if not t:
                        return self._err(400, "message vide")
                    send_cmd(f"alert-user {f} {l} {t}")
                else:
                    send_cmd(f"kick user {f} {l} {t}".strip())
                audit(who, f"{path.rsplit('/', 1)[1]} {f} {l}")
                return self._json(200, {"ok": True})
            if path == "/api/alerts/test":
                if not channels():
                    return self._err(400, "aucun canal d'alerte configuré (voir le .env)")
                notify("Test", f"Notification de test envoyée par {who}.")
                return self._json(200, {"ok": True})
            if path == "/api/backups/create":
                name = make_backup(str(body.get("kind", "db")), "")
                audit(who, f"sauvegarde {body.get('kind', 'db')} {name}")
                return self._json(200, {"ok": True, "name": name})
            if path == "/api/backups/delete":
                d = bdir(body.get("name"))
                shutil.rmtree(d)
                audit(who, f"suppression sauvegarde {os.path.basename(d)}")
                return self._json(200, {"ok": True})
            if path == "/api/backups/restore":
                d = bdir(body.get("name"))
                if not os.path.isdir(os.path.join(d, "db")):
                    return self._err(400, "cette sauvegarde ne contient pas de bases")
                with open(os.path.join(DATA, "restore.pending"), "w", encoding="utf-8") as f:
                    f.write(os.path.basename(d))
                audit(who, f"restauration programmée {os.path.basename(d)}")
                return self._json(200, {"ok": True, "message": "Restauration programmée : elle s'appliquera au prochain démarrage de la grille."})
            if path == "/api/backups/loadoar":
                d = bdir(body.get("name"))
                oars = sorted(f for f in os.listdir(d) if f.endswith(".oar"))
                if not oars:
                    return self._err(400, "aucun OAR dans cette sauvegarde")
                manifest_path = os.path.join(d, "manifest.json")
                if os.path.isfile(manifest_path):
                    with open(manifest_path, encoding="utf-8") as f:
                        manifest = json.load(f)
                else:
                    manifest = {}
                oar_regions = {
                    item["file"]: item["region"]
                    for item in manifest.get("oar_regions", [])
                    if isinstance(item, dict) and isinstance(item.get("file"), str)
                    and isinstance(item.get("region"), str)
                }
                for filename in oars:
                    region = oar_regions.get(filename, os.path.splitext(filename)[0])
                    pick_region(region)
                    send_cmd(f"load oar {qarg(os.path.join(d, filename))}")
                    audit(who, f"load oar {os.path.basename(d)}/{filename} -> {region}")
                return self._json(200, {"ok": True})
        except CmdError as e:
            return self._err(e.code, str(e))
        except ValueError as e:
            return self._err(400, str(e))
        except (OSError, sqlite3.Error) as e:
            return self._err(500, f"erreur: {e}")
        except Exception as e:
            print(f"[webconsole] erreur interne POST {path}: {e!r}", flush=True)
            return self._err(500, "erreur interne")
        self._err(404, "introuvable")

    def _login(self, body):
        ip = self.client_address[0]
        if locked(ip):
            return self._err(429, "trop de tentatives, réessayer dans quelques minutes")
        u, p, code = str(body.get("user", "")), str(body.get("pass", "")), str(body.get("code", ""))
        role = None
        if ct_eq(u, USER) & ct_eq(p, PASSWORD):
            role = "admin"
        elif VIEWER_USER and len(VIEWER_PASS) >= 8 and ct_eq(u, VIEWER_USER) & ct_eq(p, VIEWER_PASS):
            role = "viewer"
        if role and TOTP_SECRET and not totp_ok(TOTP_SECRET, code):
            role = None
        if not role:
            FAILS.setdefault(ip, []).append(time.time())
            audit(ip, f"échec de connexion ({u[:30]!r})")
            time.sleep(1)
            return self._err(401, "identifiants incorrects")
        tok = secrets.token_urlsafe(32)
        with _slock:
            now = time.time()
            for t in [t for t, v in SESSIONS.items() if v["exp"] < now]:
                SESSIONS.pop(t, None)
            SESSIONS[tok] = {"user": u, "role": role, "exp": now + SESSION_TTL}
        FAILS.pop(ip, None)
        audit(ip, f"connexion {u} ({role})")
        self._json(200, {"ok": True, "role": role},
                   extra=[("Set-Cookie", f"wc={tok}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Strict")])


if __name__ == "__main__":
    try:
        os.makedirs(os.path.dirname(ACTIONS), exist_ok=True)
        os.makedirs(BACKUPS, exist_ok=True)
    except OSError:
        pass
    if "/" not in STATIC:
        print(f"[webconsole] interface introuvable dans {UI_DIR}", flush=True)
        sys.exit(0)
    try:
        srv = ThreadingHTTPServer((BIND, PORT), Handler)
    except OSError as e:
        print(f"[webconsole] impossible d'écouter sur {BIND}:{PORT} ({e}) : vérifier HOST_LAN_IP et que le port est libre", flush=True)
        sys.exit(0)
    srv.daemon_threads = True
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=ticker, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    print(f"[webconsole] v2 : écoute sur {BIND}:{PORT} (utilisateur {USER}"
          f"{', TOTP' if TOTP_SECRET else ''}{', lecteur ' + VIEWER_USER if VIEWER_USER else ''})", flush=True)
    srv.serve_forever()
