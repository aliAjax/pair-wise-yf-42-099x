import argparse
import signal
import sys
from pathlib import Path

from src.http_api import create_server
from src.quota import QuotaService
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def main(argv=None):
    parser = argparse.ArgumentParser(description="动物园谱系与繁育协调")
    parser.add_argument("--db", default="./data.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8308)
    args = parser.parse_args(argv)

    repository = SQLiteRepository(args.db)
    rules = RuleEngine()
    service = DomainService(repository, rules)
    quota = QuotaService(repository, service.audit)
    recovered = quota.recover_on_startup()
    if recovered:
        print("恢复未完成对账任务: " + ", ".join(recovered), flush=True)
    static_dir = Path(__file__).resolve().parent / "static"
    server = create_server(
        args.host, args.port, service, rules, str(static_dir), quota=quota
    )

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        print("动物园谱系与繁育协调 listening on http://%s:%s" % (args.host, args.port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
