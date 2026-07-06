"""
Standalone server for the Procurement AI module.
Run independently — does not affect the main AI framework.

Usage:
    python server.py
    OR
    uvicorn server:app --host 0.0.0.0 --port 8001 --reload
"""

import sys
import os
import importlib.util

# Add AI framework root to path so that "app.config" (used inside procurement 2.py) resolves
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Load "procurement 2.py" — importlib handles the space in the filename
_spec = importlib.util.spec_from_file_location(
    "procurement",
    os.path.join(os.path.dirname(__file__), "procurement.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapi import FastAPI

app = FastAPI(
    root_path="/procurement"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(_mod.router)

if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("Procurement AI Service")
    print("Swagger UI : http://localhost:8001/docs")
    print("=" * 60)
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=True)
