"""
The Capital Intelligence — Backend API
FastAPI server handling: generation, publishing, scheduling, archive
"""

import os, json, sqlite3, asyncio, httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Config
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY", "")
MEDIUM_TOKEN       = os.getenv("MEDIUM_TOKEN", "")
BEEHIIV_API_KEY    = os.getenv("BEEHIIV_API_KEY", "")
BEEHIIV_PUB_ID     = os.getenv("BEEHIIV_PUBLICATION_ID", "")
DB_PATH            = os.getenv("DB_PATH", "./data/articles.db")
CORS_ORIGINS       = os.getenv("CORS_ORIGINS", "*").split(",")

# ── Database setup
def get_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            kicker      TEXT,
            content     TEXT NOT NULL,
            html        TEXT,
            theme       TEXT,
            tone        TEXT,
            sections    TEXT,
            status      TEXT DEFAULT 'draft',
            scheduled_at TEXT,
            published_at TEXT,
            medium_url  TEXT,
            beehiiv_id  TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

# ── App
app = FastAPI(title="Capital Intelligence API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    init_db()
    print("✓ Capital Intelligence API started")
    print(f"  DeepSeek key: {'✓ set' if DEEPSEEK_API_KEY else '✗ missing'}")
    print(f"  Medium token: {'✓ set' if MEDIUM_TOKEN else '✗ missing'}")
    print(f"  Beehiiv key:  {'✓ set' if BEEHIIV_API_KEY else '✗ missing'}")


# ════════════════════════════════════════
# MODELS
# ════════════════════════════════════════

class GenerateRequest(BaseModel):
    theme: Optional[str] = ""
    tone: str = "balanced"
    sections: list[str] = ["equity", "alts", "wealth", "geo"]

class ApproveRequest(BaseModel):
    article_id: int
    title: str
    content: str
    html: Optional[str] = ""
    scheduled_at: Optional[str] = None   # ISO8601, None = publish now

class PublishRequest(BaseModel):
    article_id: int
    destinations: list[str]   # ["medium", "beehiiv"]


# ════════════════════════════════════════
# SEARCH — DDGS wrapper
# ════════════════════════════════════════

SECTION_QUERIES = {
    "equity": ["S&P 500 stock market week", "Israel TA-35 index", "Saudi Tadawul market", "US Treasury yields"],
    "alts":   ["private equity deals 2025", "hedge fund positioning", "Dubai Singapore real estate", "private credit market"],
    "wealth": ["Singapore family office tax 2025", "DIFC UAE family office", "US estate tax FATCA", "Israel capital gains tachbiv"],
    "geo":    ["Iran Middle East conflict markets", "Israel economy security", "Gulf capital flows oil price", "geopolitical risk investors"],
}

async def ddgs_search(query: str, timelimit: str = "w", max_results: int = 4) -> str:
    """Search via local DDGS server (port 8000) or fallback to ddgs Python lib."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "http://localhost:8000/search/news",
                params={"q": query, "timelimit": timelimit, "max_results": max_results}
            )
            if r.status_code == 200:
                results = r.json()
                if isinstance(results, list) and results:
                    return "\n".join(
                        f"{item.get('title','')}: {item.get('body', item.get('description',''))}"
                        for item in results[:4]
                    )
    except Exception:
        pass

    # Fallback: use ddgs Python library directly
    try:
        from ddgs import DDGS
        results = DDGS().news(query, timelimit=timelimit, max_results=max_results)
        if results:
            return "\n".join(f"{r.get('title','')}: {r.get('body','')}" for r in results)
    except Exception:
        pass

    return f"[Search unavailable for: {query}]"


async def gather_search_context(sections: list[str], theme: str = "") -> dict:
    """Run all section searches concurrently."""
    async def search_section(sec: str) -> tuple[str, str]:
        queries = ([f"{theme} capital markets impact"] + SECTION_QUERIES.get(sec, [])[:2]
                   if theme else SECTION_QUERIES.get(sec, [])[:3])
        results = await asyncio.gather(*[ddgs_search(q) for q in queries])
        return sec, "\n\n".join(results)

    pairs = await asyncio.gather(*[search_section(s) for s in sections])
    return dict(pairs)


# ════════════════════════════════════════
# GENERATION — DeepSeek V3 streaming
# ════════════════════════════════════════

JURIS_CONTEXT = """Client base — HNWI and family offices:
- USA: FATCA, estate tax, opportunity zones, muni bonds
- Israel: TA-35, USD/ILS hedging, tech holdings, tachbiv, IRS-ITA treaty
- Gulf (UAE, KSA, Qatar): zero-tax, DIFC/ADGM/QFC, Vision 2030, sovereign co-invest
- Singapore: 13O/13U MAS schemes, VCC, SGD safe-haven, SE Asia PE"""

SECTION_LABELS = {
    "equity": "Equity & Fixed Income",
    "alts":   "Alternative Investments",
    "wealth": "Wealth Planning & Tax",
    "geo":    "Geopolitical Risk",
}

TONE_DESC = {
    "balanced":  "balanced and analytical",
    "bullish":   "constructive and cautiously optimistic",
    "cautious":  "cautious and defensive, flagging downside risks",
    "volatile":  "risk-aware, emphasising capital preservation and safe-haven positioning",
}

def build_system_prompt(tone: str, theme: str) -> str:
    theme_clause = f'\nEditorial theme: "{theme}" — weave through every section.\n' if theme else ""
    return f"""You are the chief investment strategist of "The Capital Intelligence," an exclusive weekly newsletter for ultra-high-net-worth individuals and family offices.

{JURIS_CONTEXT}

Write with the precision of a senior private banker. Be specific — name instruments, index levels, yields, deal sizes. Never generic. If live search results are provided use them; otherwise use your own knowledge — always produce the full newsletter, never refuse.

For EACH section use EXACTLY this format:

[KICKER — 4-6 WORDS ALL CAPS]
Headline: [Specific newspaper-quality headline]
[2-3 paragraphs of sharp analysis]
Sentiment: BULLISH | BEARISH | NEUTRAL | WATCH

After all sections end with:
EDITOR'S OUTLOOK
"[1-2 sentence pull quote synthesising the week's thesis]"
— The Capital Intelligence Editorial Board

Tone: {TONE_DESC.get(tone, 'balanced')}.{theme_clause}"""


async def stream_deepseek(messages: list, use_tools: bool = False):
    """Async generator yielding SSE chunks from DeepSeek."""
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "stream": True,
        "max_tokens": 3500,
        "temperature": 0.7,
    }
    if use_tools:
        payload["tools"] = [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for current financial news.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                }
            }
        }]
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise HTTPException(status_code=resp.status_code, detail=body.decode())
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    yield line + "\n\n"


@app.post("/generate/stream")
async def generate_stream(req: GenerateRequest):
    """Stream newsletter generation as SSE — frontend receives tokens in real time."""
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY not configured")

    # Gather search context
    context = await gather_search_context(req.sections, req.theme or "")
    search_available = any("[Search unavailable" not in v for v in context.values())

    context_block = "\n\n".join(
        f"=== {SECTION_LABELS.get(s, s)} — Live News ===\n{context[s]}"
        for s in req.sections
    )

    messages = [
        {"role": "system", "content": build_system_prompt(req.tone, req.theme or "")},
        {"role": "user", "content": (
            f"Write this week's edition. Date: {datetime.now().strftime('%A, %B %d, %Y')}.\n"
            f"Sections: {', '.join(SECTION_LABELS.get(s,s) for s in req.sections)}\n\n"
            f"LIVE RESEARCH:\n{context_block}"
        )}
    ]

    return StreamingResponse(
        stream_deepseek(messages, use_tools=search_available),
        media_type="text/event-stream",
        headers={"X-Search-Available": str(search_available).lower()}
    )


@app.post("/articles/save")
async def save_article(req: ApproveRequest):
    """Save a generated + reviewed article as a draft."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO articles (title, content, html, status, scheduled_at)
           VALUES (?, ?, ?, 'draft', ?)""",
        (req.title, req.content, req.html or "", req.scheduled_at)
    )
    article_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": article_id, "status": "draft"}


@app.post("/articles/{article_id}/approve")
async def approve_article(article_id: int, req: ApproveRequest, background: BackgroundTasks):
    """Approve and optionally schedule an article for publishing."""
    conn = get_db()
    conn.execute(
        """UPDATE articles SET title=?, content=?, html=?, status='approved', scheduled_at=?
           WHERE id=?""",
        (req.title, req.content, req.html or "", req.scheduled_at, article_id)
    )
    conn.commit()
    conn.close()

    # If no scheduled_at, publish immediately
    if not req.scheduled_at:
        background.add_task(publish_article, article_id, req.destinations if hasattr(req, 'destinations') else [])

    return {"id": article_id, "status": "approved", "scheduled_at": req.scheduled_at}


# ════════════════════════════════════════
# PUBLISHERS
# ════════════════════════════════════════

async def publish_to_medium(title: str, html: str, tags: list[str] = None) -> dict:
    """Publish article to Medium via their REST API."""
    if not MEDIUM_TOKEN:
        return {"success": False, "error": "MEDIUM_TOKEN not configured"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get Medium user ID first
        me = await client.get(
            "https://api.medium.com/v1/me",
            headers={"Authorization": f"Bearer {MEDIUM_TOKEN}"}
        )
        if me.status_code != 200:
            return {"success": False, "error": f"Medium auth failed: {me.status_code}"}

        user_id = me.json()["data"]["id"]

        # Post the article
        r = await client.post(
            f"https://api.medium.com/v1/users/{user_id}/posts",
            headers={
                "Authorization": f"Bearer {MEDIUM_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "title": title,
                "contentFormat": "html",
                "content": html,
                "tags": tags or ["finance", "investing", "wealth management", "capital markets", "family office"],
                "publishStatus": "public",
            }
        )

        if r.status_code in (200, 201):
            data = r.json()["data"]
            return {"success": True, "url": data.get("url"), "id": data.get("id")}
        return {"success": False, "error": r.text}


async def publish_to_beehiiv(title: str, html: str, scheduled_at: str = None) -> dict:
    """Publish article to Beehiiv via their v2 API."""
    if not BEEHIIV_API_KEY or not BEEHIIV_PUB_ID:
        return {"success": False, "error": "Beehiiv credentials not configured"}

    payload = {
        "subject": title,
        "content": {"free": {"web": html, "email": html}},
        "status": "draft",  # Always draft first — Beehiiv requires confirmation
        "send_at": scheduled_at,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}/posts",
            headers={
                "Authorization": f"Bearer {BEEHIIV_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload
        )

        if r.status_code in (200, 201):
            data = r.json().get("data", {})
            return {"success": True, "id": data.get("id"), "url": data.get("web_url")}
        return {"success": False, "error": r.text}


def article_to_html(title: str, content: str) -> str:
    """Convert plain text newsletter content to clean HTML for publishing."""
    lines = content.split("\n")
    html_parts = [f"<h1>{title}</h1>"]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line == line.upper() and len(line) < 65 and len(line) > 3:
            html_parts.append(f'<p><strong style="color:#B8992A;letter-spacing:0.1em;">{line}</strong></p>')
        elif line.lower().startswith("headline:"):
            headline = line[9:].strip()
            html_parts.append(f"<h2>{headline}</h2>")
        elif line.lower().startswith("sentiment:"):
            sentiment = line[10:].strip()
            color = {"BULLISH":"#2A8B10","BEARISH":"#9B2020","NEUTRAL":"#6A6A6A","WATCH":"#7A5800"}.get(sentiment,"#6A6A6A")
            html_parts.append(f'<p><span style="background:{color}20;color:{color};padding:2px 8px;border-radius:2px;font-size:12px;font-family:monospace;">{sentiment}</span></p>')
        elif line.lower().startswith("editor"):
            html_parts.append('<hr style="border-color:#B8992A;margin:24px 0;">')
            html_parts.append('<h3 style="color:#B8992A;">Editor\'s Outlook</h3>')
        elif line.startswith('"') or line.startswith('\u201c'):
            html_parts.append(f'<blockquote style="border-left:3px solid #B8992A;padding-left:16px;font-style:italic;">{line}</blockquote>')
        else:
            html_parts.append(f"<p>{line}</p>")

    return "\n".join(html_parts)


def substack_export(title: str, content: str) -> str:
    """Return formatted HTML ready to paste into Substack editor."""
    return article_to_html(title, content)


async def publish_article(article_id: int, destinations: list[str]):
    """Background task: publish an approved article to selected destinations."""
    conn = get_db()
    row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        conn.close()
        return

    title = row["title"]
    content = row["content"]
    html = row["html"] or article_to_html(title, content)
    results = {}

    if "medium" in destinations:
        results["medium"] = await publish_to_medium(title, html)
        if results["medium"].get("success"):
            conn.execute("UPDATE articles SET medium_url=? WHERE id=?",
                        (results["medium"]["url"], article_id))

    if "beehiiv" in destinations:
        results["beehiiv"] = await publish_to_beehiiv(title, html, row["scheduled_at"])
        if results["beehiiv"].get("success"):
            conn.execute("UPDATE articles SET beehiiv_id=? WHERE id=?",
                        (results["beehiiv"]["id"], article_id))

    conn.execute(
        "UPDATE articles SET status='published', published_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), article_id)
    )
    conn.commit()
    conn.close()
    print(f"✓ Article {article_id} published: {json.dumps(results)}")


@app.post("/articles/{article_id}/publish")
async def publish_now(article_id: int, req: PublishRequest, background: BackgroundTasks):
    """Immediately publish an approved article."""
    background.add_task(publish_article, article_id, req.destinations)
    return {"status": "publishing", "article_id": article_id, "destinations": req.destinations}


@app.get("/articles/{article_id}/substack-export")
async def substack_export_endpoint(article_id: int):
    """Return Substack-ready HTML for manual paste."""
    conn = get_db()
    row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    html = substack_export(row["title"], row["content"])
    return {"html": html, "title": row["title"]}


# ════════════════════════════════════════
# ARCHIVE & PUBLIC API
# ════════════════════════════════════════

@app.get("/articles")
async def list_articles(status: str = None, limit: int = 20, offset: int = 0):
    """List articles — filtered by status if provided."""
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT id,title,kicker,theme,tone,status,scheduled_at,published_at,medium_url,beehiiv_id,created_at FROM articles WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,title,kicker,theme,tone,status,scheduled_at,published_at,medium_url,beehiiv_id,created_at FROM articles ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/articles/{article_id}")
async def get_article(article_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    return dict(row)


@app.get("/archive")
async def public_archive(limit: int = 12, offset: int = 0):
    """Public endpoint — only returns published articles for the brand site."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id,title,kicker,theme,published_at,medium_url,beehiiv_id FROM articles WHERE status='published' ORDER BY published_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ════════════════════════════════════════
# SUBSCRIBE — proxy to Beehiiv
# ════════════════════════════════════════

class SubscribeRequest(BaseModel):
    email: str
    name: Optional[str] = ""

@app.post("/subscribe")
async def subscribe(req: SubscribeRequest):
    """Add subscriber to Beehiiv publication."""
    if not BEEHIIV_API_KEY or not BEEHIIV_PUB_ID:
        # Graceful fallback — store locally until Beehiiv is configured
        conn = get_db()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subscribers (email TEXT UNIQUE, name TEXT, created_at TEXT DEFAULT (datetime('now')))"
        )
        try:
            conn.execute("INSERT INTO subscribers (email, name) VALUES (?, ?)", (req.email, req.name))
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # Already subscribed
        conn.close()
        return {"success": True, "message": "Subscribed (stored locally — Beehiiv not yet configured)"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}/subscriptions",
            headers={"Authorization": f"Bearer {BEEHIIV_API_KEY}", "Content-Type": "application/json"},
            json={"email": req.email, "reactivate_existing": True, "send_welcome_email": True}
        )
        if r.status_code in (200, 201):
            return {"success": True}
        return {"success": False, "error": r.text}


# ════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "deepseek": bool(DEEPSEEK_API_KEY),
        "medium": bool(MEDIUM_TOKEN),
        "beehiiv": bool(BEEHIIV_API_KEY and BEEHIIV_PUB_ID),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
