"""Local development entry point: ``python run.py`` or ``uvicorn run:app``."""

import uvicorn

from src.server.app import app


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
