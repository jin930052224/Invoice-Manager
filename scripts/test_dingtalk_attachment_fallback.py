import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import dingtalk_approval_import_worker as worker


ATTACHMENT = {
    'spaceId': '26341904053',
    'fileId': '227060250605',
    'fileName': 'T3出行电子发票.pdf',
    'fileSize': 4,
}


class StorageAttachmentFallbackTest(unittest.TestCase):
    @patch.object(worker, '_storage_operator_union_id', return_value='union /id')
    @patch.object(worker, '_app_credentials', return_value=('client', 'secret'))
    @patch.object(worker.subprocess, 'run')
    def test_storage_api_supplies_operator_union_id(self, run, credentials, union_id):
        run.return_value = Mock(returncode=0, stdout=json.dumps({'dentry': {'id': '1'}}))
        worker._storage_api('/v1.0/storage/spaces/1/dentries/2/query', {'option': {}})
        command = run.call_args.args[0]
        self.assertEqual(command[3], '/v1.0/storage/spaces/1/dentries/2/query')
        self.assertEqual(json.loads(command[command.index('--params') + 1]),
                         {'unionId': 'union /id'})

    @patch.object(worker.requests, 'get')
    @patch.object(worker, '_storage_api')
    def test_downloads_by_approval_space_and_file_id(self, api, get):
        api.side_effect = [
            {'dentry': {'id': ATTACHMENT['fileId'], 'spaceId': ATTACHMENT['spaceId'],
                        'name': ATTACHMENT['fileName']}},
            {'headerSignatureInfo': {'resourceUrls': ['https://example.test/file'],
                                     'headers': {'x-test': 'value'}}},
        ]
        get.return_value = Mock(content=b'%PDF')
        self.assertEqual(worker._download_attachment_storage(ATTACHMENT), b'%PDF')
        self.assertEqual(api.call_args_list[0].args[0],
                         '/v1.0/storage/spaces/26341904053/dentries/227060250605/query')
        get.assert_called_once_with('https://example.test/file',
                                    headers={'x-test': 'value'}, timeout=60)

    @patch.object(worker.requests, 'get')
    @patch.object(worker, '_storage_api')
    def test_rejects_mismatched_file_metadata_before_download(self, api, get):
        api.return_value = {'dentry': {'id': ATTACHMENT['fileId'],
                                       'spaceId': ATTACHMENT['spaceId'],
                                       'name': 'different.pdf'}}
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            worker._download_attachment_storage(ATTACHMENT)
        get.assert_not_called()

    @patch.object(worker, '_download_attachment_storage', return_value=b'%PDF')
    @patch.object(worker, '_download_attachment_user', side_effect=RuntimeError('userNotExist'))
    @patch.object(worker, '_download_attachment_app', side_effect=RuntimeError('userNotExist'))
    def test_departed_originator_falls_back_to_storage(self, app, user, storage):
        self.assertEqual(worker.download_attachment('instance', ATTACHMENT['fileId'],
                                                     ATTACHMENT), b'%PDF')
        storage.assert_called_once_with(ATTACHMENT)

    @patch.object(worker.subprocess, 'run')
    def test_historical_scan_waits_for_storage_permission(self, run):
        run.return_value = Mock(returncode=0, stdout=json.dumps({
            'data': {'items': [{'scopeValue': 'Storage.File.Read', 'authed': False}]}
        }))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(worker, 'HISTORICAL_RECONCILE_STATE',
                              Path(directory) / 'state.json'):
                self.assertIsNone(worker.historical_reconcile_job())
                self.assertFalse((Path(directory) / 'state.json').exists())

    @patch.object(worker.subprocess, 'run')
    def test_historical_scan_is_queued_once_per_week_after_permission(self, run):
        run.side_effect = [
            Mock(returncode=0, stdout=json.dumps({'data': {'items': [
                {'scopeValue': scope, 'authed': True}]}}))
            for scope in ('Storage.File.Read', 'qyapi_get_member')]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(worker, 'HISTORICAL_RECONCILE_STATE',
                              Path(directory) / 'state.json'):
                job = worker.historical_reconcile_job()
                self.assertEqual(job['lookback_days'], 365)
                self.assertIsNone(worker.historical_reconcile_job())
                self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
