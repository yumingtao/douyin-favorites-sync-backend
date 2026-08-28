from __future__ import annotations

import uvicorn


if __name__ == "__main__":
    uvicorn.run("backend.app:app", host="127.0.0.1", port=8765, reload=False)
