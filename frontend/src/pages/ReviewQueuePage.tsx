import { useEffect, useMemo, useState } from 'react';
import { Button, Card, Col, Empty, Popconfirm, Row, Space, Statistic, Table, Tag, Typography, message } from 'antd';
import { CheckOutlined, EyeOutlined, FileUnknownOutlined, ReloadOutlined } from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';
import { batchArchiveVouchers, batchConfirmInvoices, listInvoices } from '../services/api';
import type { Invoice } from '../types/invoice';
import { InvoiceStatus } from '../types/invoice';

export default function ReviewQueuePage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const result = await listInvoices({ page: 1, page_size: 100, status: InvoiceStatus.REVIEWING });
      setInvoices(result.items);
      setSelectedIds((current) => current.filter((id) => result.items.some((item) => item.id === id)));
    } catch {
      message.error('待审核列表加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const totals = useMemo(() => invoices.reduce((result, invoice) => ({
    amount: result.amount + Number(invoice.amount || 0),
    tax: result.tax + Number(invoice.tax_amount || 0),
    total: result.total + Number(invoice.total_with_tax || 0),
  }), { amount: 0, tax: 0, total: 0 }), [invoices]);

  const confirmMany = async (ids: number[]) => {
    if (!ids.length) return;
    setConfirming(true);
    try {
      const result = await batchConfirmInvoices(ids);
      if (result.failed) {
        message.warning(`已确认 ${result.success} 张；${result.failed} 张缺少必填信息，仍保留待审核`);
      } else {
        message.success(`已确认并锁定 ${result.success} 张发票`);
      }
      await refresh();
    } finally {
      setConfirming(false);
    }
  };

  const archiveMany = async (ids: number[]) => {
    if (!ids.length) return;
    setArchiving(true);
    try {
      const result = await batchArchiveVouchers(ids);
      message.success(result.message);
      await refresh();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '归档失败');
    } finally {
      setArchiving(false);
    }
  };

  const columns = [
    { title: '发票号码', dataIndex: 'invoice_number', render: (value: string | null) => value || '-' },
    { title: '审批编号', dataIndex: 'approval_instance_id', render: (value: string | null) => value || '-' },
    { title: '开票日期', dataIndex: 'issue_date', render: (value: string | null) => value || '-' },
    { title: '购买方', dataIndex: 'buyer_name', ellipsis: true, render: (value: string | null) => value || '-' },
    { title: '销售方', dataIndex: 'seller_name', ellipsis: true, render: (value: string | null) => value || '-' },
    { title: '价税合计', dataIndex: 'total_with_tax', render: (value: number | null) => value == null ? '-' : `¥${Number(value).toFixed(2)}` },
    { title: '归属人', dataIndex: 'owner', render: (value: string | null) => value || '-' },
    { title: '状态', dataIndex: 'status', render: () => <Tag color="gold">待审核</Tag> },
    {
      title: '操作',
      key: 'actions',
      render: (_: unknown, record: Invoice) => (
        <Space size="small">
          <Button
            type="link"
            icon={<EyeOutlined />}
            onClick={() => navigate(`/invoices/${record.id}`, {
              state: {
                ids: invoices.map((item) => item.id),
                returnTo: location.pathname,
                returnLabel: '返回待审核',
              },
            })}
          >
            审核
          </Button>
          <Popconfirm
            title="确认这是发票？"
            description="确认后将锁定发票内容。"
            okText="确认发票"
            cancelText="取消"
            onConfirm={() => confirmMany([record.id])}
          >
            <Button type="link" icon={<CheckOutlined />}>确认发票</Button>
          </Popconfirm>
          <Popconfirm
            title="标记为不是发票？"
            description="文件会保留在凭证归档，但从发票列表移除。"
            okText="归档到凭证"
            cancelText="取消"
            onConfirm={() => archiveMany([record.id])}
          >
            <Button type="link" danger icon={<FileUnknownOutlined />}>不是发票</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Typography.Title level={2} style={{ margin: 0 }}>待审核</Typography.Title>
        <Typography.Text type="secondary">集中核对识别结果。确认后锁定为发票；不是发票的附件会归档备份，不参与钉钉同步。</Typography.Text>
      </Space>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="待审核单据" value={invoices.length} suffix="张" /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="金额合计" value={totals.amount} precision={2} prefix="¥" /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="税额合计" value={totals.tax} precision={2} prefix="¥" /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="价税合计" value={totals.total} precision={2} prefix="¥" /></Card></Col>
      </Row>

      <Card style={{ marginTop: 16 }}>
        <Space style={{ marginBottom: 16 }} wrap>
          <Button icon={<ReloadOutlined />} onClick={refresh} loading={loading}>刷新</Button>
          <Popconfirm
            title={`确认选中的 ${selectedIds.length} 张发票？`}
            description="确认后将锁定发票内容。"
            okText="确认发票"
            cancelText="取消"
            onConfirm={() => confirmMany(selectedIds)}
            disabled={!selectedIds.length}
          >
            <Button type="primary" icon={<CheckOutlined />} loading={confirming} disabled={!selectedIds.length}>批量确认发票</Button>
          </Popconfirm>
          <Popconfirm
            title={`将选中的 ${selectedIds.length} 份归档为凭证？`}
            description="文件会保留在凭证归档，并从发票列表移除。"
            okText="归档到凭证"
            cancelText="取消"
            onConfirm={() => archiveMany(selectedIds)}
            disabled={!selectedIds.length}
          >
            <Button danger icon={<FileUnknownOutlined />} loading={archiving} disabled={!selectedIds.length}>批量标记不是发票</Button>
          </Popconfirm>
        </Space>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={invoices}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          rowSelection={{ selectedRowKeys: selectedIds, onChange: (keys) => setSelectedIds(keys as number[]) }}
          locale={{ emptyText: <Empty description="暂无待审核单据" /> }}
          scroll={{ x: 1200 }}
        />
      </Card>
    </div>
  );
}
