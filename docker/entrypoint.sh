#!/bin/sh
set -e

echo "[entrypoint] 等待数据库 ${POSTGRES_HOST:-db}:${POSTGRES_PORT:-5432} 就绪..."
python - <<'PY'
import os, time, psycopg2
url = os.environ.get("IRR_DATABASE_URL")
for i in range(60):
    try:
        psycopg2.connect(url).close()
        print("[entrypoint] 数据库连接成功")
        break
    except Exception as exc:
        print(f"[entrypoint] 数据库未就绪({i+1}/60): {exc}")
        time.sleep(2)
else:
    raise SystemExit("数据库等待超时")
PY

# 建表（应用 startup 也会执行，此处提前保证 seed 前表已存在）
python -c "from app.db import Base, engine; from app import models; Base.metadata.create_all(bind=engine)"

if [ "${IRR_SEED:-true}" = "true" ]; then
  echo "[entrypoint] 写入样例数据（迟到补传/换表缺证/渠道失衡/证据冲突 + 渗漏/表漂移/证据不足诊断场景）..."
  python -m scripts.seed || echo "[entrypoint] 种子已存在或写入失败，跳过"
fi

exec "$@"
