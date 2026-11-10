import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, DatePicker, Empty, Input, message, Row, Space, Statistic, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, BankOutlined, ArrowLeftOutlined, DownloadOutlined } from '@ant-design/icons';
import type { Dayjs } from 'dayjs';
import { Link, useSearchParams } from 'react-router-dom';
import { downloadInvoiceExport, listCompanies, listInvoices } from '../services/api';
import type { CompanyGroup } from '../services/api';
import type { Invoice } from '../types/invoice';
import styles from './CompanyPage.module.css';

const money = (n: string | number) => Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const companyName = (c: CompanyGroup) => c.name || (c.tax_id ? '公司名称待补充' : '未归类');
const { RangePicker } = DatePicker;

export default function CompanyPage() {
  const [params, setParams] = useSearchParams();
  const selectedKey = params.get('company');
  const [groups, setGroups] = useState<CompanyGroup[]>([]);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    listCompanies().then(r => { if (active) setGroups(r.items); })
      .catch(() => { if (active) setError('公司分类加载失败，请重试'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [version]);

  useEffect(() => {
    if (!selectedKey) return;
    let active = true;
    setDetailLoading(true); setDetailError(''); setInvoices([]);
    listInvoices({
      company_key: selectedKey,
      page,
      page_size: 20,
      start_date: dateRange?.[0]?.format('YYYY-MM-DD'),
      end_date: dateRange?.[1]?.format('YYYY-MM-DD'),
    })
      .then(r => { if (active) { setInvoices(r.items); setTotal(r.total); } })
      .catch(() => { if (active) { setDetailError('发票明细加载失败，请重试'); setTotal(0); } })
      .finally(() => { if (active) setDetailLoading(false); });
    return () => { active = false; };
  }, [selectedKey, page, version, dateRange]);

  const visible = useMemo(() => groups.filter(c => `${companyName(c)} ${c.tax_id || ''}`.toLowerCase().includes(search.trim().toLowerCase())), [groups, search]);
  const selected = groups.find(c => c.key === selectedKey);
  const companyReturnTo = `/companies?${params.toString()}`;
  const count = groups.reduce((sum, c) => sum + c.invoice_count, 0);
  const amount = groups.reduce((sum, c) => sum + Number(c.total_amount), 0);
  const missing = groups.reduce((sum, c) => sum + c.missing_amount_count, 0);
  const choose = (key: string) => { setPage(1); setParams({ company: key }); };
  const exportCompany = async () => {
    if (!selectedKey || !selected) return;
    const exportParams = new URLSearchParams({ company_key: selectedKey });
    const start = dateRange?.[0]?.format('YYYY-MM-DD');
    const end = dateRange?.[1]?.format('YYYY-MM-DD');
    if (start) exportParams.set('start_date', start);
    if (end) exportParams.set('end_date', end);
    const period = start && end ? `${start}_${end}` : '全部时间';
    const safeName = companyName(selected).replace(/[\\/:*?"<>|]/g, '_');
    setExporting(true);
    try {
      await downloadInvoiceExport('excel', exportParams, `${safeName}_${period}.xlsx`);
      message.success('公司发票导出成功');
    } catch {
      message.error('导出失败，请重试');
    } finally {
      setExporting(false);
    }
  };

  return <div className={styles.page}>
    <div className={styles.heading}>
      <div><Typography.Title level={2} style={{ margin: 0 }}>公司归类</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>按购买方公司自动归类，同一税号合并展示。修改发票的购买方信息后，分类随之更新。</Typography.Paragraph></div>
      <Button icon={<ReloadOutlined />} onClick={() => setVersion(v => v + 1)} loading={loading}>刷新</Button>
    </div>
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => setVersion(v => v + 1)}>重试</Button>} />}
    <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
      <Col xs={24} sm={8}><Card><Statistic title="公司分类（含未归类）" value={groups.length} suffix="类" /></Card></Col>
      <Col xs={24} sm={8}><Card><Statistic title="发票总数" value={count} suffix="张" /></Card></Col>
      <Col xs={24} sm={8}><Card><Statistic title="价税合计" value={amount} precision={2} prefix="¥" />{missing > 0 && <Typography.Text type="secondary">另有 {missing} 张金额待补充</Typography.Text>}</Card></Col>
    </Row>
    {selectedKey ? <Card title={<Space wrap><Button icon={<ArrowLeftOutlined />} onClick={() => { setPage(1); setDateRange(null); setParams({}); }}>全部公司</Button><span>{selected ? companyName(selected) : '公司发票'}</span></Space>} extra={<Space wrap>
      <RangePicker
        value={dateRange}
        onChange={value => { setPage(1); setDateRange(value); }}
        format="YYYY-MM-DD"
        allowClear
        placeholder={['开始日期', '结束日期']}
      />
      <Button type="primary" icon={<DownloadOutlined />} loading={exporting} onClick={exportCompany}>导出该公司</Button>
    </Space>}>
      {selected && <Typography.Paragraph type="secondary">税号：{selected.tax_id || '未填写'} · {selected.invoice_count} 张 · 价税合计 ¥{money(selected.total_amount)}{selected.missing_amount_count > 0 ? `（${selected.missing_amount_count} 张金额待补充）` : ''}</Typography.Paragraph>}
      {detailError && <Alert type="error" message={detailError} action={<Button onClick={() => setVersion(v => v + 1)}>重试</Button>} />}
      <Table<Invoice> rowKey="id" dataSource={invoices} loading={detailLoading} scroll={{ x: 850 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }}
        columns={[
          { title: '发票号码', dataIndex: 'invoice_number', render: (v, r) => <Link to={`/invoices/${r.id}`} state={{ ids: invoices.map(item => item.id), returnTo: companyReturnTo, returnLabel: '返回公司' }}>{v || r.file_name}</Link> },
          { title: '开票日期', dataIndex: 'issue_date', render: v => v || '—' },
          { title: '销售方', dataIndex: 'seller_name', render: v => v || '—' },
          { title: '价税合计', dataIndex: 'total_with_tax', align: 'right', render: v => v == null ? '待补充' : `¥${money(v)}` },
          { title: '状态', dataIndex: 'status', render: v => <Tag>{v}</Tag> },
        ]} />
    </Card> : <Card title="公司目录" extra={<Input.Search aria-label="搜索公司或税号" placeholder="搜索公司名称或税号" allowClear value={search} onChange={e => setSearch(e.target.value)} style={{ maxWidth: 300 }} />}>
      <Table<CompanyGroup> rowKey="key" loading={loading} dataSource={visible} scroll={{ x: 820 }} pagination={{ pageSize: 15, showSizeChanger: false }}
        locale={{ emptyText: <Empty description={search ? '没有匹配的公司' : '暂无发票，上传后自动归类'} /> }}
        columns={[
          { title: '购买方公司', dataIndex: 'name', render: (_, c) => <Button type="link" style={{ padding: 0 }} onClick={() => choose(c.key)}><BankOutlined />{companyName(c)}</Button> },
          { title: '纳税人识别号', dataIndex: 'tax_id', render: v => v || '未填写' },
          { title: '发票数', dataIndex: 'invoice_count', sorter: (a,b) => a.invoice_count-b.invoice_count },
          { title: '价税合计', dataIndex: 'total_amount', align: 'right', sorter: (a,b) => Number(a.total_amount)-Number(b.total_amount), render: (v,c) => <span>¥{money(v)}{c.missing_amount_count > 0 && <div><Typography.Text type="secondary">{c.missing_amount_count} 张待补充</Typography.Text></div>}</span> },
          { title: '待审核', dataIndex: 'review_count', render: v => v ? <Tag color="orange">{v} 张</Tag> : '—' },
          { title: '操作', key: 'open', render: (_,c) => <Button onClick={() => choose(c.key)}>查看发票</Button> },
        ]} />
    </Card>}
  </div>;
}
