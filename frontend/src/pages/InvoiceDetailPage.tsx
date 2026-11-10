import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import {
  Descriptions,
  Button,
  message,
  Spin,
  Row,
  Col,
  Form,
  Input,
  Select,
  Modal,
  Space,
} from 'antd';
import {
  ArrowLeftOutlined,
  LeftOutlined,
  RightOutlined,
  EditOutlined,
  SaveOutlined,
  SyncOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownloadOutlined,
  ExpandOutlined,
} from '@ant-design/icons';
import { getInvoice, fetchInvoiceFileBlob, downloadBlobUrl, updateInvoice, resolveDiff, confirmInvoice, unlockInvoice, reprocessInvoice, getAdjacentInvoices, archiveInvoiceAsVoucher } from '../services/api';
import type { InvoiceDetail } from '../types/invoice';
import { InvoiceStatus } from '../types/invoice';
import StatusTag from '../components/StatusTag';
import { useAuth } from '../contexts/AuthContext';
import styles from './InvoiceDetailPage.module.css';

const fieldLabels: Record<string, string> = {
  invoice_number: '发票号码',
  issue_date: '开票日期',
  buyer_name: '购买方名称',
  buyer_tax_id: '购买方纳税人识别号',
  seller_name: '销售方名称',
  seller_tax_id: '销售方纳税人识别号',
  item_name: '项目名称',
  total_with_tax: '价税合计',
  specification: '规格型号 / 行程信息',
  unit: '单位',
  quantity: '数量',
  unit_price: '单价',
  amount: '金额',
  tax_rate: '税率',
  tax_amount: '税额',
};

function InvoiceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { user: currentUser } = useAuth();
  const locationState = location.state as { ids?: number[]; returnTo?: string; returnLabel?: string } | null;
  const idList: number[] = locationState?.ids ?? [];
  const returnTo = locationState?.returnTo?.startsWith('/') ? locationState.returnTo : '/';
  const returnLabel = locationState?.returnLabel || '返回列表';
  const currentIndex = idList.indexOf(Number(id));
  const prevIdFromList = currentIndex > 0 ? idList[currentIndex - 1] : null;
  const nextIdFromList = currentIndex >= 0 && currentIndex < idList.length - 1 ? idList[currentIndex + 1] : null;
  // When navigating within the list, compute adjacent IDs directly from idList
  // so they stay correct across same-component re-renders (React doesn't remount on route change).
  // Fall back to API-fetched adjacent IDs when opened directly without idList.
  const [adjPrevId, setAdjPrevId] = useState<number | null>(null);
  const [adjNextId, setAdjNextId] = useState<number | null>(null);
  const prevId = idList.length > 0 ? prevIdFromList : adjPrevId;
  const nextId = idList.length > 0 ? nextIdFromList : adjNextId;
  const [loading, setLoading] = useState(true);
  const [reprocessing, setReprocessing] = useState(false);
  const [invoice, setInvoice] = useState<InvoiceDetail | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [form] = Form.useForm();
  const [resolvingDiff, setResolvingDiff] = useState<number | null>(null);
  const [customValueModal, setCustomValueModal] = useState<{ visible: boolean; diffId: number | null; fieldName: string }>({
    visible: false,
    diffId: null,
    fieldName: '',
  });
  const [customValue, setCustomValue] = useState('');
  const [rightPanelWidth, setRightPanelWidth] = useState(420);
  const [isDragging, setIsDragging] = useState(false);
  const [fullscreenPreview, setFullscreenPreview] = useState(false);
  const [fileBlob, setFileBlob] = useState<{ url: string; filename: string } | null>(null);
  const isRailwayTicket = Boolean(invoice?.item_name?.startsWith('铁路'));
  const dragStartX = useRef(0);
  const dragStartWidth = useRef(420);

  const handleResizerMouseDown = useCallback((e: React.MouseEvent) => {
    setIsDragging(true);
    dragStartX.current = e.clientX;
    dragStartWidth.current = rightPanelWidth;
    e.preventDefault();
  }, [rightPanelWidth]);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging) return;
      const delta = dragStartX.current - e.clientX;
      const newWidth = Math.max(300, Math.min(900, dragStartWidth.current + delta));
      setRightPanelWidth(newWidth);
    };
    const handleMouseUp = () => setIsDragging(false);
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isDragging]);

  const fetchInvoice = async () => {
    if (!id) return;

    setLoading(true);
    try {
      const data = await getInvoice(parseInt(id));
      setInvoice(data);
      form.setFieldsValue(data);
    } catch (error) {
      message.error('获取发票详情失败');
      console.error(error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchInvoice();
    // Fetch adjacent IDs from API only when idList is not available
    if (idList.length === 0 && id) {
      getAdjacentInvoices(parseInt(id)).then(({ prev_id, next_id }) => {
        setAdjPrevId(prev_id);
        setAdjNextId(next_id);
      }).catch(() => {/* ignore */});
    } else {
      setAdjPrevId(null);
      setAdjNextId(null);
    }
  }, [id]);

  // Load file blob with auth header; revoke URL on cleanup
  useEffect(() => {
    if (!invoice) return;
    let blobUrl: string | null = null;
    fetchInvoiceFileBlob(invoice.id)
      .then((blob) => {
        blobUrl = blob.url;
        setFileBlob(blob);
      })
      .catch(() => {/* preview unavailable */});
    return () => {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
      setFileBlob(null);
    };
  }, [invoice?.id]);

  const handleSave = async () => {
    if (!id || !invoice) return;

    try {
      const values = await form.validateFields();
      await updateInvoice(parseInt(id), values);
      message.success('保存成功');
      setEditMode(false);
      fetchInvoice();
    } catch (error) {
      message.error('保存失败');
    }
  };

  const handleResolveDiff = async (diffId: number, source: 'ocr' | 'llm' | 'custom', customVal?: string) => {
    if (!id) return;
    setResolvingDiff(diffId);
    try {
      const result = await resolveDiff(parseInt(id), diffId, source, customVal);
      message.success(`${fieldLabels[result.field_name] || result.field_name} 已解决`);
      if (result.all_resolved) {
        message.success('所有差异已解决，发票已确认');
      }
      fetchInvoice();
    } catch (error) {
      message.error('解决差异失败');
    } finally {
      setResolvingDiff(null);
    }
  };

  const handleConfirmAll = async () => {
    if (!id) return;

    if (invoice) {
      const requiredFields: Array<keyof InvoiceDetail> = [
        'invoice_number',
        'issue_date',
        'total_with_tax',
      ];
      // Read from form (covers edits not yet saved) or fall back to invoice state
      const formValues = form.getFieldsValue();
      const missing = requiredFields.filter((field) => {
        const value = formValues[field] ?? invoice[field];
        if (value === null || value === undefined) {
          return true;
        }
        if (typeof value === 'string') {
          return value.trim().length === 0;
        }
        return false;
      });

      if (missing.length > 0) {
        const fieldLabels: Record<string, string> = {
          invoice_number: '发票号码',
          issue_date: '开票日期',
          total_with_tax: '价税合计',
        };
        const names = missing.map((f) => fieldLabels[f] ?? f).join('、');
        message.error(`请先补全必填字段（${names}），再进行确认。`);
        return;
      }
    }

    try {
      await confirmInvoice(parseInt(id));
      message.success(invoice?.llm_result ? '发票已确认' : '发票已确认（OCR-only）');
      fetchInvoice();
    } catch (error: unknown) {
      if (error && typeof error === 'object' && 'response' in error) {
        const axiosError = error as { response?: { data?: { detail?: string } } };
        if (axiosError.response?.data?.detail) {
          message.error(axiosError.response.data.detail);
          return;
        }
      }
      message.error('确认失败');
    }
  };

  const handleReprocess = async () => {
    if (!id) return;
    setReprocessing(true);
    try {
      await reprocessInvoice(parseInt(id));
      message.success('重新解析完成');
      await fetchInvoice();
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '重新解析失败，原有数据已保留');
    } finally {
      setReprocessing(false);
    }
  };

  const handleUnlock = () => {
    if (!id) return;
    Modal.confirm({
      title: '取消锁定发票',
      content: '取消锁定后可编辑发票内容或重新解析。',
      okText: '取消锁定',
      cancelText: '保留锁定',
      onOk: async () => {
        await unlockInvoice(parseInt(id));
        message.success('已取消锁定');
        await fetchInvoice();
      },
    });
  };

  const handleArchiveAsVoucher = () => {
    if (!id) return;
    Modal.confirm({
      title: '标记为不是发票？',
      content: '文件会保留在凭证归档中，但会从发票列表和待审核列表移除。',
      okText: '归档到凭证',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        await archiveInvoiceAsVoucher(parseInt(id));
        message.success('已归档到凭证备份');
        navigate(returnTo);
      },
    });
  };

  const handleCustomValueSubmit = async () => {
    if (customValueModal.diffId) {
      await handleResolveDiff(customValueModal.diffId, 'custom', customValue);
      setCustomValueModal({ visible: false, diffId: null, fieldName: '' });
      setCustomValue('');
    }
  };

  if (loading) {
    return (
      <div className={styles.loadingContainer}>
        <Spin size="large" />
      </div>
    );
  }

  if (!invoice) {
    return <div>发票不存在</div>;
  }

  const hasLlm = Boolean(invoice.llm_result);
  const hasDiffs = Boolean(invoice.parsing_diffs && invoice.parsing_diffs.length > 0);
  const hasUnresolvedDiffs = Boolean(invoice.parsing_diffs && invoice.parsing_diffs.some(d => !d.resolved));

  // Calculate match count for comparison header
  const matchCount = invoice.parsing_diffs?.filter(d => d.resolved).length || 0;
  const totalCount = invoice.parsing_diffs?.length || 0;

  return (
    <div className={styles.pageContainer}>
      {/* Page Header */}
      <div className={styles.pageHeader}>
        <div className={styles.headerLeft}>
          <button className={styles.backButton} onClick={() => navigate(returnTo)}>
            <ArrowLeftOutlined />
            {returnLabel}
          </button>
          <Button.Group size="small" style={{ marginLeft: 8 }}>
              <Button
                icon={<LeftOutlined />}
                disabled={prevId === null}
                onClick={() => prevId !== null && navigate(`/invoices/${prevId}`, { state: locationState })}
              >
                上一张
              </Button>
              <Button
                iconPosition="end"
                icon={<RightOutlined />}
                disabled={nextId === null}
                onClick={() => nextId !== null && navigate(`/invoices/${nextId}`, { state: locationState })}
              >
                下一张
              </Button>
            </Button.Group>
          <div className={styles.headerTitle}>
            <div className={styles.invoiceNumber}>
              {invoice.approval_instance_id || invoice.invoice_number || '发票详情'}
            </div>
            <div className={styles.invoiceMetadata}>
              开票日期: {invoice.issue_date || '-'} • 归属: {invoice.owner || '-'}
            </div>
          </div>
        </div>
        <div className={styles.headerActions}>
          {invoice.status === InvoiceStatus.CONFIRMED ? (
            <Button onClick={handleUnlock}>取消锁定</Button>
          ) : (
            <Space>
              <button className={styles.confirmButton} onClick={handleConfirmAll}>确认发票</button>
              {invoice.status === InvoiceStatus.REVIEWING && (
                <Button danger onClick={handleArchiveAsVoucher}>不是发票</Button>
              )}
            </Space>
          )}
        </div>
      </div>

      {/* Content Body */}
      <div className={styles.contentBody}>
        {/* Left Panel */}
        <div className={styles.leftPanel}>
          {/* Invoice Info Card */}
          <div className={styles.card}>
            <div className={styles.cardHeader}>
              <div className={styles.cardTitle}>
                {isRailwayTicket ? '铁路电子客票信息' : '发票信息'}
                <StatusTag status={invoice.status === '已确认' ? 'success' : 'processing'}>
                  {invoice.status}
                </StatusTag>
              </div>
              <div className={styles.cardActions}>
                {editMode ? (
                  <>
                    <Button onClick={() => setEditMode(false)}>取消</Button>
                    <Button type="primary" icon={<SaveOutlined />} onClick={handleSave}>
                      保存
                    </Button>
                  </>
                ) : invoice.status !== InvoiceStatus.CONFIRMED && (
                  <Button icon={<EditOutlined />} onClick={() => setEditMode(true)}>
                    编辑
                  </Button>
                )}
              </div>
            </div>
            <div className={styles.cardBody}>
              {editMode ? (
                <Form form={form} layout="vertical">
                  <Row gutter={16}>
                    <Col span={12}>
                      <Form.Item name="invoice_number" label="发票号码" required>
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="issue_date" label="开票日期" required>
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="buyer_name" label="购买方名称">
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="buyer_tax_id" label="购买方纳税人识别号">
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="seller_name" label="销售方名称">
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="seller_tax_id" label="销售方纳税人识别号">
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={24}>
                      <Form.Item name="item_name" label="项目名称">
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={24}>
                      <Form.Item name="specification" label={isRailwayTicket ? '行程信息' : '规格型号'}>
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name="total_with_tax" label="价税合计" required>
                        <Input type="number" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name="amount" label="金额">
                        <Input type="number" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name="tax_amount" label="税额">
                        <Input type="number" />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="status" label="状态">
                        <Select
                          options={Object.values(InvoiceStatus).map((s) => ({
                            label: s,
                            value: s,
                          }))}
                        />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="owner" label="归属人">
                        <Input />
                      </Form.Item>
                    </Col>
                  </Row>
                </Form>
              ) : (
                <Descriptions column={2} bordered size="small">
                  <Descriptions.Item label={<><span style={{color:'#ff4d4f',marginRight:2}}>*</span>发票号码</>}>
                    {invoice.invoice_number || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="审批编号">
                    {invoice.approval_instance_id || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<><span style={{color:'#ff4d4f',marginRight:2}}>*</span>开票日期</>}>
                    {invoice.issue_date || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="购买方名称">
                    {invoice.buyer_name || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="购买方纳税人识别号">
                    {invoice.buyer_tax_id || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="销售方名称">
                    {invoice.seller_name || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="销售方纳税人识别号">
                    {invoice.seller_tax_id || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="项目名称" span={2}>
                    {invoice.item_name || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={isRailwayTicket ? '行程信息' : '规格型号'} span={isRailwayTicket ? 2 : 1}>
                    {invoice.specification || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="单位">
                    {invoice.unit || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="数量">
                    {invoice.quantity != null ? invoice.quantity : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="单价">
                    {invoice.unit_price != null ? invoice.unit_price : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="金额">
                    {invoice.amount != null ? `¥${Number(invoice.amount).toFixed(2)}` : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="税率">
                    {invoice.tax_rate || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="税额">
                    {invoice.tax_amount != null ? `¥${Number(invoice.tax_amount).toFixed(2)}` : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<><span style={{color:'#ff4d4f',marginRight:2}}>*</span>价税合计</>}>
                    {invoice.total_with_tax != null ? `¥${Number(invoice.total_with_tax).toFixed(2)}` : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="归属人">
                    {invoice.owner || '-'}
                  </Descriptions.Item>
                  {currentUser?.is_admin && (
                    <Descriptions.Item label="上传者">
                      {invoice.uploaded_by_display_name || invoice.uploaded_by_username || '-'}
                    </Descriptions.Item>
                  )}
                </Descriptions>
              )}
            </div>
          </div>

          {/* Comparison & Review Section */}
          <div className={styles.card}>
            <div className={styles.cardHeader}>
              <div className={styles.cardTitle}>
                解析结果比对
                {!hasLlm && (
                  <StatusTag status="processing">
                    OCR-only
                  </StatusTag>
                )}
                {hasLlm && hasUnresolvedDiffs && (
                  <StatusTag status="warning">
                    {totalCount - matchCount}/{totalCount} 待确认
                  </StatusTag>
                )}
                {hasLlm && hasDiffs && !hasUnresolvedDiffs && (
                  <StatusTag status="success">
                    已完成比对
                  </StatusTag>
                )}
              </div>
              <div className={styles.cardActions}>
                <Button
                  icon={<SyncOutlined spin={reprocessing} />}
                  onClick={handleReprocess}
                  loading={reprocessing}
                  disabled={invoice.status === InvoiceStatus.CONFIRMED}
                  size="small"
                >
                  重新解析
                </Button>
              </div>
            </div>

            <Spin spinning={reprocessing} tip="正在重新解析...">
              {hasLlm && hasDiffs && (
                <div className={styles.comparisonHeader}>
                  <span className={styles.matchStatus}>
                    {matchCount}/{totalCount} 字段匹配
                  </span>
                </div>
              )}

              <div className={styles.cardBody}>
              {!hasLlm && (
                <div className={styles.infoAlert}>
                  发票已通过OCR识别预处理，可直接编辑后确认。配置LLM服务可获得更精准的智能比对功能，点击"重新解析"启用双重校验。
                </div>
              )}
              {hasDiffs ? (
                <table className={styles.comparisonTable}>
                  <thead>
                    <tr>
                      <th style={{ width: '20%' }}>字段</th>
                      <th style={{ width: '30%' }}>OCR识别结果</th>
                      <th style={{ width: '30%' }}>LLM解析结果</th>
                      <th style={{ width: '20%' }}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invoice.parsing_diffs?.map((diff) => {
                      const isMatch = diff.ocr_value === diff.llm_value;
                      return (
                        <tr key={diff.id} className={!isMatch ? styles.mismatch : styles.match}>
                          <td className={styles.fieldCell}>
                            {fieldLabels[diff.field_name] || diff.field_name}
                          </td>
                          <td>
                            <div className={styles.valueCell}>
                              {!isMatch && <CloseCircleOutlined className={`${styles.statusIcon} ${styles.mismatch}`} />}
                              {isMatch && <CheckCircleOutlined className={`${styles.statusIcon} ${styles.match}`} />}
                              <span>{diff.ocr_value || '-'}</span>
                            </div>
                          </td>
                          <td>
                            <div className={styles.valueCell}>
                              {!isMatch && <CloseCircleOutlined className={`${styles.statusIcon} ${styles.mismatch}`} />}
                              {isMatch && <CheckCircleOutlined className={`${styles.statusIcon} ${styles.match}`} />}
                              <span>{diff.llm_value || '-'}</span>
                            </div>
                          </td>
                          <td>
                            {invoice.status !== InvoiceStatus.CONFIRMED && !diff.resolved && !isMatch && (
                              <div style={{ display: 'flex', gap: '8px' }}>
                                <Button
                                  type="link"
                                  size="small"
                                  loading={resolvingDiff === diff.id}
                                  onClick={() => handleResolveDiff(diff.id, 'ocr')}
                                >
                                  选OCR
                                </Button>
                                <Button
                                  type="link"
                                  size="small"
                                  loading={resolvingDiff === diff.id}
                                  onClick={() => handleResolveDiff(diff.id, 'llm')}
                                >
                                  选LLM
                                </Button>
                                <Button
                                  type="link"
                                  size="small"
                                  onClick={() => {
                                    setCustomValueModal({ visible: true, diffId: diff.id, fieldName: diff.field_name });
                                    setCustomValue(diff.ocr_value || diff.llm_value || '');
                                  }}
                                >
                                  自定义
                                </Button>
                              </div>
                            )}
                            {diff.resolved && (
                              <StatusTag status="success">已解决</StatusTag>
                            )}
                            {invoice.status !== InvoiceStatus.CONFIRMED && isMatch && !diff.resolved && (
                              <Button
                                type="link"
                                size="small"
                                onClick={() => handleResolveDiff(diff.id, 'ocr')}
                              >
                                确认
                              </Button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <div style={{ textAlign: 'center', padding: '20px', color: '#999' }}>
                  {hasLlm ? '无比对差异' : 'OCR-only 模式暂无比对数据'}
                </div>
              )}
            </div>
            </Spin>
          </div>

          {/* Custom Value Modal */}
          <Modal
            title={`自定义值 - ${fieldLabels[customValueModal.fieldName] || customValueModal.fieldName}`}
            open={customValueModal.visible}
            onOk={handleCustomValueSubmit}
            onCancel={() => {
              setCustomValueModal({ visible: false, diffId: null, fieldName: '' });
              setCustomValue('');
            }}
            okText="确定"
            cancelText="取消"
          >
            <Input
              value={customValue}
              onChange={(e) => setCustomValue(e.target.value)}
              placeholder="请输入自定义值"
            />
          </Modal>
        </div>

        {/* Drag Resizer */}
        <div
          className={`${styles.resizer} ${isDragging ? styles.resizerActive : ''}`}
          onMouseDown={handleResizerMouseDown}
        />

        {/* Right Panel - PDF Preview */}
        <div className={styles.rightPanel} style={{ width: rightPanelWidth, flexShrink: 0 }}>
          <div className={styles.previewCard}>
            <div className={styles.previewHeader}>
              <span className={styles.previewTitle}>原始文件</span>
              <div className={styles.previewControls}>
                <button
                  className={styles.iconButton}
                  onClick={() => setFullscreenPreview(true)}
                  title="全屏预览"
                >
                  <ExpandOutlined />
                </button>
                <button
                  className={styles.downloadButton}
                  onClick={() => fileBlob && downloadBlobUrl(fileBlob.url, fileBlob.filename)}
                  disabled={!fileBlob}
                >
                  <DownloadOutlined />
                  下载
                </button>
              </div>
            </div>
            <div className={styles.previewBody}>
              <div className={styles.previewContent}>
                {isDragging && <div style={{ position: 'absolute', inset: 0, zIndex: 10 }} />}
                {invoice.file_type === 'pdf' ? (
                  <iframe
                    src={fileBlob?.url || ''}
                    style={{ width: '100%', height: '100%', minHeight: 500, border: 'none', display: 'block' }}
                    title="PDF Preview"
                  />
                ) : (
                  <img
                    src={fileBlob?.url || ''}
                    alt="Invoice"
                    style={{ width: '100%', objectFit: 'contain' }}
                  />
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Fullscreen Preview Modal */}
        <Modal
          open={fullscreenPreview}
          onCancel={() => setFullscreenPreview(false)}
          footer={null}
          width="92vw"
          style={{ top: 20 }}
          styles={{ body: { padding: 0, height: 'calc(90vh - 55px)' } }}
          title={invoice.file_name || '文件预览'}
        >
          {invoice.file_type === 'pdf' ? (
            <iframe
              src={fileBlob?.url || ''}
              style={{ width: '100%', height: '100%', border: 'none', display: 'block' }}
              title="PDF Preview Fullscreen"
            />
          ) : (
            <img
              src={fileBlob?.url || ''}
              alt="Invoice"
              style={{ width: '100%', height: '100%', objectFit: 'contain' }}
            />
          )}
        </Modal>
      </div>
    </div>
  );
}

export default InvoiceDetailPage;
