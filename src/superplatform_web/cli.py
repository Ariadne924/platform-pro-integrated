"""CLI entry point for Superplatform Web."""

import argparse


def main():
    parser = argparse.ArgumentParser(prog="superplatform-web", description="Superplatform Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code change")
    args = parser.parse_args()

    import uvicorn

    print(f"Superplatform Web → http://{args.host}:{args.port}")
    uvicorn.run(
        "superplatform_web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
