"""コマンドラインエントリポイント。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from . import __version__
from .api import create_app
from .config import AppConfig, load_config
from .simulator import demo_config

DEFAULT_CONFIG = "config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snmp-monitor",
        description="SNMP モニタリングダッシュボードを起動します",
    )
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG, help="設定ファイル (既定: config.yaml)"
    )
    parser.add_argument("--host", help="待ち受けアドレス (設定ファイルより優先)")
    parser.add_argument("--port", type=int, help="待ち受けポート (設定ファイルより優先)")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="実機を使わずダミーデバイスで動かす (設定ファイル不要)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="ログレベル (既定: info)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def resolve_config(args: argparse.Namespace) -> AppConfig:
    path = Path(args.config)
    if args.demo:
        base = load_config(path) if path.exists() else AppConfig()
        config = demo_config(base)
    else:
        if not path.exists():
            raise SystemExit(
                f"設定ファイルが見つかりません: {path}\n"
                "config.example.yaml をコピーして作成するか、"
                "--demo を付けてデモモードで起動してください。"
            )
        config = load_config(path)
        if not config.enabled_devices:
            raise SystemExit(
                "有効なデバイスが 1 台もありません。設定ファイルの devices を確認してください。"
            )
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    config = resolve_config(args)
    app = create_app(config)

    if config.demo_mode:
        logging.getLogger(__name__).warning(
            "デモモードで起動します (SNMP は実行されず、ダミーデータを表示します)"
        )
    logging.getLogger(__name__).info(
        "http://%s:%s を開いてください", config.server.host, config.server.port
    )
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=args.log_level,
        access_log=args.log_level == "debug",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
