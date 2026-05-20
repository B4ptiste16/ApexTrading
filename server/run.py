#!/usr/bin/env python3
"""
Run the APEX auth server locally for development.
Usage:  python server/run.py
        python server/run.py --port 9000
"""

import sys
import os
import argparse

# Ensure project root is on sys.path so `server` package resolves correctly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="APEX Auth Server")
    p.add_argument("--host",   default="127.0.0.1")
    p.add_argument("--port",   default=8000, type=int)
    p.add_argument("--reload", action="store_true", default=True)
    args = p.parse_args()

    print(f"\n  APEX Auth Server  →  http://{args.host}:{args.port}")
    print(f"  Docs              →  http://{args.host}:{args.port}/docs\n")

    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
