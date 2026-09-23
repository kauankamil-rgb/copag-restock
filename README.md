# Monitor de estoque — B2B Copag (Pokémon)

Avisa no Telegram quando um produto da categoria [Pokémon](https://www.b2b.copagloja.com.br/pokemon)
sai de **esgotado** para **disponível**, ou quando um produto novo entra no catálogo já com estoque.

Não usa scraping de HTML nem login: a loja roda em VTEX e expõe o estoque real
pela API pública de Intelligent Search.

## Como funciona

1. A cada 5 minutos o GitHub Actions consulta a API da categoria.
2. Compara o resultado com o snapshot anterior (`state.json`, versionado no repo).
3. Se um SKU passou de `qty 0` → `qty > 0`, dispara uma mensagem no Telegram com nome, preço e link direto.
4. Grava o novo snapshot. Commit só acontece quando o estoque muda de fato.

A primeira execução apenas grava o estado inicial, sem alertar.

## Configuração do Telegram (uma vez, ~2 min)

1. No Telegram, fale com [@BotFather](https://t.me/BotFather) → `/newbot` → escolha nome e username.
   Ele devolve um token no formato `1234567890:AAH...`.
2. Mande qualquer mensagem (`oi`) para o bot que você acabou de criar — sem isso ele não pode te responder.
3. Pegue seu chat id:
   ```bash
   curl -s "https://api.telegram.org/bot<SEU_TOKEN>/getUpdates" | grep -o '"id":[0-9-]*' | head -1
   ```
4. Cadastre os dois valores como secrets do repositório:
   ```bash
   gh secret set TELEGRAM_BOT_TOKEN --body "<SEU_TOKEN>"
   gh secret set TELEGRAM_CHAT_ID  --body "<SEU_CHAT_ID>"
   ```
5. Teste o canal:
   ```bash
   TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 monitor.py --test-alert
   ```

## Uso local

```bash
python3 monitor.py --list      # mostra o estoque atual de todos os SKUs
python3 monitor.py --dry-run   # roda a comparação sem alertar nem gravar
python3 monitor.py             # execução normal
```

Sem dependências: só Python 3 da própria máquina.

## Monitorar mais lojas

Cada loja vigiada é um alvo em `targets.json`. Adicione um objeto e pronto — o `state.json`
guarda os alvos separadamente e um alvo que falha não derruba nem apaga os outros.

```json
[
  { "id": "copag-pokemon", "label": "Copag B2B — Pokémon",
    "adapter": "vtex", "store": "https://www.b2b.copagloja.com.br", "category": "pokemon" },

  { "id": "copag-baralhos", "label": "Copag B2B — Baralhos",
    "adapter": "vtex", "store": "https://www.b2b.copagloja.com.br", "category": "baralhos" },

  { "id": "loja-x", "label": "Loja X — Cartas",
    "adapter": "shopify", "store": "https://loja-x.com", "collection": "pokemon", "currency": "US$" }
]
```

Campos: `id` (chave do estado, não mude depois), `label` (aparece no alerta), `adapter`,
`store`, e `category` (VTEX) ou `collection` (Shopify, opcional — sem ela varre a loja toda).
`currency` é o símbolo exibido no preço (padrão `R$`) e `enabled: false` desliga um alvo sem removê-lo.

### Adapters disponíveis

- **`vtex`** — qualquer loja VTEX, via API pública de Intelligent Search. Traz a quantidade real.
- **`shopify`** — qualquer loja Shopify, via `products.json`. O Shopify só expõe o booleano
  `available`, então a quantidade aparece como 1/0 — suficiente para detectar a virada.

Loja em outra plataforma precisa de um adapter novo: uma função que recebe o alvo e devolve
`{sku: {name, qty, price, url, cur}}`. O resto do fluxo não muda.

## Limitações conhecidas

- O cron do GitHub Actions é **best effort**: em horário de pico a execução pode atrasar
  alguns minutos. Na prática o alerta chega em 5–15 min após o restock.
- A loja reporta `10000` como quantidade para itens em estoque — é um teto do VTEX,
  não o estoque literal. O que importa aqui é `0` vs `> 0`.
- O estoque é o da vitrine pública. Preço e disponibilidade finais podem variar conforme
  a política comercial da sua conta B2B depois do login.
