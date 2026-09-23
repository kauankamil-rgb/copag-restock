#!/usr/bin/env python3
"""Monitor de restock multi-loja com alerta no Telegram.

Cada loja vigiada e um "alvo" descrito em targets.json, resolvido por um
adapter conforme a plataforma. O snapshot de todos os alvos fica em
state.json, e o alerta dispara quando um SKU sai de zerado para disponivel.
Um alvo que falha nao contamina os outros nem perde seu estado anterior.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TARGETS_FILE = os.path.join(HERE, "targets.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
MAX_ALERTS_POR_ALVO = 15


def get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


# --------------------------------------------------------------------------
# Adapters: recebem o alvo e devolvem {sku: {name, qty, price, url}}
# --------------------------------------------------------------------------

def adapter_vtex(t):
    """Lojas VTEX. Usa a API publica de Intelligent Search.

    O par query=<slug>&map=c e o que filtra por categoria de verdade; sem ele
    a API faz busca textual fuzzy e devolve o catalogo inteiro.
    """
    store = t["store"].rstrip("/")
    cat = t["category"]
    page_size = 50
    out, page = {}, 1
    while True:
        qs = urllib.parse.urlencode({"query": cat, "map": "c", "count": page_size, "page": page})
        data = get_json("%s/api/io/_v/api/intelligent-search/product_search/%s?%s" % (store, cat, qs))
        products = data.get("products", [])
        for prod in products:
            for item in prod.get("items", []):
                offers = [s.get("commertialOffer", {}) for s in item.get("sellers", [])]
                qty = max([o.get("AvailableQuantity", 0) for o in offers] or [0])
                price = next((o.get("Price") for o in offers if o.get("Price")), None)
                out[item["itemId"]] = {
                    "name": item.get("nameComplete") or prod["productName"],
                    "qty": qty,
                    "price": price,
                    "url": store + prod.get("link", ""),
                    "cur": t.get("currency", "R$"),
                }
        if not products or page * page_size >= data.get("recordsFiltered", 0):
            break
        page += 1
    return out


def adapter_shopify(t):
    """Lojas Shopify. Usa products.json, publico em praticamente toda loja.

    O Shopify nao expoe quantidade, so o booleano `available` por variante,
    entao qty vira 1 ou 0 - suficiente para detectar a virada.
    """
    store = t["store"].rstrip("/")
    handle = t.get("collection")
    base = "%s/collections/%s/products.json" % (store, handle) if handle else "%s/products.json" % store
    out, page = {}, 1
    while page <= 20:
        data = get_json("%s?limit=250&page=%d" % (base, page))
        products = data.get("products", [])
        if not products:
            break
        for prod in products:
            for var in prod.get("variants", []):
                nome = prod["title"]
                if var.get("title") and var["title"].lower() != "default title":
                    nome = "%s - %s" % (nome, var["title"])
                preco = var.get("price")
                try:
                    preco = float(preco)
                except (TypeError, ValueError):
                    preco = None
                out[str(var["id"])] = {
                    "name": nome,
                    "qty": 1 if var.get("available") else 0,
                    "price": preco,
                    "url": "%s/products/%s" % (store, prod.get("handle", "")),
                    "cur": t.get("currency", "R$"),
                }
        page += 1
    return out


ADAPTERS = {"vtex": adapter_vtex, "shopify": adapter_shopify}


# --------------------------------------------------------------------------

def load_targets():
    with open(TARGETS_FILE, encoding="utf-8") as fh:
        alvos = json.load(fh)
    for t in alvos:
        if t.get("adapter") not in ADAPTERS:
            raise SystemExit("alvo '%s': adapter '%s' desconhecido (use: %s)"
                             % (t.get("id"), t.get("adapter"), ", ".join(ADAPTERS)))
    return [t for t in alvos if t.get("enabled", True)]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            s = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Migra o formato antigo (alvo unico) para o multi-alvo, sem gerar alerta falso.
    if "skus" in s and "targets" not in s:
        return {"copag-pokemon": s["skus"]}
    return s.get("targets", {})


def save_state(estados):
    payload = {"targets": {k: dict(sorted(v.items())) for k, v in sorted(estados.items())}}
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[aviso] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes - alerta so no log", file=sys.stderr)
        return False
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "false"}
    ).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok", False)
    except urllib.error.HTTPError as err:
        print("[erro] Telegram %s: %s" % (err.code, err.read().decode("utf-8", "replace")), file=sys.stderr)
        return False


def money(value, cur="R$"):
    if value is None:
        return "-"
    txt = "%s %.2f" % (cur, value)
    return txt.replace(".", ",") if cur == "R$" else txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="nao envia Telegram nem grava state.json")
    ap.add_argument("--list", action="store_true", help="imprime o estoque atual e sai")
    ap.add_argument("--test-alert", action="store_true", help="envia uma mensagem de teste no Telegram")
    ap.add_argument("--target", help="roda apenas o alvo com este id")
    args = ap.parse_args()

    if args.test_alert:
        ok = send_telegram("✅ <b>Monitor de restock</b> conectado.")
        print("teste enviado" if ok else "falha no envio")
        return 0 if ok else 1

    alvos = load_targets()
    if args.target:
        alvos = [t for t in alvos if t["id"] == args.target]
        if not alvos:
            raise SystemExit("alvo '%s' nao encontrado em targets.json" % args.target)

    estados = load_state()
    falhas = 0

    for t in alvos:
        label = t.get("label", t["id"])
        try:
            atual = ADAPTERS[t["adapter"]](t)
        except Exception as err:  # noqa: BLE001 - um alvo quebrado nao derruba os outros
            print("[erro] %s: %s" % (label, err), file=sys.stderr)
            falhas += 1
            continue

        if not atual:
            print("[erro] %s: catalogo vazio - estado preservado" % label, file=sys.stderr)
            falhas += 1
            continue

        if args.list:
            print("\n## %s (%d SKUs)" % (label, len(atual)))
            for sku, d in sorted(atual.items(), key=lambda kv: kv[1]["name"]):
                print("%s %6d  %11s  %s" % ("OK " if d["qty"] else "OFF", d["qty"], money(d["price"], d["cur"]), d["name"][:60]))
            continue

        prev = estados.get(t["id"], {})
        first_run = not prev

        restock = [(s, d) for s, d in atual.items() if d["qty"] > 0 and s in prev and prev[s].get("qty", 0) <= 0]
        novos = [(s, d) for s, d in atual.items() if d["qty"] > 0 and s not in prev]
        zerou = [s for s, d in atual.items() if d["qty"] <= 0 and prev.get(s, {}).get("qty", 0) > 0]

        print("%s | skus=%d disponiveis=%d restock=%d novos=%d zerou=%d"
              % (label, len(atual), sum(1 for d in atual.values() if d["qty"] > 0), len(restock), len(novos), len(zerou)))

        if first_run:
            print("  primeira execucao deste alvo: estado gravado, sem alertas")
        else:
            for tag, grupo in (("🔥 VOLTOU AO ESTOQUE", restock), ("🆕 PRODUTO NOVO DISPONÍVEL", novos)):
                for sku, d in grupo[:MAX_ALERTS_POR_ALVO]:
                    qtd = "%s un." % d["qty"] if d["qty"] > 1 else "sim"
                    msg = ("%s\n\n<b>%s</b>\n<i>%s</i>\n\nPreço: <b>%s</b>\nDisponível: %s\n\n"
                           '<a href="%s">Abrir na loja</a>' % (tag, d["name"], label, money(d["price"], d["cur"]), qtd, d["url"]))
                    print("  ALERTA: %s | %s" % (tag, d["name"]))
                    if not args.dry_run:
                        send_telegram(msg)

        estados[t["id"]] = {s: {"name": d["name"], "qty": d["qty"], "price": d["price"]} for s, d in atual.items()}

    if not args.list and not args.dry_run:
        save_state(estados)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
