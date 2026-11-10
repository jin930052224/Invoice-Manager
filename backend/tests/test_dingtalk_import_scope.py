"""导入 worker 的「审批通过」守卫测试。

这个文件**刻意不 import app.***，因为 dingtalk_approval_import_worker.py 是宿主机脚本
（要 macOS Keychain 里的 DWS 登录态，容器里跑不了）。找到脚本就测，找不到就跳过。

在宿主机跑：
    cd backend && python -m pytest tests/test_dingtalk_import_scope.py -q
容器里（脚本在 /scripts 未挂载时）会 skip。
"""
import importlib.util
import os
from datetime import date
from pathlib import Path

import pytest

_CANDIDATES = [
    Path(os.environ.get("INVOICE_MANAGER_SCRIPTS", "")) / "dingtalk_approval_import_worker.py"
    if os.environ.get("INVOICE_MANAGER_SCRIPTS")
    else None,
    Path(__file__).resolve().parents[2] / "scripts" / "dingtalk_approval_import_worker.py",
    Path("/scripts/dingtalk_approval_import_worker.py"),
]


def _worker_path():
    for candidate in _CANDIDATES:
        if candidate and candidate.exists():
            return candidate
    return None


WORKER_PATH = _worker_path()

pytestmark = pytest.mark.skipif(
    WORKER_PATH is None,
    reason="宿主机脚本 dingtalk_approval_import_worker.py 不在当前环境（容器只挂了 backend/）",
)


@pytest.fixture(scope="module")
def worker():
    spec = importlib.util.spec_from_file_location("dingtalk_approval_import_worker", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rejects_invoice_number_mistyped_as_approval_number(worker):
    """真实事故：有人把 20 位发票号码填进 approval_number，前 8 位 26317000 解析失败，
    异常未捕获导致整个 worker 进程退出（last exit code = 1）。"""
    assert worker.approval_windows("26317000003387517253") is None


@pytest.mark.parametrize("bad", ["", "abc", "26317000", "00000000", "99999999"])
def test_rejects_malformed_approval_number(worker, bad):
    assert worker.approval_windows(bad) is None


def test_accepts_real_approval_number(worker):
    windows = worker.approval_windows("202609181416000253467")

    assert windows is not None
    assert windows[0][0].date() == date(2026, 9, 18)
    assert windows[0][1].date() == date(2026, 9, 19)


def test_approval_submission_time_uses_dingtalk_create_time(worker):
    value = worker.approval_submitted_at({'createTime': 1792997391000})
    assert value == '2026-10-26T14:49:51+08:00'
    assert worker.approval_submitted_at({'businessId': '202610261449000500099'}) is None


def test_rejected_approval_is_skipped(worker):
    """审批中的附件可预识别；只有最终通过后才能同步报销统计表。"""
    agreed = {"status": "COMPLETED", "processInstanceResult": "agree"}
    refused = {"status": "COMPLETED", "processInstanceResult": "refuse"}
    running = {"status": "RUNNING", "processInstanceResult": "agree"}

    assert worker.is_approved(agreed) is True
    assert worker.is_approved(refused) is False
    # 审批中可能已有中间节点的 agree，不能当最终通过。
    assert worker.is_approved(running) is False
    assert worker.should_import_attachments(running) is True
    assert worker.should_enqueue_order(running, True) is False
    assert worker.should_enqueue_order(agreed, True) is True
    assert worker.should_enqueue_order(agreed, False) is False
    assert worker.should_import_attachments(refused) is False
    assert worker.should_import_attachments({"status": "TERMINATED"}) is False
    assert worker.is_approved({}) is False
    assert worker.is_approved({"processInstanceResult": None}) is False


def test_worker_skips_concurrent_run(worker, monkeypatch, tmp_path):
    import fcntl

    monkeypatch.setattr(worker, 'ROOT', tmp_path)
    lock_path = tmp_path / '.worker.lock'
    monkeypatch.setattr(worker, 'WORKER_LOCK', lock_path)
    calls = []
    monkeypatch.setattr(worker, '_main_unlocked', lambda: calls.append('run'))

    with lock_path.open('a+') as active_lock:
        fcntl.flock(active_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker.main()
        assert calls == []

    worker.main()
    assert calls == ['run']


def test_successful_source_marker_is_written_immediately(worker, monkeypatch, tmp_path):
    marker = tmp_path / 'imported.json'
    monkeypatch.setattr(worker, 'STATE', marker)
    imported = set()

    worker.mark_imported(imported, 'approval:file')

    assert imported == {'approval:file'}
    assert __import__('json').loads(marker.read_text()) == ['approval:file']


# --- 压缩包解包（zip / rar） ---

def _zip_bytes(entries, utf8=True):
    """造一个 zip。utf8=False 模拟国内压缩工具：名字按 GBK 写、不打 UTF-8 标记。

    注意 zipfile 见到非 ASCII 名字会自动改写成 UTF-8 并置 0x800 标记，
    所以要子类化 ZipInfo 把 _encodeFilenameFlags 换掉，才能造出真正的“无标记 GBK 名”包。
    ZipInfo 用了 __slots__，实例上直接赋值会报 read-only。
    """
    import io
    import zipfile

    class LegacyZipInfo(zipfile.ZipInfo):
        def _encodeFilenameFlags(self):
            return self.filename.encode('cp437'), 0

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            info = LegacyZipInfo(name) if not utf8 else zipfile.ZipInfo(name)
            if not utf8:
                info.filename = info.filename.encode('gbk').decode('cp437')
            archive.writestr(info, payload)
    return buffer.getvalue()


def test_zip_entries_are_extracted(worker):
    payload = _zip_bytes([('高铁票1.pdf', b'%PDF-1.4 fake'),
                          ('瑞幸发票.pdf', b'%PDF-1.4 coffee')])

    entries, seen = worker.extract_archive(payload, 'zip')

    assert sorted(name for name, _ in entries) == ['瑞幸发票.pdf', '高铁票1.pdf']
    assert seen == 2


def test_zip_without_utf8_flag_is_decoded_as_gbk(worker):
    """国内压缩工具多数不打 UTF-8 标记，zipfile 会按 cp437 解出乱码，
    不转回来文件名会是「¸ßÌúÆ±1.pdf」，台账里就全是乱码。"""
    payload = _zip_bytes([('高铁票1.pdf', b'%PDF-1.4')], utf8=False)

    entries, _ = worker.extract_archive(payload, 'zip')

    assert [name for name, _ in entries] == ['高铁票1.pdf']


def test_zip_skips_non_invoice_and_macos_noise(worker):
    payload = _zip_bytes([('发票.pdf', b'%PDF-1.4'),
                          ('清单.xlsx', b'PK fake'),
                          ('__MACOSX/._发票.pdf', b'junk'),
                          ('.DS_Store', b'junk')])

    entries, _ = worker.extract_archive(payload, 'zip')

    assert [name for name, _ in entries] == ['发票.pdf']


def test_corrupt_zip_does_not_raise(worker):
    """坏包/加密包只记日志跳过，不能把整轮扫描带崩（之前 worker 就是这么挂的）。"""
    entries, seen = worker.extract_archive(b'not a zip at all', 'zip')

    assert entries == []
    assert seen == 1


def test_unsupported_archive_type_returns_empty(worker):
    assert worker.extract_archive(b'whatever', '7z') == ([], 0)


# --- 上传限流退避 ---

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _http_error(status_code):
    import requests

    error = requests.HTTPError(f'{status_code}')
    error.response = _FakeResponse(status_code)
    return error


def test_upload_retries_on_rate_limit_then_succeeds(worker, monkeypatch):
    """上传接口限流 10/分钟。上次回补 146 下载只成功 39，129 张全是 429 被误记成 failed。
    这里必须退避重试，不能当成文件有问题。"""
    calls = []

    def fake_upload_one(*_args, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise _http_error(429)

    monkeypatch.setattr(worker, 'upload_one', fake_upload_one)
    monkeypatch.setattr(worker, 'login_token', lambda _cache: 'test-token')
    monkeypatch.setattr(worker.time, 'sleep', lambda _s: None)
    throttle = worker.UploadThrottle(min_interval=0)

    worker.upload_with_retry({}, throttle, b'data', 'a.pdf', {'businessId': 'x'}, '张三', None)

    assert len(calls) == 3


def test_upload_gives_up_after_max_rate_limit_retries(worker, monkeypatch):
    import requests

    calls = []

    def fake_upload_one(*_args, **_kwargs):
        calls.append(1)
        raise _http_error(429)

    monkeypatch.setattr(worker, 'upload_one', fake_upload_one)
    monkeypatch.setattr(worker, 'login_token', lambda _cache: 'test-token')
    monkeypatch.setattr(worker.time, 'sleep', lambda _s: None)
    throttle = worker.UploadThrottle(min_interval=0)

    with pytest.raises(requests.HTTPError):
        worker.upload_with_retry({}, throttle, b'data', 'a.pdf', {'businessId': 'x'}, '张三', None)

    assert len(calls) == worker.MAX_RATE_LIMIT_RETRIES + 1


def test_upload_does_not_retry_on_non_rate_limit_error(worker, monkeypatch):
    """400 之类的真错误要立刻抛出并记 failed，不能白等 5 次退避。"""
    import requests

    calls = []

    def fake_upload_one(*_args, **_kwargs):
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(worker, 'upload_one', fake_upload_one)
    monkeypatch.setattr(worker, 'login_token', lambda _cache: 'test-token')
    monkeypatch.setattr(worker.time, 'sleep', lambda _s: None)

    with pytest.raises(requests.HTTPError):
        worker.upload_with_retry({}, worker.UploadThrottle(min_interval=0), b'd', 'a.pdf',
                                 {'businessId': 'x'}, '张三', None)

    assert len(calls) == 1


def test_upload_returns_created_record(worker, monkeypatch):
    created = {'id': 648, 'status': 'success'}
    monkeypatch.setattr(worker, 'upload_one', lambda *_args, **_kwargs: created)
    monkeypatch.setattr(worker, 'login_token', lambda _cache: 'test-token')

    result = worker.upload_with_retry(
        {}, worker.UploadThrottle(min_interval=0), b'd', 'a.pdf',
        {'businessId': 'x'}, '张三', None)

    assert result == created


def test_wait_for_records_ignores_archived_voucher_uploaded_status(worker, monkeypatch):
    records = [{'id': 1, 'status': '已上传', 'document_type': 'voucher'}]
    monkeypatch.setattr(worker, 'fetch_approval_records', lambda *_args, **_kwargs: records)
    monkeypatch.setattr(worker, 'login_token', lambda _cache: 'token')

    ready, found = worker.wait_for_approval_records_ready({}, '202609181416000253467', timeout=0)

    assert ready is True
    assert found == records


def test_wait_for_records_defers_invoice_processing(worker, monkeypatch):
    records = [{'id': 2, 'status': '解析中', 'document_type': 'invoice'}]
    monkeypatch.setattr(worker, 'fetch_approval_records', lambda *_args, **_kwargs: records)
    monkeypatch.setattr(worker, 'login_token', lambda _cache: 'token')

    ready, found = worker.wait_for_approval_records_ready({}, '202609181416000253467', timeout=0)

    assert ready is False
    assert found == records
