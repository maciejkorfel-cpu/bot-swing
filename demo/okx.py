"""Klient OKX — WYŁĄCZNIE konto DEMO.

Twarda blokada (DemoGuard):
 1. zmienna OKX_DEMO musi mieć wartość "1" — inaczej DemoGuardError przy tworzeniu klienta (zanim padnie jakiekolwiek
    zapytanie z kluczem);
 2. KAŻDE zapytanie prywatne przechodzi przez _req(), które zawsze dokleja nagłówek x-simulated-trading: 1 i sprawdza go
    tuż przed wysłaniem (nie da się go wyłączyć parametrem);
 3. verify_demo_key(): jedno zapytanie tylko do odczytu (GET /account/config) BEZ nagłówka demo — klucz demo MUSI zostać
    odrzucony przez OKX (kod 50101 „APIKey does not match current environment”). Jeśli przejdzie, to klucz konta
    REALNEGO → DemoGuardError, bot kończy pracę bez żadnego zlecenia;
 4. ścieżki składające zlecenia są dozwolone dopiero po verify_demo_key() (flaga self.verified).
Klucze tylko ze zmiennych środowiskowych (GitHub Secrets). Nic nie jest logowane poza kodem błędu.
"""
import base64, hashlib, hmac, json, os, time
from datetime import datetime, timezone

DEMO_HEADER = {"x-simulated-trading": "1"}
WRITE_PREFIXES = ("/api/v5/trade/", "/api/v5/account/set-", "/api/v5/asset/transfer")


class DemoGuardError(RuntimeError):
    pass


class OkxError(RuntimeError):
    def __init__(self, path, code, msg, detail=""):
        super().__init__(f"OKX {path} {code}: {msg} {detail}".strip())
        self.code, self.detail = str(code), detail


def require_demo_env():
    if os.getenv("OKX_DEMO", "") != "1":
        raise DemoGuardError("OKX_DEMO != 1 — bot działa tylko na koncie DEMO. Koniec pracy, żadnych zleceń.")


class OkxDemo:
    def __init__(self, session, host, key, secret, passphrase):
        require_demo_env()
        if not (key and secret and passphrase):
            raise DemoGuardError("brak kluczy API demo w zmiennych środowiskowych")
        self.s, self.host = session, host
        self._k, self._s, self._p = key, secret, passphrase
        self.verified = False

    # ------------------------------------------------------------------ podpis
    def _headers(self, method, path, data, demo=True):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        sign = base64.b64encode(hmac.new(self._s.encode(), (ts + method + path + data).encode(), hashlib.sha256).digest()).decode()
        h = {"OK-ACCESS-KEY": self._k, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self._p,
             "Content-Type": "application/json"}
        if demo:
            h.update(DEMO_HEADER)
        return h

    def _req(self, method, path, params=None, body=None):
        require_demo_env()
        if path.startswith(WRITE_PREFIXES) and not self.verified:
            raise DemoGuardError("próba zlecenia przed weryfikacją klucza demo — zablokowano")
        if params:
            path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        data = json.dumps(body) if body is not None else ""
        h = self._headers(method, path, data, demo=True)
        if h.get("x-simulated-trading") != "1":            # ostatnia linia obrony
            raise DemoGuardError("brak nagłówka demo — zablokowano")
        for attempt in range(3):
            try:
                r = self.s.request(method, self.host + path, headers=h, data=data or None, timeout=15)
                if r.status_code == 429:
                    time.sleep(1 + attempt); continue
                js = r.json()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise OkxError(path.split("?")[0], "NET", str(e)[:200])
                time.sleep(1 + attempt)
        if js.get("code") != "0":
            d = js.get("data") or [{}]
            det = (d[0] or {}).get("sMsg", "") if isinstance(d, list) and d else ""
            raise OkxError(path.split("?")[0], js.get("code"), js.get("msg"), det)
        return js["data"]

    def verify_demo_key(self):
        """Klucz musi być kluczem DEMO: zapytanie tylko-do-odczytu bez nagłówka demo ma zostać odrzucone (50101)."""
        path = "/api/v5/account/config"
        r = self.s.get(self.host + path, headers=self._headers("GET", path, "", demo=False), timeout=15)
        try:
            code = str(r.json().get("code"))
        except Exception:  # noqa: BLE001
            code = f"HTTP{r.status_code}"
        if code == "0":
            raise DemoGuardError("KLUCZ API DZIAŁA NA KONCIE REALNYM — bot się zatrzymuje, żadnych zleceń. "
                                 "Utwórz klucz w trybie Demo trading.")
        if code not in ("50101", "50119"):
            # 50101 = klucz z innego środowiska (oczekiwane dla demo); inny błąd = nie da się potwierdzić → stop
            raise DemoGuardError(f"nie można potwierdzić, że klucz jest kluczem demo (kod {code}) — stop")
        cfg = self._req("GET", "/api/v5/account/config")[0]   # z nagłówkiem demo musi działać
        self.verified = True
        return cfg

    def get(self, path, **params):
        return self._req("GET", path, params=params or None)

    def post(self, path, body):
        return self._req("POST", path, body=body)


# ---------------------------------------------------------------------- publiczne dane (bez kluczy)
OKX_HOSTS = ["https://www.okx.com", "https://eea.okx.com", "https://my.okx.com"]


def public_get(session, path, params=None, demo=False, hosts=None):
    last = None
    for host in hosts or OKX_HOSTS:
        for attempt in range(3):
            try:
                r = session.get(host + path, params=params, headers=DEMO_HEADER if demo else {}, timeout=15)
                if r.status_code == 429:
                    time.sleep(1 + attempt); continue
                js = r.json()
                if js.get("code") != "0":
                    raise RuntimeError(f"OKX {js.get('code')}: {js.get('msg')}")
                return js["data"]
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.5)
    raise RuntimeError(f"brak dostępu do API OKX {path}: {last}")


def candles(session, inst, bar, limit=1500, demo=False):
    """Zamknięte świece rosnąco [ts, o, h, l, c] z history-candles (stronicowanie)."""
    out, after = [], None
    while len(out) < limit:
        p = {"instId": inst, "bar": bar, "limit": "100"}
        if after:
            p["after"] = after
        d = public_get(session, "/api/v5/market/history-candles", p, demo=demo)
        if not d:
            break
        out += d
        after = d[-1][0]
        time.sleep(0.11)
    rows = {int(c[0]): [int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])] for c in out if c[8] == "1"}
    return [rows[k] for k in sorted(rows)][-limit:]


def funding_history(session, inst, since_ms=0, demo=False):
    out, after = [], None
    for _ in range(40):
        p = {"instId": inst, "limit": "100"}
        if after:
            p["after"] = after
        d = public_get(session, "/api/v5/public/funding-rate-history", p, demo=demo)
        if not d:
            break
        out += d
        after = d[-1]["fundingTime"]
        if int(after) < since_ms:
            break
        time.sleep(0.11)
    return sorted({int(x["fundingTime"]): float(x["realizedRate"] or x["fundingRate"]) for x in out}.items())
