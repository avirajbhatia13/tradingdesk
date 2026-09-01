"""Where things live on disk.

In the full desk this module is the only place that reads the environment and
`config/accounts.yaml`, and it carries the broker credentials, account list and
feed settings that the live dashboard runs on. None of that is in this
repository — this is the research engine, not the trading desk — so what
remains here is the handful of paths the backtest engine needs to find data.

The names and values match the private module exactly, so the code below this
point is byte-identical to what runs the real thing.
"""

from pathlib import Path

# The repo root, three levels above this file (backend/app/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

# The DuckDB catalogue that indexes the Parquet lake. The lake itself is not in
# this repository; see the README on what that means for running a backtest.
DB_PATH = REPO_ROOT / "db" / "trading.db"
