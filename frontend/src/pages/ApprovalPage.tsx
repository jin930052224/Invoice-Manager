import { useEffect, useMemo, useState } from 'react';
import { Card, Empty, Input, Space, Spin, Statistic, Switch, Table, Tag, Typography } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { listDingTalkApprovals, listInvoices } from '../services/api';
import type { Invoice } from '../types/invoice';

interface ApprovalRow {
  key: string;
  approval_number: string;
  owner?: string | null;
  status?: string;
  submitted_at?: string | null;
  count: number;
  total: number;
  invoice_ids: number[];
}

export default function ApprovalPage() {
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [approvalRecords, setApprovalRecords] = useState<Array<{ approval_number: string; owner?: string; status?: string; submitted_at?: string | null }>>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [showTerminated, setShowTerminated] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    const loadApprovals = async () => {
      try {
        const [firstPage, approvalResponse] = await Promise.all([
          listInvoices({ page: 1, page_size: 100 }),
          listDingTalkApprovals(),
        ]);
        const pageCount = Math.ceil(firstPage.total / firstPage.page_size);
        const remainingPages = await Promise.all(
          Array.from({ length: Math.max(0, pageCount - 1) }, (_, index) =>
            listInvoices({ page: index + 2, page_size: firstPage.page_size }),
          ),
        );
        setInvoices([firstPage, ...remainingPages].flatMap((page) => page.items));
        setApprovalRecords(approvalResponse.items);
      } finally {
        setLoading(false);
      }
    };
    void loadApprovals();
  }, []);

  const rows = useMemo<ApprovalRow[]>(() => {
    const grouped = invoices
      .filter((invoice) => invoice.approval_instance_id)
      .reduce<Record<string, ApprovalRow>>((result, invoice) => {
        const approvalNumber = invoice.approval_instance_id!;
        const row = result[approvalNumber] ?? {
          key: approvalNumber,
          approval_number: approvalNumber,
          count: 0,
          total: 0,
          owner: invoice.owner,
          status: undefined,
          submitted_at: null,
          invoice_ids: [],
        };
        row.count += 1;
        row.total += Number(invoice.total_with_tax || 0);
        row.invoice_ids.push(invoice.id);
        result[approvalNumber] = row;
        return result;
      }, {});

    approvalRecords.forEach((approval) => {
      if (!grouped[approval.approval_number]) {
        grouped[approval.approval_number] = {
          key: approval.approval_number,
          approval_number: approval.approval_number,
          count: 0,
          total: 0,
          owner: approval.owner,
          status: approval.status,
          submitted_at: approval.submitted_at,
          invoice_ids: [],
        };
      } else {
        grouped[approval.approval_number].status = approval.status;
        grouped[approval.approval_number].submitted_at = approval.submitted_at;
      }
    });

    return Object.values(grouped).sort((left, right) =>
      right.approval_number.localeCompare(left.approval_number),
    );
  }, [approvalRecords, invoices]);

  const filteredRows = useMemo(() => {
    const keyword = search.trim().toLocaleLowerCase();
    return rows.filter((row) => {
      if (!showTerminated && row.status === 'TERMINATED') return false;
      if (!keyword) return true;
      return row.approval_number.toLocaleLowerCase().includes(keyword)
        || (row.owner || '').toLocaleLowerCase().includes(keyword);
    });
  }, [rows, search, showTerminated]);

  return <div style={{ padding: 24 }}>
    <Typography.Title level={2}>审批编号归类</Typography.Title>
    <Typography.Paragraph type="secondary">按钉钉审批编号汇总发票及价税合计。</Typography.Paragraph>
    <Card>
      <Statistic title={search.trim() ? '匹配审批单数' : '审批单数'} value={filteredRows.length} />
    </Card>
    <Spin spinning={loading}>
      <Card style={{ marginTop: 16 }}>
        <Space style={{ marginBottom: 16 }} wrap>
          <Input
            allowClear
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            prefix={<SearchOutlined />}
            placeholder="搜索审批编号或归属人"
            style={{ width: 360, maxWidth: '100%' }}
          />
          <Switch checked={showTerminated} onChange={setShowTerminated} />
          <Typography.Text>显示已撤销审批（{rows.filter((row) => row.status === 'TERMINATED').length}）</Typography.Text>
        </Space>
        <Table<ApprovalRow>
          rowKey="approval_number"
          dataSource={filteredRows}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{ emptyText: <Empty description={search.trim() ? '未找到匹配的审批单' : '暂无关联审批编号的发票'} /> }}
          columns={[
            { title: '审批编号', dataIndex: 'approval_number' },
            { title: '提交时间', dataIndex: 'submitted_at', render: (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '-' },
            { title: '审批状态', dataIndex: 'status', render: (value?: string) => value === 'RUNNING' ? <Tag color="blue">审批中</Tag> : value === 'COMPLETED' ? <Tag color="green">已结束</Tag> : value === 'TERMINATED' ? <Tag>已撤销</Tag> : '-' },
            { title: '归属人', dataIndex: 'owner', render: (value) => value || '-' },
            { title: '发票数', dataIndex: 'count' },
            { title: '价税合计', dataIndex: 'total', render: (value: number) => `¥${value.toFixed(2)}` },
            {
              title: '查看发票',
              render: (_value, row) => (
                <a onClick={() => navigate(`/approvals/${encodeURIComponent(row.approval_number)}/invoices`)}>
                  查看 {row.count} 张
                </a>
              ),
            },
          ]}
        />
      </Card>
    </Spin>
  </div>;
}
