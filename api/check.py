"""Endpoint invocado pelo cron da Vercel a cada minuto.

Reusa o mesmo monitor.py do CLI e do GitHub Actions. Na Vercel a funcao roda
sem disco, entao o snapshot precisa vir do Redis: sem as variaveis do Upstash
a funcao falha de cara, em vez de tentar gravar num filesystem read-only.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        segredo = os.environ.get("CRON_SECRET")
        if segredo and self.headers.get("authorization") != "Bearer " + segredo:
            self._responde(401, {"ok": False, "erro": "nao autorizado"})
            return

        if not monitor.usando_redis():
            self._responde(500, {
                "ok": False,
                "erro": "defina UPSTASH_REDIS_REST_URL e UPSTASH_REDIS_REST_TOKEN no projeto",
            })
            return

        try:
            log, falhas = monitor.run(monitor.load_targets())
            print("\n".join(log))  # aparece nos runtime logs da Vercel
            self._responde(500 if falhas else 200, {"ok": not falhas, "log": log})
        except Exception:
            erro = traceback.format_exc(limit=4)
            print(erro, file=sys.stderr)
            self._responde(500, {"ok": False, "erro": erro})

    def _responde(self, code, body):
        blob = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)
