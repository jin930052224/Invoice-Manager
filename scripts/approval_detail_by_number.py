#!/usr/bin/env python3
"""按「审批编号」(businessId) 取钉钉日常报销审批单详情。

⚠️ 为什么必须用这个脚本而不是直接 dws oa approval detail：

  dws oa approval detail --instance-id 要的是 **processInstanceId**，
  形如 "4_7ZDgmlSeiTiP3fcho76g08321789712185"；
  而表格 M 列 / 库里 approval_instance_id 存的是 **businessId**（审批编号），
  形如 "202609181416000253467"（21 位数字，前 8 位是发起日期）。

  直接拿 businessId 当 --instance-id 传，钉钉回
      {"success":false,"dingOpenErrcode":400002}
  dws 把外层包成 "business error: success=false"，**极容易被误判成
  「老审批单读不到」(G 类) 而放弃重试**。实际上这是参数类型错误。

  正确姿势：先 list-by-admin 按天翻页拿到 processInstanceId，再 detail。

用法：
  python3 approval_detail_by_number.py 202609181416000253467
  python3 approval_detail_by_number.py 202609181416000253467 --raw   # 输出完整详情 JSON

退出码：0 成功 / 1 未找到 / 2 接口失败
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'
DWS_ALT = '/Users/king/.workbuddy/binaries/node/cli-connector-packages/bin/dws'
PROCESS_CODE = 'PROC-FF9942ED-92C0-46BA-9923-3861BBB66AAE'  # 日常报销
RETRIES = 3
RETRY_SLEEP = 2


def _dws_bin() -> str:
    import os
    for p in (DWS, DWS_ALT):
        if os.path.exists(p):
            return p
    return 'dws'


def call_json(args: list[str]) -> tuple[dict | None, str | None]:
    """调用 dws 并解析 JSON。dws 会间歇性抽风，失败重试。"""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            out = subprocess.check_output([_dws_bin(), *args], text=True, timeout=45)
        except subprocess.CalledProcessError as exc:
            last = f'dws 退出码 {exc.returncode}: {(exc.output or "")[:300]}'
        except Exception as exc:  # noqa: BLE001
            last = f'{type(exc).__name__}: {exc}'
        else:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                last = f'非 JSON 输出: {out[:300]}'
            else:
                if data.get('error'):
                    # 参数/业务错误重试无意义，直接返回
                    return None, json.dumps(data['error'], ensure_ascii=False)[:400]
                return data, None
        if attempt < RETRIES:
            time.sleep(RETRY_SLEEP)
    return None, last


def date_from_business_id(business_id: str) -> str:
    """businessId 前 8 位就是发起日期：202609181416000253467 -> 2026-09-18"""
    if len(business_id) < 8 or not business_id[:8].isdigit():
        raise ValueError(f'无法从审批编号前缀推断日期: {business_id!r}')
    return f'{business_id[:4]}-{business_id[4:6]}-{business_id[6:8]}'


def resolve_process_instance_id(business_id: str, day: str) -> tuple[str | None, str | None]:
    cursor = '0'
    for _ in range(50):  # 单日最多翻 50 页（1000 条）足够
        page, err = call_json([
            'oa', 'approval', 'list-by-admin',
            '--process-code', PROCESS_CODE,
            '--start', f'{day}T00:00:00',
            '--end', f'{day}T23:59:59',
            '--cursor', str(cursor),
            '--limit', '20',
            '--format', 'json',
        ])
        if page is None:
            return None, err
        body = page.get('result') or {}
        for item in body.get('list') or []:
            if item.get('businessId') == business_id:
                pid = item.get('processInstanceId')
                if not pid:
                    return None, f'列表命中但无 processInstanceId: {json.dumps(item, ensure_ascii=False)}'
                return pid, None
        if not body.get('hasMore'):
            break
        nxt = body.get('nextCursor')
        if nxt in (None, '', cursor):
            break
        cursor = str(nxt)
    return None, f'{day} 当天列表里没找到审批编号 {business_id}'


def fetch_detail(business_id: str) -> tuple[dict | None, str | None, str | None]:
    day = date_from_business_id(business_id)
    pid, err = resolve_process_instance_id(business_id, day)
    if pid is None:
        return None, err, None
    detail, err = call_json(['oa', 'approval', 'detail', '--instance-id', pid, '--format', 'json'])
    if detail is None:
        return None, err, pid
    body = detail.get('result')
    if body is None:
        return None, f'详情未返回 result: {json.dumps(detail, ensure_ascii=False)[:300]}', pid
    return body, None, pid


def main() -> int:
    ap = argparse.ArgumentParser(description='按审批编号取日常报销审批单详情')
    ap.add_argument('business_id', help='审批编号（21 位，表 M 列 / 库 approval_instance_id）')
    ap.add_argument('--raw', action='store_true', help='输出完整详情 JSON')
    args = ap.parse_args()

    body, err, pid = fetch_detail(args.business_id)
    if body is None:
        print(json.dumps({'ok': False, 'businessId': args.business_id, 'error': err},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 1 if err and '没找到' in err else 2

    if args.raw:
        print(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            'ok': True,
            'businessId': body.get('businessId'),
            'processInstanceId': pid,
            'title': body.get('processInstanceTitle'),
            'status': body.get('status'),
            'result': body.get('processInstanceResult'),
            'finishTime': body.get('finishTime'),
            'formFieldCount': len(body.get('formValueVOS') or []),
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
