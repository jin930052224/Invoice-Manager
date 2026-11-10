import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Input, Row, Space, Statistic, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, UserOutlined, ArrowLeftOutlined } from '@ant-design/icons';
import { Link, useSearchParams } from 'react-router-dom';
import { listInvoices, listPersons } from '../services/api';
import type { PersonGroup } from '../services/api';
import type { Invoice } from '../types/invoice';
import styles from './CompanyPage.module.css';

const money = (value: string | number) => Number(value).toLocaleString('zh-CN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export default function PersonPage() {
  const [params, setParams] = useSearchParams();
  const selectedKey = params.get('person');
  const [groups, setGroups] = useState<PersonGroup[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    listPersons()
      .then(result => { if (active) setGroups(result.items); })
      .catch(() => { if (active) setError('个人分类加载失败，请重试'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [version]);

  useEffect(() => {
    if (!selectedKey) return;
    let active = true;
    setDetailLoading(true);
    listInvoices({ company_key: selectedKey, page, page_size: 20 })
      .then(result => {
        if (active) {
          setInvoices(result.items);
          setTotal(result.total);
        }
      })
      .catch(() => { if (active) setError('个人发票明细加载失败，请重试'); })
      .finally(() => { if (active) setDetailLoading(false); });
    return () => { active = false; };
  }, [selectedKey, page, version]);

  const visible = useMemo(
    () => groups.filter(group => group.name.toLowerCase().includes(search.trim().toLowerCase())),
    [groups, search],
  );
  const selected = groups.find(group => group.key === selectedKey);
  const personReturnTo = `/persons?${params.toString()}`;
  const invoiceCount = groups.reduce((sum, group) => sum + group.invoice_count, 0);
  const totalAmount = groups.reduce((sum, group) => sum + Number(group.total_amount), 0);
  const missingCount = groups.reduce((sum, group) => sum + group.missing_amount_count, 0);
  const choose = (key: string) => {
    setPage(1);
    setParams({ person: key });
  };

  return <div className={styles.page}>
    <div className={styles.heading}>
      <div>
        <Typography.Title level={2} style={{ margin: 0 }}>个人归类</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
          按购买方个人姓名自动归类。填写了企业税号的购买方归入公司目录。
        </Typography.Paragraph>
      </div>
      <Button icon={<ReloadOutlined />} loading={loading} onClick={() => setVersion(value => value + 1)}>刷新</Button>
    </div>

    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => setVersion(value => value + 1)}>重试</Button>} />}

    <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
      <Col xs={24} sm={8}><Card><Statistic title="个人数量" value={groups.length} suffix="人" /></Card></Col>
      <Col xs={24} sm={8}><Card><Statistic title="发票总数" value={invoiceCount} suffix="张" /></Card></Col>
      <Col xs={24} sm={8}><Card><Statistic title="价税合计" value={totalAmount} precision={2} prefix="¥" />
        {missingCount > 0 && <Typography.Text type="secondary">另有 {missingCount} 张金额待补充</Typography.Text>}
      </Card></Col>
    </Row>

    {selectedKey ? <Card title={<Space wrap>
      <Button icon={<ArrowLeftOutlined />} onClick={() => { setPage(1); setParams({}); }}>全部个人</Button>
      <span>{selected?.name || '个人发票'}</span>
    </Space>}>
      {selected && <Typography.Paragraph type="secondary">
        {selected.invoice_count} 张 · 价税合计 ¥{money(selected.total_amount)}
        {selected.missing_amount_count > 0 ? `（${selected.missing_amount_count} 张金额待补充）` : ''}
      </Typography.Paragraph>}
      <Table<Invoice>
        rowKey="id"
        dataSource={invoices}
        loading={detailLoading}
        scroll={{ x: 850 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }}
        columns={[
          { title: '发票号码', dataIndex: 'invoice_number', render: (value, row) => <Link to={`/invoices/${row.id}`} state={{ ids: invoices.map(item => item.id), returnTo: personReturnTo, returnLabel: '返回个人' }}>{value || row.file_name}</Link> },
          { title: '开票日期', dataIndex: 'issue_date', render: value => value || '—' },
          { title: '销售方', dataIndex: 'seller_name', render: value => value || '—' },
          { title: '价税合计', dataIndex: 'total_with_tax', align: 'right', render: value => value == null ? '待补充' : `¥${money(value)}` },
          { title: '状态', dataIndex: 'status', render: value => <Tag>{value}</Tag> },
        ]}
      />
    </Card> : <Card title="个人目录" extra={<Input.Search
      aria-label="搜索个人姓名"
      placeholder="搜索个人姓名"
      allowClear
      value={search}
      onChange={event => setSearch(event.target.value)}
      style={{ maxWidth: 300 }}
    />}>
      <Table<PersonGroup>
        rowKey="key"
        loading={loading}
        dataSource={visible}
        pagination={{ pageSize: 15, showSizeChanger: false }}
        locale={{ emptyText: <Empty description={search ? '没有匹配的个人' : '暂无个人发票'} /> }}
        columns={[
          { title: '购买方个人', dataIndex: 'name', render: (value, row) => <Button type="link" style={{ padding: 0 }} onClick={() => choose(row.key)}><UserOutlined />{value}</Button> },
          { title: '发票数', dataIndex: 'invoice_count', sorter: (a, b) => a.invoice_count - b.invoice_count },
          { title: '价税合计', dataIndex: 'total_amount', align: 'right', sorter: (a, b) => Number(a.total_amount) - Number(b.total_amount), render: (value, row) => <span>¥{money(value)}{row.missing_amount_count > 0 && <div><Typography.Text type="secondary">{row.missing_amount_count} 张待补充</Typography.Text></div>}</span> },
          { title: '待审核', dataIndex: 'review_count', render: value => value ? <Tag color="orange">{value} 张</Tag> : '—' },
          { title: '操作', key: 'open', render: (_, row) => <Button onClick={() => choose(row.key)}>查看发票</Button> },
        ]}
      />
    </Card>}
  </div>;
}
