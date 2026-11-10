import { useState, useEffect, useMemo } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import {
  Card,
  Table,
  Tag,
  Button,
  Space,
  Select,
  Input,
  message,
  Popconfirm,
  Modal,
  Statistic,
  Row,
  Col,
  Empty,
  Alert,
} from 'antd';
import {
  EyeOutlined,
  DeleteOutlined,
  UploadOutlined,
  ReloadOutlined,
  DownloadOutlined,
  SyncOutlined,
  CopyOutlined,
  CheckOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import { listInvoices, listCompanies, deleteInvoice, batchUpdateInvoices, batchDeleteInvoices, batchReprocessInvoices, batchUnlockInvoices, batchSyncInvoices, batchConfirmInvoices, getStatistics, checkDuplicates, downloadInvoiceExport, startDingTalkApprovalImport, getDingTalkApprovalImportStatus, getParsingStatus } from '../services/api';
import type { CompanyGroup, ParsingStatus } from '../services/api';
import type { Invoice, Statistics, DuplicateGroup } from '../types/invoice';
import { InvoiceStatus } from '../types/invoice';
import ResizableTitle from '../components/ResizableTitle';
import ColumnSelector from '../components/ColumnSelector';
import { useColumnSettings } from '../hooks/useColumnSettings';
import { useInvoiceStatistics } from '../hooks/useInvoiceStatistics';
import MetricCard from '../components/dashboard/MetricCard';
import ControlBar from '../components/dashboard/ControlBar';
import { useAuth } from '../contexts/AuthContext';
import styles from './InvoiceListPage.module.css';
import 'react-resizable/css/styles.css';

const statusColors: Record<string, string> = {
  '已上传': 'default',
  '解析中': 'processing',
  '待处理': 'blue',
  '待审核': 'warning',
  '已确认': 'success',
  '已报销': 'green',
  '未报销': 'orange',
};

function InvoiceListPage() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const approvalNumber = searchParams.get('approval_number');
  const navigate = useNavigate();
  const { user: currentUser } = useAuth();
  const [loading, setLoading] = useState(false);

  // Old approval links used the root invoice list with a query parameter.
  // Move them into the dedicated approval module so the main list keeps its
  // own navigation history and filters.
  useEffect(() => {
    if (approvalNumber) {
      navigate(`/approvals/${encodeURIComponent(approvalNumber)}/invoices`, { replace: true });
    }
  }, [approvalNumber, navigate]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([]);
  const [statistics, setStatistics] = useState<Statistics | null>(null);
  const [syncLoading, setSyncLoading] = useState(false);

  // Duplicate check state
  const [dupLoading, setDupLoading] = useState(false);
  const [dupModalOpen, setDupModalOpen] = useState(false);
  const [dupGroups, setDupGroups] = useState<DuplicateGroup[]>([]);
  const [dupSummary, setDupSummary] = useState({ total_groups: 0, total_duplicates: 0 });
  const [duplicateDeletingId, setDuplicateDeletingId] = useState<number | null>(null);

  const handleCheckDuplicates = async () => {
    setDupLoading(true);
    try {
      const result = await checkDuplicates();
      setDupGroups(result.groups);
      setDupSummary({ total_groups: result.total_groups, total_duplicates: result.total_duplicates });
      setDupModalOpen(true);
    } catch {
      message.error('查重失败');
    } finally {
      setDupLoading(false);
    }
  };

  const approvalNumbersOf = (group: DuplicateGroup) =>
    Array.from(
      new Set(group.invoices.map((invoice) => invoice.approval_instance_id).filter(Boolean)),
    ) as string[];

  const handleDeleteDuplicate = async (invoice: Invoice) => {
    setDuplicateDeletingId(invoice.id);
    try {
      await deleteInvoice(invoice.id);
      message.success(`已删除发票 ID ${invoice.id}`);
      await Promise.all([handleCheckDuplicates(), fetchInvoices()]);
    } catch {
      message.error('删除发票失败');
    } finally {
      setDuplicateDeletingId(null);
    }
  };

  // Filters
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [ownerFilter, setOwnerFilter] = useState<string>('');
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [searchValue, setSearchValue] = useState<string>('');
  const [partyTypeFilter, setPartyTypeFilter] = useState<'company' | 'person' | undefined>();
  const [partyKeyFilter, setPartyKeyFilter] = useState<string | undefined>();
  const [personBuyerSearch, setPersonBuyerSearch] = useState('');
  const [companyGroups, setCompanyGroups] = useState<CompanyGroup[]>([]);
  const [approvalImport, setApprovalImport] = useState<any>(null);
  const [parsingStatus, setParsingStatus] = useState<ParsingStatus | null>(null);

  useEffect(() => {
    listCompanies()
      .then(companies => setCompanyGroups(companies.items))
      .catch(() => message.error('公司分类加载失败'));
  }, []);

  useEffect(() => {
    const refresh = () => getParsingStatus().then(setParsingStatus).catch(() => {});
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, []);

  // Column settings
  const {
    columnConfigs,
    setColumnConfigs,
    columnWidths,
    handleColumnResize,
    visibleColumns,
  } = useColumnSettings();

  const fetchInvoices = async (showError = false) => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page, page_size: pageSize };
      if (approvalNumber) params.approval_number = approvalNumber;
      if (statusFilter) params.status = statusFilter;
      if (ownerFilter) params.owner = ownerFilter;
      if (dateRange?.[0]) params.start_date = dateRange[0].format('YYYY-MM-DD');
      if (dateRange?.[1]) params.end_date = dateRange[1].format('YYYY-MM-DD');
      if (searchValue) params.keyword = searchValue;
      if (partyTypeFilter) params.party_type = partyTypeFilter;
      if (partyKeyFilter) params.company_key = partyKeyFilter;
      if (partyTypeFilter === 'person' && personBuyerSearch.trim()) params.buyer_keyword = personBuyerSearch.trim();

      const response = await listInvoices(params);
      setInvoices(response.items);
      setTotal(response.total);
    } catch (error) {
      setInvoices([]);
      setTotal(0);
      if (showError) {
        message.error('获取发票列表失败');
      }
      console.error(error);
    } finally {
      setLoading(false);
    }
  };

  const fetchStatistics = async () => {
    if (selectedRowKeys.length === 0) {
      setStatistics(null);
      return;
    }

    try {
      const stats = await getStatistics(selectedRowKeys);
      setStatistics(stats);
    } catch (error) {
      console.error(error);
    }
  };

  useEffect(() => {
    fetchInvoices();
  }, [page, pageSize, statusFilter, ownerFilter, dateRange, searchValue, partyTypeFilter, partyKeyFilter, personBuyerSearch]);

  // Auto-poll while any invoice is in processing state
  useEffect(() => {
    const hasProcessing = invoices.some(
      (inv) => inv.status === InvoiceStatus.PROCESSING || inv.status === InvoiceStatus.UPLOADED
    );
    if (!hasProcessing) return;
    const timer = setInterval(() => fetchInvoices(), 5000);
    return () => clearInterval(timer);
  }, [invoices]);

  useEffect(() => {
    fetchStatistics();
  }, [selectedRowKeys]);

  const handleStatusChange = (value: string | undefined) => {
    setStatusFilter(value);
    setPage(1);
  };

  const handleSearchChange = (value: string) => {
    setSearchValue(value);
    setPage(1);
  };

  const handleDateRangeChange = (range: [dayjs.Dayjs | null, dayjs.Dayjs | null] | null) => {
    setDateRange(range);
    setPage(1);
  };

  const handleOwnerChange = (value: string) => {
    setOwnerFilter(value);
    setPage(1);
  };

  const handlePartyTypeChange = (value: 'company' | 'person' | undefined) => {
    setPartyTypeFilter(value);
    setPartyKeyFilter(undefined);
    setPersonBuyerSearch('');
    setPage(1);
  };

  const handlePartyKeyChange = (value: string | undefined) => {
    setPartyKeyFilter(value);
    setPage(1);
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteInvoice(id);
      message.success('删除成功');
      fetchInvoices();
    } catch (error) {
      message.error('删除失败');
    }
  };

  const handleBatchUpdate = async (status: string) => {
    if (selectedRowKeys.length === 0) {
      message.warning('请先选择发票');
      return;
    }

    try {
      await batchUpdateInvoices(selectedRowKeys, status);
      message.success('批量更新成功');
      setSelectedRowKeys([]);
      fetchInvoices();
    } catch (error) {
      message.error('批量更新失败');
    }
  };

  const handleBatchDelete = () => {
    if (selectedRowKeys.length === 0) {
      message.warning('请先选择发票');
      return;
    }

    Modal.confirm({
      title: '确认批量删除',
      content: `确定要删除选中的 ${selectedRowKeys.length} 张发票吗？此操作不可恢复。`,
      okText: '确认删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          const result = await batchDeleteInvoices(selectedRowKeys);
          message.success(result.message);
          setSelectedRowKeys([]);
          fetchInvoices();
        } catch (error) {
          message.error('批量删除失败');
        }
      },
    });
  };

  const handleBatchReprocess = () => {
    if (selectedRowKeys.length === 0) {
      message.warning('请先选择发票');
      return;
    }

    Modal.confirm({
      title: '确认重新解析',
      content: `确定要重新解析选中的 ${selectedRowKeys.length} 张发票吗？成功后更新解析结果，失败时保留原有数据。`,
      okText: '确认重新解析',
      cancelText: '取消',
      onOk: async () => {
        try {
          const result = await batchReprocessInvoices(selectedRowKeys);
          message.success(result.message);
          setSelectedRowKeys([]);
          fetchInvoices();
        } catch (error: any) {
          message.error(error?.response?.data?.detail || '批量重新解析失败');
        }
      },
    });
  };

  const handleBatchUnlock = () => {
    Modal.confirm({
      title: '批量取消锁定',
      content: `将选中的 ${selectedRowKeys.length} 张发票中已确认锁定的记录改为待审核，之后可以编辑或重新解析。未锁定的记录会跳过。`,
      okText: '取消锁定',
      cancelText: '返回',
      onOk: async () => {
        try {
          const result = await batchUnlockInvoices(selectedRowKeys);
          message.success(result.message);
          setSelectedRowKeys([]);
          fetchInvoices();
        } catch (error: any) {
          message.error(error?.response?.data?.detail || '批量取消锁定失败');
        }
      },
    });
  };

  const handleBatchConfirm = () => {
    if (selectedRowKeys.length === 0) {
      message.warning('请先选择发票');
      return;
    }
    Modal.confirm({
      title: '批量确认发票',
      content: `确定要确认选中的 ${selectedRowKeys.length} 张发票吗？（仍有未解决差异的发票会失败）`,
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        try {
          const result = await batchConfirmInvoices(selectedRowKeys);
          if (result.failed === 0) {
            message.success(`成功确认 ${result.success} 张发票`);
          } else {
            message.warning(`成功 ${result.success} 张，失败 ${result.failed} 张（可能有未解决的差异）`);
          }
          setSelectedRowKeys([]);
          fetchInvoices();
        } catch (error) {
          message.error('批量确认失败');
        }
      },
    });
  };

  const handleExport = (format: 'csv' | 'excel') => {
    const params = new URLSearchParams();

    if (selectedRowKeys.length > 0) {
      params.append('invoice_ids', selectedRowKeys.join(','));
    }
    if (statusFilter) {
      params.append('status', statusFilter);
    }
    if (ownerFilter) {
      params.append('owner', ownerFilter);
    }
    if (dateRange?.[0]) {
      params.append('start_date', dateRange[0].format('YYYY-MM-DD'));
    }
    if (dateRange?.[1]) {
      params.append('end_date', dateRange[1].format('YYYY-MM-DD'));
    }
    if (partyKeyFilter) {
      params.append('company_key', partyKeyFilter);
    }
    if (partyTypeFilter) {
      params.append('party_type', partyTypeFilter);
    }
    if (partyTypeFilter === 'person' && personBuyerSearch.trim()) {
      params.append('buyer_keyword', personBuyerSearch.trim());
    }
    if (searchValue) {
      params.append('keyword', searchValue);
    }

    downloadInvoiceExport(format, params).catch(() => message.error('导出失败'));
  };

  const handleDingTalkSync = async () => {
    const params: Record<string, string> = {};
    if (selectedRowKeys.length > 0) params.invoice_ids = selectedRowKeys.join(',');
    if (statusFilter) params.status = statusFilter;
    if (ownerFilter) params.owner = ownerFilter;
    if (dateRange?.[0]) params.start_date = dateRange[0].format('YYYY-MM-DD');
    if (dateRange?.[1]) params.end_date = dateRange[1].format('YYYY-MM-DD');
    if (searchValue) params.keyword = searchValue;
    if (partyTypeFilter) params.party_type = partyTypeFilter;
    if (partyKeyFilter) params.company_key = partyKeyFilter;
    if (partyTypeFilter === 'person' && personBuyerSearch.trim()) {
      params.buyer_keyword = personBuyerSearch.trim();
    }
    setSyncLoading(true);
    try {
      const result = await batchSyncInvoices(params);
      message.success(result.message);
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '提交钉钉同步失败');
    } finally {
      setSyncLoading(false);
    }
  };

  const handleApprovalAttachmentUpload = () => {
    startDingTalkApprovalImport().then(setApprovalImport).then(() => message.success('审批流程附件导入任务已提交')).catch(() => message.error('提交导入任务失败'));
  };
  useEffect(() => { const t=window.setInterval(()=>getDingTalkApprovalImportStatus().then(setApprovalImport).catch(()=>{}), 3000); getDingTalkApprovalImportStatus().then(setApprovalImport).catch(()=>{}); return ()=>window.clearInterval(t); }, []);

  // All column definitions
  const allColumnDefinitions: Record<string, ColumnsType<Invoice>[number]> = useMemo(
    () => ({
      invoice_number: {
        title: '发票号码',
        dataIndex: 'invoice_number',
        key: 'invoice_number',
        width: columnWidths.invoice_number,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.invoice_number,
          onResize: handleColumnResize('invoice_number'),
        }),
      },
      approval_instance_id: {
        title: '审批编号',
        dataIndex: 'approval_instance_id',
        key: 'approval_instance_id',
        width: columnWidths.approval_instance_id,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.approval_instance_id,
          onResize: handleColumnResize('approval_instance_id'),
        }),
      },
      issue_date: {
        title: '开票日期',
        dataIndex: 'issue_date',
        key: 'issue_date',
        width: columnWidths.issue_date,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.issue_date,
          onResize: handleColumnResize('issue_date'),
        }),
      },
      buyer_name: {
        title: '购买方',
        dataIndex: 'buyer_name',
        key: 'buyer_name',
        width: columnWidths.buyer_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.buyer_name,
          onResize: handleColumnResize('buyer_name'),
        }),
      },
      buyer_tax_id: {
        title: '购买方税号',
        dataIndex: 'buyer_tax_id',
        key: 'buyer_tax_id',
        width: columnWidths.buyer_tax_id,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.buyer_tax_id,
          onResize: handleColumnResize('buyer_tax_id'),
        }),
      },
      seller_name: {
        title: '销售方',
        dataIndex: 'seller_name',
        key: 'seller_name',
        width: columnWidths.seller_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.seller_name,
          onResize: handleColumnResize('seller_name'),
        }),
      },
      seller_tax_id: {
        title: '销售方税号',
        dataIndex: 'seller_tax_id',
        key: 'seller_tax_id',
        width: columnWidths.seller_tax_id,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.seller_tax_id,
          onResize: handleColumnResize('seller_tax_id'),
        }),
      },
      item_name: {
        title: '项目名称',
        dataIndex: 'item_name',
        key: 'item_name',
        width: columnWidths.item_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.item_name,
          onResize: handleColumnResize('item_name'),
        }),
      },
      specification: {
        title: '规格型号',
        dataIndex: 'specification',
        key: 'specification',
        width: columnWidths.specification,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.specification,
          onResize: handleColumnResize('specification'),
        }),
      },
      unit: {
        title: '单位',
        dataIndex: 'unit',
        key: 'unit',
        width: columnWidths.unit,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.unit,
          onResize: handleColumnResize('unit'),
        }),
      },
      quantity: {
        title: '数量',
        dataIndex: 'quantity',
        key: 'quantity',
        width: columnWidths.quantity,
        align: 'right',
        render: (val) => (val != null ? Number(val) : '-'),
        onHeaderCell: () => ({
          width: columnWidths.quantity,
          onResize: handleColumnResize('quantity'),
        }),
      },
      unit_price: {
        title: '单价',
        dataIndex: 'unit_price',
        key: 'unit_price',
        width: columnWidths.unit_price,
        align: 'right',
        render: (val) => (val != null ? `¥${Number(val).toFixed(4)}` : '-'),
        onHeaderCell: () => ({
          width: columnWidths.unit_price,
          onResize: handleColumnResize('unit_price'),
        }),
      },
      amount: {
        title: '金额(不含税)',
        dataIndex: 'amount',
        key: 'amount',
        width: columnWidths.amount,
        align: 'right',
        render: (val) => (val != null ? `¥${Number(val).toFixed(2)}` : '-'),
        onHeaderCell: () => ({
          width: columnWidths.amount,
          onResize: handleColumnResize('amount'),
        }),
      },
      tax_rate: {
        title: '税率',
        dataIndex: 'tax_rate',
        key: 'tax_rate',
        width: columnWidths.tax_rate,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.tax_rate,
          onResize: handleColumnResize('tax_rate'),
        }),
      },
      tax_amount: {
        title: '税额',
        dataIndex: 'tax_amount',
        key: 'tax_amount',
        width: columnWidths.tax_amount,
        align: 'right',
        render: (val) => (val != null ? `¥${Number(val).toFixed(2)}` : '-'),
        onHeaderCell: () => ({
          width: columnWidths.tax_amount,
          onResize: handleColumnResize('tax_amount'),
        }),
      },
      total_with_tax: {
        title: '价税合计',
        dataIndex: 'total_with_tax',
        key: 'total_with_tax',
        width: columnWidths.total_with_tax,
        align: 'right',
        render: (val) => (val != null ? `¥${Number(val).toFixed(2)}` : '-'),
        onHeaderCell: () => ({
          width: columnWidths.total_with_tax,
          onResize: handleColumnResize('total_with_tax'),
        }),
      },
      status: {
        title: '状态',
        dataIndex: 'status',
        key: 'status',
        width: columnWidths.status,
        render: (status: string) => (
          <Tag color={statusColors[status] || 'default'}>{status}</Tag>
        ),
        onHeaderCell: () => ({
          width: columnWidths.status,
          onResize: handleColumnResize('status'),
        }),
      },
      owner: {
        title: '归属人',
        dataIndex: 'owner',
        key: 'owner',
        width: columnWidths.owner,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.owner,
          onResize: handleColumnResize('owner'),
        }),
      },
      uploaded_by: {
        title: '上传者',
        key: 'uploaded_by',
        width: columnWidths.uploaded_by ?? 120,
        render: (_: unknown, record: Invoice) => {
          const name = record.uploaded_by_display_name || record.uploaded_by_username;
          return name || '-';
        },
        onHeaderCell: () => ({
          width: columnWidths.uploaded_by ?? 120,
          onResize: handleColumnResize('uploaded_by'),
        }),
      },
      file_name: {
        title: '文件名',
        dataIndex: 'file_name',
        key: 'file_name',
        width: columnWidths.file_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.file_name,
          onResize: handleColumnResize('file_name'),
        }),
      },
      created_at: {
        title: '创建时间',
        dataIndex: 'created_at',
        key: 'created_at',
        width: columnWidths.created_at,
        render: (val) => (val ? dayjs(val).format('YYYY-MM-DD HH:mm') : '-'),
        onHeaderCell: () => ({
          width: columnWidths.created_at,
          onResize: handleColumnResize('created_at'),
        }),
      },
      updated_at: {
        title: '更新时间',
        dataIndex: 'updated_at',
        key: 'updated_at',
        width: columnWidths.updated_at,
        render: (val) => (val ? dayjs(val).format('YYYY-MM-DD HH:mm') : '-'),
        onHeaderCell: () => ({
          width: columnWidths.updated_at,
          onResize: handleColumnResize('updated_at'),
        }),
      },
      action: {
        title: '操作',
        key: 'action',
        width: columnWidths.action,
        fixed: 'right' as const,
        render: (_, record) => (
          <Space>
            <Button
              type="link"
              icon={<EyeOutlined />}
            onClick={() => navigate(`/invoices/${record.id}`, { state: { ids: invoices.map((i) => i.id), returnTo: `${location.pathname}${location.search}`, returnLabel: approvalNumber ? '返回审批发票' : '返回列表' } })}
            >
              详情
            </Button>
            <Popconfirm
              title="确定删除？"
              onConfirm={() => handleDelete(record.id)}
              okText="确定"
              cancelText="取消"
            >
              <Button type="link" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </Space>
        ),
      },
    }),
    [columnWidths, handleColumnResize, navigate]
  );

  // Build columns based on visible column configs
  // Admin-only columns: 'uploaded_by'
  const columns: ColumnsType<Invoice> = useMemo(() => {
    return visibleColumns
      .filter((config) => config.key !== 'uploaded_by' || !!currentUser?.is_admin)
      .map((config) => allColumnDefinitions[config.key])
      .filter(Boolean);
  }, [visibleColumns, allColumnDefinitions, currentUser?.is_admin]);

  // Calculate total scroll width
  const scrollX = useMemo(() => {
    return visibleColumns.reduce(
      (sum, col) => sum + (columnWidths[col.key] || 100),
      0
    );
  }, [visibleColumns, columnWidths]);

  // Calculate statistics from current page invoices
  const pageStatistics = useInvoiceStatistics(invoices);

  // Generate total text for control bar
  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / pageSize)), [total, pageSize]);
  const displayStart = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const displayEnd = total === 0 ? 0 : Math.min(page * pageSize, total);

  const totalText = useMemo(() => {
    return `显示 ${displayStart}-${displayEnd} 条，共 ${total} 条`;
  }, [displayStart, displayEnd, total]);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  // Table components with resizable header
  const tableComponents = {
    header: {
      cell: ResizableTitle,
    },
  };

  return (
    <div className={styles.pageContainer}>
      {/* Page Header */}
      <div className={styles.pageHeader}>
        <div className={styles.headerContent}>
          <h1 className={styles.pageTitle}>发票列表</h1>
          <p className={styles.pageSubtitle}>管理和查看所有发票记录</p>
        </div>
        <div className={styles.headerActions}>
          <Button
            icon={<CopyOutlined />}
            onClick={handleCheckDuplicates}
            loading={dupLoading}
            className={styles.secondaryButton}
          >
            查重
          </Button>
          <Button
            icon={<DownloadOutlined />}
            onClick={() => handleExport('excel')}
            className={styles.secondaryButton}
          >
            导出
          </Button>
          <Button
            onClick={() => navigate('/vouchers')}
            className={styles.secondaryButton}
          >
            凭证归档
          </Button>
          <Button
            icon={<SyncOutlined />}
            onClick={handleDingTalkSync}
            loading={syncLoading}
            className={styles.secondaryButton}
          >
            同步钉钉
          </Button>
          <Button
            icon={<UploadOutlined />}
            onClick={handleApprovalAttachmentUpload}
            className={styles.secondaryButton}
          >
            审批流程附件上传
          </Button>
          <Button
            type="primary"
            icon={<UploadOutlined />}
            onClick={() => navigate('/upload')}
            className={styles.primaryButton}
          >
            上传发票
          </Button>
        </div>
      </div>

      {approvalImport && <div style={{ marginBottom: 12, color: approvalImport.failed ? '#cf1322' : '#666' }}>审批附件导入：{approvalImport.failed ? '部分附件导入失败' : approvalImport.status} · 扫描进度 {approvalImport.windows_completed ?? 0}/{approvalImport.windows_total ?? 0} 个时间段 · 已扫描审批 {approvalImport.scanned_approvals ?? 0} 笔 · 总附件 {approvalImport.total} · 已下载 {approvalImport.downloaded} · 已识别 {approvalImport.recognized} · 发票 {approvalImport.invoices} · 非发票 {approvalImport.non_invoices} · 跳过 {approvalImport.skipped} · 失败 {approvalImport.failed}</div>}
      {parsingStatus && parsingStatus.pending > 0 && (
        <Alert
          showIcon
          type="info"
          style={{ marginBottom: 12 }}
          message={`正在解析：${parsingStatus.processing} 张处理中，${parsingStatus.queued} 张排队`}
          description={`没有发票字段的附件会自动归档到凭证备份；当前已归档 ${parsingStatus.archived_vouchers} 份。进度每 5 秒刷新。`}
        />
      )}
      {/* Metric Cards */}
      <div className={styles.metricsSection}>
        <MetricCard
          label={approvalNumber ? '当前筛选发票数' : '发票总数'}
          value={total}
        />
        <MetricCard
          label="金额合计"
          value={pageStatistics.totalAmount.toFixed(2)}
          prefix="¥"
        />
        <MetricCard
          label="税额合计"
          value={pageStatistics.totalTax.toFixed(2)}
          prefix="¥"
        />
        <MetricCard
          label="价税合计"
          value={pageStatistics.totalWithTax.toFixed(2)}
          prefix="¥"
        />
      </div>

      {/* Selected Statistics Card */}
      {statistics && (
        <Card style={{ marginBottom: 32 }}>
          <Row gutter={24}>
            <Col span={6}>
              <Statistic title="选中数量" value={statistics.count} suffix="张" />
            </Col>
            <Col span={6}>
              <Statistic
                title="金额合计"
                value={statistics.total_amount}
                precision={2}
                prefix="¥"
              />
            </Col>
            <Col span={6}>
              <Statistic
                title="税额合计"
                value={statistics.total_tax}
                precision={2}
                prefix="¥"
              />
            </Col>
            <Col span={6}>
              <Statistic
                title="价税合计"
                value={statistics.total_with_tax}
                precision={2}
                prefix="¥"
              />
            </Col>
          </Row>
        </Card>
      )}

      {/* Control Bar */}
      <ControlBar
        searchPlaceholder="搜索发票号/购买方/销售方..."
        searchValue={searchValue}
        onSearchChange={handleSearchChange}
        statusFilter={statusFilter}
        onStatusChange={handleStatusChange}
        statusOptions={Object.values(InvoiceStatus).map((s) => ({ label: s, value: s }))}
        dateRange={dateRange}
        onDateRangeChange={handleDateRangeChange}
        totalText={totalText}
      />

      {/* Additional Controls */}
      <div className={styles.additionalControls}>
        <Select
          allowClear
          placeholder="筛选公司或个人"
          style={{ width: 160 }}
          value={partyTypeFilter}
          onChange={handlePartyTypeChange}
          options={[
            { label: '公司', value: 'company' },
            { label: '个人', value: 'person' },
          ]}
        />

        {partyTypeFilter === 'company' && (
          <Select
            showSearch
            allowClear
            optionFilterProp="label"
            placeholder="选择具体公司"
            style={{ width: 240 }}
            value={partyKeyFilter}
            onChange={handlePartyKeyChange}
            options={companyGroups.map(group => ({
              value: group.key,
              label: group.name || group.tax_id || '未归类公司',
            }))}
          />
        )}

        {partyTypeFilter === 'person' && (
          <Input
            allowClear
            placeholder="搜索购买方（可选）"
            style={{ width: 200 }}
            value={personBuyerSearch}
            onChange={event => { setPersonBuyerSearch(event.target.value); setPage(1); }}
          />
        )}

        <Input
          placeholder="归属人"
          style={{ width: 120 }}
          value={ownerFilter}
          onChange={(e) => handleOwnerChange(e.target.value)}
          onPressEnter={() => fetchInvoices(true)}
        />

        <Button icon={<ReloadOutlined />} onClick={() => fetchInvoices(true)}>
          刷新
        </Button>

        <Button icon={<DownloadOutlined />} onClick={() => handleExport('csv')}>
          导出CSV
        </Button>

        <ColumnSelector columns={columnConfigs} onChange={setColumnConfigs} />

        {selectedRowKeys.length > 0 && (
          <>
            <Select
              placeholder="批量修改状态"
              style={{ width: 140 }}
              onChange={handleBatchUpdate}
              options={Object.values(InvoiceStatus).map((s) => ({ label: s, value: s }))}
            />
            <Button
              icon={<SyncOutlined />}
              onClick={handleBatchReprocess}
            >
              重新解析
            </Button>
            <Button onClick={handleBatchUnlock}>
              批量取消锁定
            </Button>
            <Button
              icon={<CheckOutlined />}
              onClick={handleBatchConfirm}
            >
              批量确认
            </Button>
            <Button
              danger
              icon={<DeleteOutlined />}
              onClick={handleBatchDelete}
            >
              批量删除
            </Button>
            <span style={{ lineHeight: '32px' }}>
              已选择 {selectedRowKeys.length} 项
            </span>
          </>
        )}
      </div>

      {/* Table */}
      <div className={styles.tableContainer}>
        <Table
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={invoices}
          components={tableComponents}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span>
                    暂无发票数据，
                    <Button type="link" style={{ padding: 0 }} onClick={() => navigate('/upload')}>
                      立即上传
                    </Button>
                  </span>
                }
              />
            ),
          }}
          pagination={false}
          scroll={{ x: scrollX }}
          className={styles.invoiceTable}
        />
      </div>

      {/* Custom Pagination */}
      <div className={styles.paginationRow}>
        <div className={styles.paginationLeft}>
          <span className={styles.paginationText}>
            显示 {displayStart}-{displayEnd} 共 {total} 条
          </span>
        </div>
        <div className={styles.paginationRight}>
          <Button
            disabled={page === 1}
            onClick={() => setPage(page - 1)}
          >
            上一页
          </Button>
          <span className={styles.pageNumbers}>
            {page} / {totalPages}
          </span>
          <Button
            disabled={page >= totalPages}
            onClick={() => setPage(page + 1)}
          >
            下一页
          </Button>
          <Select
            value={pageSize}
            onChange={(val) => {
              setPageSize(val);
              setPage(1);
            }}
            options={[
              { label: '10 条/页', value: 10 },
              { label: '20 条/页', value: 20 },
              { label: '50 条/页', value: 50 },
              { label: '100 条/页', value: 100 },
            ]}
            style={{ width: 120, marginLeft: 12 }}
          />
        </div>
      </div>

      {/* Duplicate Check Modal */}
      <Modal
        title={
          <span>
            <CopyOutlined style={{ marginRight: 8, color: '#faad14' }} />
            查重结果
            {dupSummary.total_groups > 0 && (
              <span style={{ marginLeft: 12, fontSize: 13, fontWeight: 400, color: '#ff4d4f' }}>
                发现 {dupSummary.total_groups} 组重复，共 {dupSummary.total_duplicates} 张发票
              </span>
            )}
          </span>
        }
        open={dupModalOpen}
        onCancel={() => setDupModalOpen(false)}
        footer={<Button onClick={() => setDupModalOpen(false)}>关闭</Button>}
        width={900}
        style={{ top: 40 }}
        styles={{ body: { maxHeight: 'calc(80vh - 110px)', overflowY: 'auto', padding: '16px 24px' } }}
      >
        {dupGroups.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '40px 0', color: '#52c41a', fontSize: 16 }}>
            ✓ 未发现跨审批单重复发票
          </div>
        ) : (
          <>
            <div style={{
              background: '#fafafa',
              border: '1px solid #f0f0f0',
              borderRadius: 6,
              padding: '8px 14px',
              marginBottom: 16,
              fontSize: 12,
              color: '#595959',
              lineHeight: 1.9,
            }}>
              共 <b>{dupSummary.total_groups}</b> 组 / <b>{dupSummary.total_duplicates}</b> 张。
              查重只显示<b>同发票号码、不同审批编号</b>的记录。同一审批编号下的重复上传不计入这里。
              {dupGroups.some((g) => approvalNumbersOf(g).length > 1) && (
                <span style={{ color: '#ff4d4f' }}>
                  其中跨审批单重复 {dupGroups.filter((g) => approvalNumbersOf(g).length > 1).length} 组，属于疑似重复报销证据，系统禁止删除任何一条。
                </span>
              )}
            </div>
            {dupGroups.map((group, idx) => {
            const approvalList = approvalNumbersOf(group);
            const crossApproval = approvalList.length > 1;
            const hasManualCopy = group.invoices.some((i) => !i.approval_instance_id);
            return (
            <div key={idx} style={{ marginBottom: 24 }}>
              <div style={{
                background: crossApproval ? '#fff1f0' : '#fff7e6',
                border: `1px solid ${crossApproval ? '#ffa39e' : '#ffd591'}`,
                borderRadius: 6,
                padding: '8px 14px',
                marginBottom: 8,
                display: 'flex',
                alignItems: 'center',
                flexWrap: 'wrap',
                gap: 12,
              }}>
                <Tag color="warning">{group.reason}</Tag>
                <span style={{ fontSize: 13, color: '#595959', wordBreak: 'break-all' }}>
                  {group.duplicate_key}
                </span>
                <span style={{
                  fontSize: 12,
                  color: crossApproval ? '#ff4d4f' : '#8c8c8c',
                  fontWeight: crossApproval ? 600 : 400,
                }}>
                  涉及审批单：
                  {approvalList.length ? approvalList.join('、') : '（均无审批单）'}
                  {crossApproval ? `（${approvalList.length} 个，跨审批单重复）` : ''}
                  {hasManualCopy ? '　含手动上传副本（无审批单）' : ''}
                </span>
                <span style={{ marginLeft: 'auto', fontSize: 12, color: '#8c8c8c' }}>
                  {group.invoices.length} 张
                </span>
                {crossApproval && (
                  <Tag color="error">疑似重复报销证据，禁止删除</Tag>
                )}
              </div>
              <Table
                size="small"
                rowKey="id"
                dataSource={group.invoices}
                pagination={false}
                scroll={{ x: 1010 }}
                columns={[
                  { title: 'ID', dataIndex: 'id', width: 70 },
                  { title: '发票号码', dataIndex: 'invoice_number', width: 140, render: (v) => v || '-' },
                  {
                    // 查重是按全库比对的，重的那两张可能来自完全不同的审批单，
                    // 不显示审批单号就分不清该找谁核对、哪一笔该删。
                    title: '审批单号',
                    dataIndex: 'approval_instance_id',
                    width: 180,
                    render: (v: string | null) =>
                      v
                        ? <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{v}</span>
                        : <Tag>手动上传</Tag>,
                  },
                  { title: '归属人', dataIndex: 'owner', width: 90, render: (v) => v || '-' },
                  {
                    title: '价税合计',
                    dataIndex: 'total_with_tax',
                    width: 110,
                    align: 'right' as const,
                    render: (v) => (v != null ? `¥${Number(v).toFixed(2)}` : '-'),
                  },
                  { title: '状态', dataIndex: 'status', width: 80, render: (v) => <Tag color={statusColors[v] || 'default'}>{v}</Tag> },
                  {
                    title: '操作',
                    width: 130,
                    fixed: 'right' as const,
                    render: (_, record) => (
                      <Space size={0}>
                        <Button
                          type="link"
                          size="small"
                          icon={<EyeOutlined />}
                          onClick={() => { setDupModalOpen(false); navigate(`/invoices/${record.id}`); }}
                        >
                          详情
                        </Button>
                        {crossApproval ? (
                          <Tag color="red">保留证据</Tag>
                        ) : (
                          <Popconfirm
                            title={`确认删除发票 ID ${record.id}？`}
                            description="只删除当前这一张记录，其他同发票号记录不受影响。"
                            okText="删除"
                            cancelText="取消"
                            okButtonProps={{ danger: true }}
                            onConfirm={() => handleDeleteDuplicate(record)}
                          >
                            <Button
                              type="link"
                              danger
                              size="small"
                              icon={<DeleteOutlined />}
                              loading={duplicateDeletingId === record.id}
                            >
                              删除
                            </Button>
                          </Popconfirm>
                        )}
                      </Space>
                    ),
                  },
                ]}
              />
            </div>
            );
          })}
          </>
        )}
      </Modal>
    </div>
  );
}

export default InvoiceListPage;
