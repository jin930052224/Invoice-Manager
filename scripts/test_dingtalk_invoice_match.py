import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from dingtalk_invoice_match import choose_match, sheet_candidates, reconcile_requests
from dingtalk_reimbursement_mapper import HEADER


class ApprovalMatchTest(unittest.TestCase):
    def test_sheet_row_matches_number_and_amount(self):
        row = [''] * len(HEADER)
        row[HEADER.index('审批编号')] = '202610281706000420279'
        row[HEADER.index('创建人')] = '张三'
        row[HEADER.index('发票号')] = '26332000007709234926'
        row[HEADER.index('报销金额（元）')] = '5.86'
        candidates = sheet_candidates([HEADER, row])
        self.assertEqual(choose_match(candidates, '26332000007709234926', '5.86'),
                         ('202610281706000420279', '张三'))
        self.assertIsNone(choose_match(candidates, '26332000007709234926', '5.87'))

    def test_cross_approval_same_number_does_not_guess(self):
        candidates = {('26332000007709234926', '5.86'):
                      {('approval-a', '张三'), ('approval-b', '李四')}}
        self.assertIsNone(choose_match(candidates, '26332000007709234926', '5.86'))

    def test_sheet_match_updates_only_empty_metadata(self):
        row = [''] * len(HEADER)
        row[HEADER.index('审批编号')] = '202610281706000420279'
        row[HEADER.index('创建人')] = '张三'
        row[HEADER.index('发票号')] = '26332000007709234926'
        row[HEADER.index('报销金额（元）')] = '5.86'

        class Response:
            status_code = 200
            def __init__(self, data): self.data = data
            def raise_for_status(self): pass
            def json(self): return self.data

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'match-10.json'
            path.write_text(json.dumps({'invoice_id': 10,
                'invoice_number': '26332000007709234926', 'amount': '5.86'}))
            invoice = {'document_type': 'invoice', 'status': '待审核',
                       'invoice_number': '26332000007709234926',
                       'total_with_tax': '5.86', 'approval_instance_id': None, 'owner': None}
            with (patch('dingtalk_invoice_match.subprocess.check_output',
                        return_value=json.dumps({'cells': [[{'value': x} for x in HEADER],
                                                            [{'value': x} for x in row]]})),
                  patch('dingtalk_invoice_match.requests.get', return_value=Response(invoice)),
                  patch('dingtalk_invoice_match.requests.put', return_value=Response({})) as put):
                reconcile_requests([path], 'test-token')
            self.assertFalse(path.exists())
            self.assertEqual(put.call_args.kwargs['json'],
                             {'approval_instance_id': '202610281706000420279', 'owner': '张三'})


if __name__ == '__main__':
    unittest.main()
