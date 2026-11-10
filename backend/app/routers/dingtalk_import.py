import json, uuid
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.auth import get_current_user
from app.models.user import User
router=APIRouter()
ROOT=Path('/dingtalk-import-queue')
class ApprovalImportRequest(BaseModel):
 approval_number: str
def latest():
 files=sorted(ROOT.glob('status-*.json'), key=lambda p:p.stat().st_mtime, reverse=True)
 return json.loads(files[0].read_text()) if files else {'status':'idle','total':0,'downloaded':0,'recognized':0,'invoices':0,'non_invoices':0,'skipped':0,'failed':0}
@router.get('/status')
async def status(_:User=Depends(get_current_user)): return latest()
@router.get('/approvals')
async def approvals(_:User=Depends(get_current_user)):
 p=ROOT/'approvals.json'
 return {'items': list(json.loads(p.read_text()).values()) if p.exists() else []}
@router.post('/run')
async def run(_:User=Depends(get_current_user)):
 ROOT.mkdir(parents=True,exist_ok=True); job=str(uuid.uuid4()); now=datetime.now().isoformat()
 # A manually triggered run is a reconciliation job: it scans the full
 # available reimbursement history instead of only the hourly window.
 (ROOT/f'job-{job}.json').write_text(json.dumps({'id':job,'requested_at':now,'lookback_days':365},ensure_ascii=False))
 state={'id':job,'status':'queued','total':0,'downloaded':0,'recognized':0,'invoices':0,'non_invoices':0,'skipped':0,'failed':0,'updated_at':now}
 (ROOT/f'status-{job}.json').write_text(json.dumps(state,ensure_ascii=False))
 return state
@router.post('/run-approval')
async def run_approval(payload: ApprovalImportRequest, _:User=Depends(get_current_user)):
 approval_number=payload.approval_number.strip()
 if len(approval_number) < 8 or not approval_number[:8].isdigit():
  raise HTTPException(status_code=400, detail='请输入有效的审批编号')
 ROOT.mkdir(parents=True,exist_ok=True); job=str(uuid.uuid4()); now=datetime.now().isoformat()
 (ROOT/f'job-{job}.json').write_text(json.dumps({'id':job,'requested_at':now,'approval_number':approval_number},ensure_ascii=False))
 state={'id':job,'status':'queued','approval_number':approval_number,'total':0,'downloaded':0,'recognized':0,'invoices':0,'non_invoices':0,'skipped':0,'failed':0,'updated_at':now}
 (ROOT/f'status-{job}.json').write_text(json.dumps(state,ensure_ascii=False))
 return state
