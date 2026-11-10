#!/usr/bin/env python3
"""完整 Excel 审批清单 → 钉钉附件导入 → 后台识别 → 《报销统计表》全量扩展。

默认只生成执行计划；加 --apply 才会创建单审批任务并串行调用现有 worker。
任务状态和最终汇总落在数据目录 audit/<日期>/，可中断后用 --resume 继续。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path('/Volumes/D 盘/Codex/发票管理系统')
IMPORT_ROOT = DATA_ROOT / 'dingtalk-import-queue'
SYNC_ROOT = DATA_ROOT / 'dingtalk-sync-queue'
DEFAULT_SOURCE = DATA_ROOT / 'audit/2026-09-29/全量同步预检-Excel审批汇总.json'
IMPORT_WORKER = PROJECT_ROOT / 'scripts/dingtalk_approval_import_worker.py'
SYNC_WORKER = PROJECT_ROOT / 'scripts/dingtalk_sync_worker.py'
MANAGED_PYTHON = Path('/Users/king/.workbuddy/binaries/python/envs/default/bin/python')
PYTHON = MANAGED_PYTHON if MANAGED_PYTHON.exists() else Path(sys.executable)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def approved_source(source: Path) -> tuple[list[str], dict[str, dict]]:
    body = read_json(source)
    if not isinstance(body, dict):
        raise RuntimeError(f'无法读取审批清单: {source}')
    ids = [str(value).strip() for value in body.get('approved_ids') or []]
    invalid = [value for value in ids if len(value) != 21 or not value.isdigit()]
    if invalid:
        raise RuntimeError(f'审批清单含非法编号: {invalid[:5]}')
    if len(ids) != len(set(ids)):
        raise RuntimeError('审批清单存在重复编号，拒绝直接创建批次')
    details = {
        str(item.get('审批编号') or '').strip(): item
        for item in body.get('orders_detail') or []
        if str(item.get('审批编号') or '').strip()
    }
    return ids, details


def fallback_block_from_excel(item: dict) -> dict:
    """审批接口因超出时间戳保留期不可查时，用Excel审批级字段补台账头行。

    附件仍然不能凭Excel伪造；这里只补真实导出里已有的审批字段，并明确标记来源。
    """
    return {
        'business_id': str(item.get('审批编号') or '').strip(),
        'process_instance_id': '',
        'company': str(item.get('公司名称') or '').strip(),
        'has_invoice': str(item.get('是否有发票') or '').strip(),
        'total': str(item.get('合计金额') or '').strip(),
        'total_upper': str(item.get('大写') or '').strip(),
        'recipient': str(item.get('收款账户') or '').strip(),
        'created_at': str(item.get('创建时间') or '').strip(),
        'creator': str(item.get('创建人') or '').strip(),
        'result': str(item.get('审批结果') or '').strip(),
        'status': str(item.get('审批状态') or '').strip(),
        'finished_at': str(item.get('完成时间') or '').strip(),
        'form_invoice_numbers': [],
        'rows': list(item.get('明细') or []),
        'row_count': len(item.get('明细') or []),
        'fallback_source': 'excel-export',
    }


def enqueue_excel_fallback(business_id: str, item: dict) -> Path:
    path = SYNC_ROOT / f'order-{business_id}.json'
    write_json(path, {
        'kind': 'reimbursement_order',
        'business_id': business_id,
        'queued_at': datetime.now(TZ).isoformat(),
        'block': fallback_block_from_excel(item),
        'fallback_source': 'excel-export',
    })
    return path


def existing_import_job(business_id: str) -> Path | None:
    for directory in (IMPORT_ROOT, IMPORT_ROOT / 'processed', IMPORT_ROOT / 'cancelled'):
        for path in directory.glob('job-*.json'):
            body = read_json(path, {})
            if str(body.get('approval_number') or '').strip() == business_id:
                return path
    return None


def create_job(business_id: str, batch_id: str) -> Path:
    job_id = f'full-{batch_id}-{business_id}'
    path = IMPORT_ROOT / f'job-{job_id}.json'
    payload = {
        'id': job_id,
        'requested_at': datetime.now(TZ).isoformat(),
        'approval_number': business_id,
        'batch_id': batch_id,
        'upload_min_interval': 7.0,
    }
    write_json(path, payload)
    return path


def run_worker(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(PYTHON), str(script)], text=True, capture_output=True)


def status_for_job(job_id: str) -> dict:
    return read_json(IMPORT_ROOT / f'status-{job_id}.json', {}) or {}


def queue_health() -> dict:
    return {
        'import_jobs': len(list(IMPORT_ROOT.glob('job-*.json'))),
        'sync_jobs': len(list(SYNC_ROOT.glob('order-*.json'))),
        'worker_error_bytes': (SYNC_ROOT / 'worker-error.log').stat().st_size
        if (SYNC_ROOT / 'worker-error.log').exists() else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='全量报销台账受控批次')
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--apply', action='store_true', help='创建任务并实际执行；默认只生成计划')
    parser.add_argument('--resume', action='store_true', help='复用今天已有批次状态继续')
    parser.add_argument('--limit', type=int, default=0, help='仅处理前 N 张，0=全部')
    args = parser.parse_args()

    ids, source_details = approved_source(args.source)
    if args.limit > 0:
        ids = ids[:args.limit]
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    batch_id = datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
    audit_dir = DATA_ROOT / 'audit' / today
    state_path = audit_dir / '全量台账批次状态.json'
    plan_path = audit_dir / '全量台账批次计划.json'
    report_path = audit_dir / '全量台账批次报告.json'

    old_state = read_json(state_path, {}) if (args.resume or args.apply) else {}
    completed = set(old_state.get('completed_ids') or [])
    failed = dict(old_state.get('failed') or {})
    if args.resume and failed:
        ids = [business_id for business_id in ids if business_id in failed]
    # 失败项必须重试，不能既在 completed 又留在 failed。历史状态可能因 launchd
    # 竞争造成这种交叉，先以 failed 为准移出 completed。
    completed.difference_update(failed)
    plan = {
        'generated_at': datetime.now(TZ).isoformat(),
        'source': str(args.source),
        'approved_orders': len(ids),
        'mode': 'apply' if args.apply else 'dry-run',
        'already_completed': len(completed & set(ids)),
        'queue_health': queue_health(),
        'approval_ids': ids,
        'rules': {
            'approved_only': True,
            'business_duplicate_blocking': False,
            'attachment_key_idempotency': True,
            'wait_for_backend_terminal_state': True,
            'sync_by_invoice_id': True,
        },
    }
    write_json(plan_path, plan)
    if not args.apply:
        print(json.dumps({'ok': True, 'plan': str(plan_path), **plan}, ensure_ascii=False, indent=2))
        return 0

    running = {
        'batch_id': batch_id,
        'started_at': datetime.now(TZ).isoformat(),
        'source': str(args.source),
        'total': len(ids),
        'completed_ids': sorted(completed),
        'failed': failed,
    }
    write_json(state_path, running)

    for index, business_id in enumerate(ids, start=1):
        if business_id in completed:
            continue
        root_jobs = list(IMPORT_ROOT.glob('job-*.json'))
        if root_jobs:
            failed[business_id] = f'导入队列根目录非空，暂停批次: {[p.name for p in root_jobs[:3]]}'
            break

        # 历史 cancelled 目录里混有旧版本把 completed 任务误归档进去的文件，
        # 本批次以当前审批详情和 imported.json 为准重跑；不要因旧路径名直接跳过。
        job_path = create_job(business_id, batch_id)
        job_id = read_json(job_path, {})['id']
        print(f'[{index}/{len(ids)}] {business_id}')
        imported = run_worker(IMPORT_WORKER)
        if imported.returncode != 0:
            failed[business_id] = f'import worker rc={imported.returncode}: {(imported.stderr or imported.stdout)[-500:]}'
        else:
            status = status_for_job(job_id)
            if status.get('status') != 'completed':
                failed[business_id] = f"导入状态异常: {status.get('status')} {status.get('reason', '')}"
            elif status.get('orders_deferred_processing'):
                failed[business_id] = '后台识别等待超时，台账同步已延后'
            else:
                if status.get('scanned_approvals') == 0 and status.get('orders_queued') == 0:
                    item = source_details.get(business_id)
                    if item:
                        enqueue_excel_fallback(business_id, item)
                    else:
                        failed[business_id] = '审批接口查不到，且Excel预检里没有审批详情'
                        item = None
                else:
                    item = True
                order_path = SYNC_ROOT / f'order-{business_id}.json'
                if item is None:
                    running.update({
                        'updated_at': datetime.now(TZ).isoformat(),
                        'completed_ids': sorted(completed), 'failed': failed,
                        'last_business_id': business_id, 'queue_health': queue_health(),
                    })
                    write_json(state_path, running)
                    continue
                completed_report = SYNC_ROOT / 'completed' / f'order-{business_id}.json'
                deadline = time.monotonic() + 180
                sync_error = ''
                while order_path.exists() and time.monotonic() < deadline:
                    synced = run_worker(SYNC_WORKER)
                    if synced.returncode != 0:
                        sync_error = (synced.stderr or synced.stdout)[-500:]
                        # launchd 并发已可能先成功消费；只要根 job 消失且 completed
                        # 报告存在，就按幂等成功收敛，不能把 unlink 竞态当业务失败。
                        if not order_path.exists() and completed_report.exists():
                            sync_error = ''
                            break
                    time.sleep(1)
                if order_path.exists():
                    failed[business_id] = f'同步任务180秒未消费: {order_path}'
                elif completed_report.exists():
                    report = read_json(completed_report, {}) or {}
                    if report.get('success'):
                        completed.add(business_id)
                        failed.pop(business_id, None)
                    else:
                        failed[business_id] = f'同步报告失败: {report.get("error", report)}'
                elif sync_error:
                    failed[business_id] = f'sync worker rc!=0: {sync_error}'
                else:
                    failed[business_id] = '同步任务消失但未找到completed报告'

        running.update({
            'updated_at': datetime.now(TZ).isoformat(),
            'completed_ids': sorted(completed),
            'failed': failed,
            'last_business_id': business_id,
            'queue_health': queue_health(),
        })
        write_json(state_path, running)
        time.sleep(0.2)

    report = {
        **running,
        'finished_at': datetime.now(TZ).isoformat(),
        'completed': len(completed & set(ids)),
        'failed_count': len([key for key in failed if key in set(ids)]),
        'remaining': len(set(ids) - completed - set(failed)),
        'queue_health': queue_health(),
    }
    write_json(report_path, report)
    print(json.dumps({'ok': report['failed_count'] == 0 and report['remaining'] == 0,
                      'report': str(report_path), **report}, ensure_ascii=False, indent=2))
    return 0 if report['failed_count'] == 0 and report['remaining'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
