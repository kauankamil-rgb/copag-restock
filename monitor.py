#!/usr/bin/env python3
"""Monitor de estoque da B2B Copag (VTEX) com alerta no Telegram.

Consulta a API publica de Intelligent Search da loja, compara com o snapshot
anterior gravado em state.json e avisa quando um SKU sai de zerado para
disponivel. Primeira execucao apenas grava o estado, sem disparar alertas.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STORE = "https://www.b2b.copagloja.com.br"
SEARCH = STORE + "/api/io/_v/api/intelligent-search/product_search"
CATEGORY = os.environ.get("COPAG_CATEGORY", "pokemon")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
PAGE_SIZE = 50
MAX_ALERTS = 15


def get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as err:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def fetch_catalog():
    """Retorna {sku_id: {...}} para todos os produtos da categoria."""
    skus, page = {}, 1
    while True:
        qs = urllib.parse.urlencode(
            {"query": CATEGORY, "map": "c", "count": PAGE_SIZE, "page": page}
        )
        data = get_json("%s/%s?%s" % (SEARCH, CATEGORY, qs))
        products = data.get("products", [])
        for prod in products:
            for item in prod.get("items", []):
                offers = [s.get("commertialOffer", {}) for s in item.get("sellers", [])]
                qty = max([o.get("AvailableQuantity", 0) for o in offers] or [0])
                price = next((o.get("Price") for o in offers if o.get("Price")), None)
                skus[item["itemId"]] = {
                    "name": item.get("nameComplete") or prod["productName"],
                    "qty": qty,
                    "price": price,
                    "url": STORE + prod.get("link", ""),
                }
        total = data.get("recordsFiltered", 0)
        if page * PAGE_SIZE >= total or not products:
            break
        page += 1
    return skus


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(skus):
    # Sem timestamp de propósito: assim o arquivo só muda quando o estoque muda,
    # e o workflow só cria commit em evento real (a data fica no histórico do git).
    payload = {
        "category": CATEGORY,
        "skus": {sku: {"name": d["name"], "qty": d["qty"], "price": d["price"]} for sku, d in sorted(skus.items())},
    }
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


def money(value):
    if value is None:
        return "-"
    return ("R$ %.2f" % value).replace(".", ",")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="nao envia Telegram nem grava state.json")
    ap.add_argument("--list", action="store_true", help="imprime o estoque atual e sai")
    ap.add_argument("--test-alert", action="store_true", help="envia uma mensagem de teste no Telegram")
    args = ap.parse_args()

    if args.test_alert:
        ok = send_telegram("✅ <b>Monitor Copag B2B</b> conectado. Categoria: <code>%s</code>" % CATEGORY)
        print("teste enviado" if ok else "falha no envio")
        return 0 if ok else 1

    skus = fetch_catalog()
    if not skus:
        print("[erro] catalogo vazio - abortando sem gravar estado", file=sys.stderr)
        return 1

    if args.list:
        for sku, d in sorted(skus.items(), key=lambda kv: kv[1]["name"]):
            print("%s %6d  %9s  %s  [sku %s]" % ("OK " if d["qty"] else "OFF", d["qty"], money(d["price"]), d["name"][:60], sku))
        return 0

    state = load_state()
    prev = state.get("skus", {})
    first_run = not prev

    restocked, novos = [], []
    for sku, d in skus.items():
        if d["qty"] <= 0:
            continue
        if sku not in prev:
            novos.append((sku, d))
        elif prev[sku].get("qty", 0) <= 0:
            restocked.append((sku, d))

    sumiu = [sku for sku in prev if sku not in skus]
    zerou = [sku for sku, d in skus.items() if d["qty"] <= 0 and prev.get(sku, {}).get("qty", 0) > 0]

    disponiveis = sum(1 for d in skus.values() if d["qty"] > 0)
    print("categoria=%s skus=%d disponiveis=%d restock=%d novos=%d zerou=%d sumiu=%d"
          % (CATEGORY, len(skus), disponiveis, len(restocked), len(novos), len(zerou), len(sumiu)))
    for sku in zerou:
        print("  - zerou: %s [sku %s]" % (skus[sku]["name"], sku))

    if first_run:
        print("primeira execucao: estado inicial gravado, sem alertas")
    else:
        for tag, grupo in (("🔥 VOLTOU AO ESTOQUE", restocked), ("🆕 PRODUTO NOVO DISPONÍVEL", novos)):
            for sku, d in grupo[:MAX_ALERTS]:
                msg = (
                    "%s\n\n<b>%s</b>\n"
                    "Preço: <b>%s</b>\n"
                    "Disponível: %s un.\n\n"
                    '<a href="%s">Abrir no B2B Copag</a>'
                    % (tag, d["name"], money(d["price"]), d["qty"], d["url"])
                )
                print("ALERTA: %s | %s" % (tag, d["name"]))
                if not args.dry_run:
                    send_telegram(msg)

    if not args.dry_run:
        save_state(skus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
