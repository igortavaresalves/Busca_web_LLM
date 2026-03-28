"""
Busca IA — FastAPI + Groq (LLaMA 3.3) + Tavily Search
-----------------------------------------------------------------
v6: whitelist de domínios confiáveis + fallback automático para busca aberta
"""

import asyncio
import json
from urllib.parse import urlparse

import httpx
from tavily import TavilyClient
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

# ── Configuração ──────────────────────────────────────────────────────────────

# Groq: API Keys
GROQ_API_KEY = "SUA_CHAVE_GROQ"  # Substitua pela sua chave da Groq
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

# Tavily: API Keys (1.000 buscas/mês grátis)
TAVILY_API_KEY = "SUA_CHAVE_TAVILY"  # Substitua pela sua chave da Tavily

# ── Whitelist de domínios confiáveis ──────────────────────────────────────────
# Quando USE_WHITELIST=True, Tavily só retorna resultados destes sites.
# Pode ser alterado em runtime via /toggle_whitelist.
TRUSTED_DOMAINS: list[str] = [
    # Notícias BR
    "globo.com",
    "g1.globo.com",
    "ge.globo.com",
    "oglobo.globo.com",
    "folha.uol.com.br",
    "estadao.com.br",
    "uol.com.br",
    "r7.com",
    "cnn.com.br",
    "bbc.com",
    "reuters.com",
    "agenciabrasil.ebc.com.br",
    # Esportes
    "espn.com.br",
    "lance.com.br",
    "sportv.globo.com",
    "transfermarkt.com.br",
    # Tech
    "tecmundo.com.br",
    "canaltech.com.br",
    "olhardigital.com.br",
    # Referência / Institucional
    "wikipedia.org",
    "gov.br",
    # Compras
    "lojasamericanas.com",
    "submarino.com.br",
    "mercadolivre.com.br",
    "magazineluiza.com.br",
    "casasbahia.com.br",
    "amazon.com.br",
    "shopee.com.br",
    "carrefour.com.br",
    "extra.com.br",
    "netshoes.com.br",
    "ponto.com.br",
    "fastshop.com.br",
    "kabum.com.br",
    "pichau.com.br"
]

app = FastAPI(title="Busca IA", version="6.0.0")
templates = Jinja2Templates(directory="templates")

# Estados globais (alteráveis via endpoints)
web_search_enabled: bool = True
use_whitelist: bool = True          # whitelist ativa por padrão


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except Exception:
        return url


def favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=32"


def build_sources(results: list[dict]) -> list[dict]:
    sources = []
    for i, r in enumerate(results, start=1):
        url = r.get("url", "")
        domain = extract_domain(url)
        sources.append({
            "id": i,
            "title": r.get("title", "Sem título"),
            "url": url,
            "domain": domain,
            "icon": favicon_url(domain),
            "snippet": r.get("content", "")[:200],
        })
    return sources


def tavily_search(question: str, domains: list[str] | None = None) -> list[dict]:
    """
    Busca via Tavily.
    - Se `domains` for passado, restringe aos domínios da whitelist.
    - Se a busca restrita retornar 0 resultados, faz fallback para busca aberta.
    """
    client = TavilyClient(api_key=TAVILY_API_KEY)

    def _search(include_domains: list[str] | None) -> list[dict]:
        kwargs = dict(
            query=question,
            search_depth="basic",
            max_results=6,
            include_answer=False,
            include_raw_content=False,
        )
        if include_domains:
            kwargs["include_domains"] = include_domains
        response = client.search(**kwargs)
        return response.get("results", [])

    # Tentativa 1: busca na whitelist
    if domains:
        print(f"[TAVILY] Buscando com whitelist ({len(domains)} domínios): '{question}'")
        try:
            results = _search(domains)
            print(f"[TAVILY] ✓ {len(results)} resultados (whitelist)")
            if results:
                return results
            # Whitelist não retornou nada — faz fallback
            print("[TAVILY] Whitelist sem resultados → fallback para busca aberta")
        except Exception as e:
            print(f"[TAVILY] ✗ Erro na busca com whitelist: {e}")

    # Tentativa 2: busca aberta (fallback ou whitelist desativada)
    print(f"[TAVILY] Buscando sem restrição: '{question}'")
    try:
        results = _search(None)
        print(f"[TAVILY] ✓ {len(results)} resultados (busca aberta)")
        for r in results:
            print(f"  → {r.get('title','?')[:55]} | {r.get('url','')[:45]}")
        return results
    except Exception as e:
        print(f"[TAVILY] ✗ Erro na busca aberta: {e}")
        return []


async def web_search(question: str) -> list[dict]:
    """Executa Tavily em thread separada para não bloquear o event loop."""
    domains = TRUSTED_DOMAINS if use_whitelist else None
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, tavily_search, question, domains)
    return build_sources(raw)


def build_prompt(question: str, sources: list[dict]) -> str:
    context_blocks = "\n\n".join(
        f"[web:{s['id']}] {s['title']}\nURL: {s['url']}\n{s['snippet']}"
        for s in sources
    )
    return f"""Você é um assistente de pesquisa inteligente. Responda à pergunta abaixo de forma clara, \
completa e bem estruturada em Português do Brasil.

**Regras obrigatórias:**
- Use citações inline no formato [web:N] sempre que usar informação de uma fonte.
- Cite ao menos 3 fontes diferentes se disponíveis.
- Não invente informações que não estejam nas fontes.
- Formate a resposta em Markdown (negrito, listas quando necessário).
- NUNCA diga que não tem acesso à internet — você tem as fontes listadas acima.

**Fontes disponíveis:**
{context_blocks}

**Pergunta:** {question}

**Resposta:**"""


def build_prompt_no_web(question: str) -> str:
    return f"""Você é um assistente inteligente. Responda à pergunta abaixo em Português do Brasil \
de forma clara e completa.

**Pergunta:** {question}

**Resposta:**"""


async def groq_stream(prompt: str):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    print(f"[GROQ] Enviando prompt ({len(prompt)} chars)")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", GROQ_URL, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    try:
                        error = json.loads(body).get("error", {}).get("message", body.decode())
                    except Exception:
                        error = body.decode()
                    yield f"data: {json.dumps({'type': 'error', 'text': f'Groq erro {response.status_code}: {error}'})}\n\n"
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        text = chunk["choices"][0]["delta"].get("content", "")
                        if text:
                            yield f"data: {json.dumps({'type': 'chunk', 'text': text})}\n\n"
                    except Exception:
                        continue

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    except httpx.TimeoutException:
        yield f"data: {json.dumps({'type': 'error', 'text': 'Timeout: Groq demorou demais.'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "web_enabled": web_search_enabled,
        "whitelist_enabled": use_whitelist,
    })


@app.post("/toggle_web")
async def toggle_web():
    global web_search_enabled
    web_search_enabled = not web_search_enabled
    return {"web_search": web_search_enabled}


@app.post("/toggle_whitelist")
async def toggle_whitelist():
    """Liga/desliga a restrição de domínios confiáveis."""
    global use_whitelist
    use_whitelist = not use_whitelist
    status = "ativada" if use_whitelist else "desativada"
    print(f"[WHITELIST] {status.upper()}")
    return {"whitelist": use_whitelist, "message": f"Whitelist {status}."}


@app.get("/whitelist")
async def get_whitelist():
    """Retorna a lista de domínios confiáveis configurados."""
    return {"enabled": use_whitelist, "domains": TRUSTED_DOMAINS}


@app.get("/status")
async def status():
    return {
        "web_search_enabled": web_search_enabled,
        "whitelist_enabled": use_whitelist,
        "trusted_domains": TRUSTED_DOMAINS,
        "groq_model": GROQ_MODEL,
        "groq_key_set": GROQ_API_KEY != "SUA_CHAVE_GROQ_AQUI",
        "tavily_key_set": TAVILY_API_KEY != "SUA_CHAVE_TAVILY_AQUI",
    }


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    question: str = body.get("question", "").strip()

    print(f"\n{'='*60}")
    print(f"[CHAT] Pergunta: '{question}'")
    print(f"[CHAT] Busca web: {'ON' if web_search_enabled else 'OFF'} | "
          f"Whitelist: {'ON' if use_whitelist else 'OFF'}")

    if not question:
        return {"error": "Pergunta vazia."}

    sources: list[dict] = []
    if web_search_enabled:
        sources = await web_search(question)

    prompt = build_prompt(question, sources) if sources else build_prompt_no_web(question)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"
        async for chunk in groq_stream(prompt):
            yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )