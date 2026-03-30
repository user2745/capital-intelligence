# The Capital Intelligence — Tech Stack

## Structure

```
capital-intelligence/
├── backend/
│   ├── main.py           # FastAPI server (port 8001)
│   ├── requirements.txt
│   └── .env              # API keys — never commit this
├── generator/
│   └── index.html        # Internal newsletter generator tool
└── public/
    └── index.html        # Public brand site
```

## Setup

### 1. Backend

```bash
cd backend
pip3 install -r requirements.txt
cp .env .env.local          # edit .env with your real keys
python3 main.py             # starts on http://localhost:8001
```

### 2. DDGS search server (optional — for live news)

```bash
pip3 install ddgs "fastapi[standard]" uvicorn
# In the ddgs repo directory:
python3 start_api.py        # starts on http://localhost:8000
```

### 3. Public site

Open `public/index.html` in a browser, or serve it:
```bash
cd public
python3 -m http.server 3000
```

### 4. Generator (internal tool)

Open `generator/index.html` directly in a browser.
Edit the `CONFIG` block at the top to set your DeepSeek key.

---

## API Keys needed

| Key | Where to get it | Required for |
|-----|----------------|--------------|
| `DEEPSEEK_API_KEY` | platform.deepseek.com | Generation |
| `MEDIUM_TOKEN` | medium.com/me/settings → Integration tokens | Publishing to Medium |
| `BEEHIIV_API_KEY` | app.beehiiv.com → Settings → API | Newsletter distribution |
| `BEEHIIV_PUBLICATION_ID` | Your Beehiiv publication URL | Newsletter distribution |

---

## API Endpoints

| Method | Endpoint | What it does |
|--------|----------|--------------|
| POST | `/generate/stream` | Stream newsletter generation (SSE) |
| POST | `/articles/save` | Save draft article |
| POST | `/articles/{id}/approve` | Approve + schedule |
| POST | `/articles/{id}/publish` | Publish to Medium/Beehiiv |
| GET  | `/articles/{id}/substack-export` | Get Substack-ready HTML |
| GET  | `/archive` | Public archive (published only) |
| GET  | `/articles` | All articles (internal) |
| POST | `/subscribe` | Add subscriber |
| GET  | `/health` | Check API key status |

---

## Publishing workflow

1. Open generator → set theme + tone → Generate
2. Review article in generator
3. Click **Save Draft** → POSTs to `/articles/save`
4. In dashboard: edit if needed → click **Approve**
5. Set publish date or publish immediately
6. Select destinations: Medium, Beehiiv, or both
7. For Substack: use **Export for Substack** → paste HTML into Substack editor
