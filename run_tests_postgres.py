"""
Runs the offline test suites against a real PostgreSQL - the database the platform uses in
production (Supabase) - instead of SQLite. Each suite gets a fresh, empty database.

Needs the embedded server package once:  pip install pgserver asyncpg
Never touches whatsapp_bot.db or any real database.

    python run_tests_postgres.py                  # every suite
    python run_tests_postgres.py test_inbox.py    # just one
"""

import os
import subprocess
import sys
import tempfile

SUITES = ["test_industry_brain.py", "test_learning_and_selftest.py", "test_platform_accounts.py",
          "test_onboarding_and_playbooks.py", "test_inbox.py", "test_voice.py", "test_sync_and_retention.py"]


def main() -> int:
    try:
        import pgserver
    except ImportError:
        print("Install the embedded PostgreSQL first:  pip install pgserver asyncpg")
        return 2
    data_dir = os.path.join(tempfile.gettempdir(), "whatsapp_bot_pg_tests")
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    base = server.get_uri()                     # postgresql://postgres:@127.0.0.1:<port>/postgres
    suites = sys.argv[1:] or [s for s in SUITES if os.path.exists(s)]
    failed = []
    for suite in suites:
        name = "t_" + suite.removesuffix(".py").replace("-", "_")
        server.psql(f"DROP DATABASE IF EXISTS {name};")
        server.psql(f"CREATE DATABASE {name};")
        head, _, query = base.partition("?")
        url = head.rsplit("/", 1)[0] + f"/{name}" + (f"?{query}" if query else "")
        env = {**os.environ, "TEST_DATABASE_URL": url, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run([sys.executable, suite], env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        lines = [l for l in (result.stdout + result.stderr).splitlines()
                 if "checks passed" in l or "FAILED" in l or "Error" in l]
        print(f"== {suite} (PostgreSQL): {'ok' if result.returncode == 0 else 'FAILED'}")
        for line in (lines[-4:] if result.returncode else lines[-1:]):
            print("   " + line)
        if result.returncode:
            failed.append(suite)
            tail = (result.stdout + result.stderr).splitlines()[-25:]
            print("\n".join("   | " + l for l in tail))
    print("\nAll suites passed on PostgreSQL." if not failed else f"\nFailed on PostgreSQL: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
