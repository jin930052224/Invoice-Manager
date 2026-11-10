import { useEffect, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Row, Space, Spin, Statistic, Table, Tag, Typography, message } from 'antd';
import { ArrowLeftOutlined, EyeOutlined } from '@ant-design/icons';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { checkDuplicates, listInvoices, startDingTalkApprovalNumberImport } from '../services/api';
import type { Invoice } from '../types/invoice';

export default function ApprovalInvoiceListPage() {
  const { approvalNumber } = useParams<{ approvalNumber: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [duplicateIds, setDuplicateIds] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const syncApproval = async () => { if (!approvalNumber) return; setSyncing(true); try { await startDingTalkApprovalNumberImport(approvalNumber); message.success('已提交该审批单附件扫描任务'); } catch { message.error('扫描任务提交失败'); } finally { setSyncing(false); } };

  useEffect(() => {
    if (!approvalNumber) return;
    setLoading(true);
    Promise.allSettled([
      listInvoices({ page_size: 100, approval_number: approvalNumber }),
      checkDuplicates(),
    ])
      .then(([invoiceResult, duplicateResult]) => {
        if (invoiceResult.status === 'fulfilled') setInvoices(invoiceResult.value.items);
        if (duplicateResult.status === 'fulfilled') {
          setDuplicateIds(new Set(duplicateResult.value.groups.flatMap((group) => group.invoices.map((invoice) => invoice.id))));
        } else {
          message.error('查重失败，请到发票列表手动查重');
        }
      })
      .finally(() => setLoading(false));
  }, [approvalNumber]);

  const statistics = invoices.reduce((result, invoice) => ({
    amount: result.amount + Number(invoice.amount || 0),
    taxAmount: result.taxAmount + Number(invoice.tax_amount || 0),
    totalWithTax: result.totalWithTax + Number(invoice.total_with_tax || 0),
  }), { amount: 0, taxAmount: 0, totalWithTax: 0 });
  const columns = [
    { title: '发票号码', dataIndex: 'invoice_number', render: (value: string | null, record: Invoice) => <Space>{value || '-'}{duplicateIds.has(record.id) && <Tag color="red">号码重复</Tag>}</Space> },
    { title: '开票日期', dataIndex: 'issue_date', render: (value: string | null) => value || '-' },
    { title: '购买方', dataIndex: 'buyer_name', render: (value: string | null) => value || '-' },
    { title: '销售方', dataIndex: 'seller_name', render: (value: string | null) => value || '-' },
    { title: '价税合计', dataIndex: 'total_with_tax', render: (value: number | null) => value == null ? '-' : `¥${Number(value).toFixed(2)}` },
    { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === '已确认' ? 'green' : 'default'}>{value}</Tag> },
    { title: '操作', key: 'action', render: (_: unknown, record: Invoice) => <Button type="link" icon={<EyeOutlined />} onClick={() => navigate(`/invoices/${record.id}`, { state: { ids: invoices.map((item) => item.id), returnTo: location.pathname, returnLabel: '返回审批发票' } })}>详情</Button> },
  ];

  return <div style={{ padding: 24 }}>
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/approvals')}>返回审批编号归类</Button>
      <Typography.Title level={2} style={{ margin: 0 }}>审批发票</Typography.Title>
      <Typography.Text type="secondary">审批编号：{approvalNumber}</Typography.Text>
      <Button onClick={syncApproval} loading={syncing}>扫描该审批单附件</Button>
      <Typography.Text type="secondary">审批单内的发票汇总</Typography.Text>
    </Space>
    {invoices.some((invoice) => duplicateIds.has(invoice.id)) && <Alert style={{ marginTop: 16 }} type="warning" showIcon message="发现相同发票号，请在审批通过前核对是否重复报销。" />}
    <Row gutter={16} style={{ marginTop: 16 }}>
      <Col xs={24} sm={12} lg={6}><Card><Statistic title="发票总数" value={invoices.length} suffix="张" /></Card></Col>
      <Col xs={24} sm={12} lg={6}><Card><Statistic title="金额合计" value={statistics.amount} precision={2} prefix="¥" /></Card></Col>
      <Col xs={24} sm={12} lg={6}><Card><Statistic title="税额合计" value={statistics.taxAmount} precision={2} prefix="¥" /></Card></Col>
      <Col xs={24} sm={12} lg={6}><Card><Statistic title="价税合计" value={statistics.totalWithTax} precision={2} prefix="¥" /></Card></Col>
    </Row>
    <Card style={{ marginTop: 16 }}>
      <Spin spinning={loading}>
        <Table rowKey="id" dataSource={invoices} columns={columns} pagination={false} locale={{ emptyText: <Empty description="该审批单暂无发票" /> }} />
      </Spin>
    </Card>
  </div>;
}
