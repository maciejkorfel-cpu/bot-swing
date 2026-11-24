"""Telegram (token i chat_id tylko ze zmiennych środowiskowych / GitHub Secrets). Błąd Telegrama nie przerywa bota."""
import os, time

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
SENT = []          # kopia wysłanych wiadomości (testy / dry-run)


def send(session, text, dry=False):
    SENT.append(text)
    if dry or not (TG_TOKEN and TG_CHAT):
        print("[TELEGRAM]", text.replace("\n", " | "))
        return False
    for _ in range(3):
        try:
            r = session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                             json={"chat_id": TG_CHAT, "text": text[:3900]}, timeout=15)
            if r.ok:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    print("Telegram: nie udało się wysłać")
    return False
