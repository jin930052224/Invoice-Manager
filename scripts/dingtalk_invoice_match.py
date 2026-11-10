"""Match parsed invoices to DingTalk reimbursement rows by number AND amount."""

import json
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

from dingtalk_reimbursement_mapper import SEM_INDEX, normalize_amount, resolve_column_map
from dingtalk_sync_worker import DWS, NODE, SHEET_ID, READ_RANGE, _row_values

API = 'http://127.0.0.1:18841/api'


def sheet_candidates(rows):
    if not rows:
        raise ValueError('钉钉报销表为空')
    columns = resolve_column_map(rows[0])
    def cell(row, name):
        col = columns[SEM_INDEX[name]]
        return row[col].strip() if col < len(row) else ''
    approval = owner = ''
    matches = {}
    for row in rows[1:]:
        approval = cell(row, '审批编号') or approval
        owner = cell(row, '创建人') or owner
        amount = normalize_amount(cell(row, '报销金额（元）'))
        if not approval or not owner or not amount:
            continue
        for number in set(re.findall(r'(?<!\d)\d{8,20}(?!\d)', cell(row, '发票号'))):
            matches.setdefault((number, amount), set()).add((approval, owner))
    return matches


def choose_match(candidates, invoice_number, amount, current_approval=''):
    try:
        normalized_amount = format(Decimal(str(amount)).normalize(), 'f')
    except (InvalidOperation, ValueError):
        return None
    options = candidates.get((str(invoice_number).strip(), normalized_amount), set())
    if current_approval:
        options = {item for item in options if item[0] == current_approval}
    approvals = {item[0] for item in options}
    owners = {item[1] for item in options}
    return next(iter(options)) if len(approvals) == len(owners) == 1 else None


def reconcile_requests(paths: list[Path], token: str) -> None:
    raw = json.loads(subprocess.check_output(
        [DWS, 'sheet', 'range', 'read', '--node', NODE, '--sheet-id', SHEET_ID,
         '--range', READ_RANGE, '--format', 'json'], text=True, timeout=120))
    if raw.get('error') or not isinstance(raw.get('cells'), list):
        raise RuntimeError('读取钉钉报销表失败')
    candidates = sheet_candidates([_row_values(row) for row in raw['cells']])
    headers = {'Authorization': f'Bearer {token}'}
    for path in paths:
        try:
            request = json.loads(path.read_text(encoding='utf-8'))
            invoice_id = int(request['invoice_id'])
            response = requests.get(f'{API}/invoices/{invoice_id}', headers=headers, timeout=15)
            if response.status_code == 404:
                path.unlink(missing_ok=True)
                continue
            response.raise_for_status()
            invoice = response.json()
            if invoice.get('document_type') != 'invoice' or invoice.get('status') == '已确认':
                path.unlink(missing_ok=True)
                continue
            if (invoice.get('invoice_number') != request.get('invoice_number')
                    or normalize_amount(invoice.get('total_with_tax')) != normalize_amount(request.get('amount'))):
                path.unlink(missing_ok=True)
                continue
            current_approval = (invoice.get('approval_instance_id') or '').strip()
            match = choose_match(candidates, invoice['invoice_number'], invoice['total_with_tax'], current_approval)
            if not match:
                continue
            approval, owner = match
            # An approval imported from DingTalk may not have reached the sheet yet.
            # The sheet is evidence for this match; never overwrite existing metadata.
            patch = {}
            if not current_approval:
                patch['approval_instance_id'] = approval
            if not invoice.get('owner'):
                patch['owner'] = owner
            if patch:
                updated = requests.put(f'{API}/invoices/{invoice_id}', json=patch,
                                        headers=headers, timeout=15)
                updated.raise_for_status()
                print(f'已从钉钉报销表匹配发票 {invoice_id} → 审批 {approval}')
            path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, requests.RequestException) as exc:
            print(f'发票匹配任务 {path.name} 暂未完成：{exc}')
