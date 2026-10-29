"""Entry point: `uvicorn main:app` or `python main.py`."""

from loggingx_ai.main import app  # noqa: F401 - uvicorn loads main:app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8001)
