from fastapi import FastAPI, Query
import uvicorn
from ddgs import DDGS

app = FastAPI()

@app.get("/search/news")
def search_news(q: str = Query(...), timelimit: str = "w", max_results: int = 5):
    try:
        results = DDGS().news(q, timelimit=timelimit, max_results=max_results)
        return results if results else []
    except Exception as e:
        return {"error": str(e)}

@app.get("/search/text")
def search_text(q: str = Query(...), max_results: int = 5):
    try:
        results = DDGS().text(q, max_results=max_results)
        return results if results else []
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
