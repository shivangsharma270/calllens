"""Start CallLens: python run.py, then open http://127.0.0.1:8000"""

import uvicorn

if __name__ == "__main__":
    # One process only: the rate limiter and job runner live in memory.
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="info")
