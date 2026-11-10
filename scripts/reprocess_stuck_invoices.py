#!/usr/bin/env python3
"""批量重解析卡在 UPLOADED / PROCESSING 的发票。

为什么需要这个脚本
------------------
钉钉审批附件导入走的是「上传 → 后台异步 LLM 解析 → CONFIRMED / REVIEWING」。
历史回补（2026-09-21 跑 92 天）期间上游 LLM 网关大量返回 500 / EOF，导致一批
发票上传成功但解析失败，永远停在 UPLOADED 或 PROCESSING：

  - UPLOADED   ：上传完就从没解析成功过
  - PROCESSING ：解析中途挂掉，状态没回滚

它们不会自动重试，只会静静躺在库里，且因为没有 invoice_number 而无法同步到
钉钉表，表现成「明明导了却对不上账」。

后端有现成端点 POST /api/invoices/batch-reprocess（限流 5 次/分钟），本脚本
负责：登录取 token → 分批调用 → 轮询直到卡住的数量清零。

用法
----
    python3 scripts/reprocess_stuck_invoices.py            # 全量重解析
    python3 scripts/reprocess_stuck_invoices.py --dry-run  # 只看有多少张
    python3 scripts/reprocess_stuck_invoices.py --timeout 1800

退出码：0 全部离开卡住状态 / 1 超时仍有残留 / 2 调用出错
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import requests

API = 'http://127.0.0.1:18841/api'
DB_CONTAINER = 'invoice_db'
STUCK_STATUSES = ('UPLOADED', 'PROCESSING')
CHUNK_SIZE = 8          # 每批张数：后台解析已有 Semaphore 闸门，这里只在网络层分批
CHUNK_INTERVAL = 13     # 端点限流 5 次/分钟，留足余量
POLL_INTERVAL = 30


def find_docker() -> str:
    for path in ('/usr/local/bin/docker', '/opt/homebrew/bin/docker', '/usr/bin/docker'):
        try:
            subprocess.run([path, '--version'], capture_output=True, timeout=10, check=True)
            return path
        except Exception:
            continue
    return 'docker'


DOCKER = find_docker()


def db_query(sql: str) -> list[list[str]]:
    proc = subprocess.run(
        [DOCKER, 'exec', DB_CONTAINER, 'psql', '-U', 'postgres', '-d', 'invoice_db',
         '-t', '-A', '-F', '\x1f', '-c', sql],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'查询本地库失败: {proc.stderr[:300]}')
    rows = []
    for line in proc.stdout.splitlines():
        if line.strip():
            rows.append(line.split('\x1f'))
    return rows


def stuck_ids() -> list[int]:
    sql = ("SELECT id FROM invoices WHERE status IN "
           "(" + ','.join(f"'{s}'" for s in STUCK_STATUSES) + ") ORDER BY id")
    return [int(r[0]) for r in db_query(sql) if r and r[0].strip().isdigit()]


def status_breakdown() -> dict[str, int]:
    out = {}
    for row in db_query("SELECT status, count(*) FROM invoices GROUP BY status ORDER BY 2 DESC"):
        if len(row) >= 2:
            out[row[0].strip()] = int(row[1].strip())
    return out


def login() -> str:
    resp = requests.post(f'{API}/auth/login',
                         json={'username': 'admin', 'password': 'qwe123'}, timeout=30)
    resp.raise_for_status()
    return resp.json()['access_token']


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只统计，不触发重解析')
    ap.add_argument('--timeout', type=int, default=1800, help='等待清零的最长秒数')
    args = ap.parse_args()

    before = status_breakdown()
    ids = stuck_ids()
    print(f'当前状态分布: {before}')
    print(f'卡在 {" / ".join(STUCK_STATUSES)} 的发票: {len(ids)} 张')
    if not ids:
        print('✅ 无需重解析')
        return 0
    if args.dry_run:
        print('（dry-run，未触发）')
        return 0

    token = login()
    headers = {'Authorization': f'Bearer {token}'}

    scheduled = 0
    for i in range(0, len(ids), CHUNK_SIZE):
        chunk = ids[i:i + CHUNK_SIZE]
        try:
            resp = requests.post(f'{API}/invoices/batch-reprocess', headers=headers,
                                 json={'invoice_ids': chunk}, timeout=60)
        except requests.RequestException as exc:
            print(f'⚠️ 批次 {chunk} 请求异常: {exc}', file=sys.stderr)
            return 2
        if resp.status_code == 429:
            print(f'⏱ 触发限流，等待 {CHUNK_INTERVAL * 2}s 后重试批次 {chunk}')
            time.sleep(CHUNK_INTERVAL * 2)
            continue
        if resp.status_code != 200:
            print(f'⚠️ 批次 {chunk} 返回 {resp.status_code}: {resp.text[:200]}', file=sys.stderr)
            return 2
        scheduled += resp.json().get('count', len(chunk))
        print(f'  已投递 {scheduled}/{len(ids)}（批次 {i // CHUNK_SIZE + 1}）')
        if i + CHUNK_SIZE < len(ids):
            time.sleep(CHUNK_INTERVAL)

    print(f'\n⏳ 已投递 {scheduled} 张，等待后台解析收敛...')
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        remaining = len(stuck_ids())
        print(f'[{time.strftime("%H:%M:%S")}] 仍卡住 {remaining} 张  {status_breakdown()}')
        if remaining == 0:
            print('\n✅ 全部离开卡住状态')
            return 0
        time.sleep(POLL_INTERVAL)

    remaining = len(stuck_ids())
    print(f'\n⚠️ 超时，仍有 {remaining} 张未解析完成: {stuck_ids()}', file=sys.stderr)
    print('   多为上游 LLM 网关持续异常，可稍后再跑一次本脚本。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
