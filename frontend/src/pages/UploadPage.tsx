import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Upload, message, List, Tag, Radio, Tooltip, Input, Button } from 'antd';
import { UploadOutlined, CheckCircleOutlined, CloseCircleOutlined, InfoCircleOutlined } from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload/interface';
import { uploadInvoices, getLLMStatus, startDingTalkApprovalNumberImport, getDingTalkApprovalImportStatus } from '../services/api';
import type { UploadResponse, LLMStatusResponse } from '../types/invoice';
import styles from './UploadPage.module.css';

const { Dragger } = Upload;

function UploadPage() {
  const navigate = useNavigate();
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [processingMode, setProcessingMode] = useState<string>('llm_only');
  const [llmStatus, setLlmStatus] = useState<LLMStatusResponse | null>(null);

  useEffect(() => {
    getLLMStatus().then(setLlmStatus).catch(() => {});
  }, []);
  const [results, setResults] = useState<UploadResponse[]>([]);
  const [countdown, setCountdown] = useState<number | null>(null);
  const [approvalNumber, setApprovalNumber] = useState('');
  const [approvalImport, setApprovalImport] = useState<any>(null);
  const [approvalImporting, setApprovalImporting] = useState(false);

  useEffect(() => {
    if (!approvalImport || ['completed', 'completed_with_errors', 'failed', 'rejected', 'idle'].includes(approvalImport.status)) return;
    const timer = window.setInterval(() => getDingTalkApprovalImportStatus().then(setApprovalImport).catch(() => {}), 2000);
    return () => window.clearInterval(timer);
  }, [approvalImport]);

  const handleApprovalImport = async () => {
    if (!approvalNumber.trim()) { message.warning('请输入审批编号'); return; }
    setApprovalImporting(true);
    try {
      const job = await startDingTalkApprovalNumberImport(approvalNumber.trim());
      setApprovalImport(job);
      message.success('审批附件导入已启动');
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '审批附件导入失败');
    } finally { setApprovalImporting(false); }
  };

  // Auto-navigate to list after successful upload
  useEffect(() => {
    if (countdown === null) return;
    if (countdown <= 0) {
      navigate('/');
      return;
    }
    const timer = setTimeout(() => setCountdown((c) => (c !== null ? c - 1 : null)), 1000);
    return () => clearTimeout(timer);
  }, [countdown, navigate]);

  const handleUpload = async () => {
    if (fileList.length === 0) {
      message.warning('请先选择文件');
      return;
    }

    setUploading(true);
    try {
      const files = fileList.map((f) => f.originFileObj as File);
      const response = await uploadInvoices(files, processingMode);
      setResults(response);

      const successCount = response.filter((r) => r.status === 'success').length;
      if (successCount > 0) {
        message.success(`成功上传 ${successCount} 个文件，正在识别发票`);
        setFileList([]);
        setCountdown(5); // auto navigate in 5s
      }

      const failCount = response.filter((r) => r.status === 'error').length;
      if (failCount > 0) {
        message.error(`${failCount} 个文件上传失败`);
      }
    } catch (error) {
      message.error('上传失败，请重试');
      console.error(error);
    } finally {
      setUploading(false);
    }
  };

  const uploadProps = {
    multiple: true,
    accept: '.pdf,.jpg,.jpeg,.png',
    fileList,
    beforeUpload: (file: File) => {
      const isValid = ['application/pdf', 'image/jpeg', 'image/png'].includes(file.type);
      if (!isValid) {
        message.error('只支持 PDF、JPG、PNG 格式');
        return Upload.LIST_IGNORE;
      }

      const isLt10M = file.size / 1024 / 1024 < 10;
      if (!isLt10M) {
        message.error('文件大小不能超过 10MB');
        return Upload.LIST_IGNORE;
      }

      return false; // Prevent auto upload
    },
    onChange: ({ fileList: newFileList }: { fileList: UploadFile[] }) => {
      setFileList(newFileList);
    },
    onRemove: (file: UploadFile) => {
      setFileList(fileList.filter((f) => f.uid !== file.uid));
    },
  };

  return (
    <div className={styles.pageContainer}>
      <div className={styles.contentWrapper}>
        <header className={styles.pageHeader}>
          <h1 className={styles.pageTitle}>上传发票</h1>
        </header>

        <div className={styles.uploadCard}>
          <div className={styles.modeSelector}>
            <span className={styles.modeSelectorLabel}>
              处理模式
              <Tooltip title="OCR只读取图片文字，无需API费用；LLM视觉简单直接识别，精度更高；OCR+LLM双重校验推荐">
                <InfoCircleOutlined style={{ marginLeft: 4, color: '#8c8c8c', cursor: 'help' }} />
              </Tooltip>
            </span>
            <Radio.Group
              value={processingMode}
              onChange={(e) => setProcessingMode(e.target.value)}
              optionType="button"
              buttonStyle="solid"
            >
              <Radio.Button value="ocr_only">OCR 优先（智能补全）</Radio.Button>
              <Radio.Button value="ocr_and_llm">OCR + LLM（全量）</Radio.Button>
              <Tooltip title={llmStatus && !llmStatus.supports_vision ? '当前 LLM 不支持视觉，选择后将自动降级为 OCR + LLM 模式' : ''}>
                <Radio.Button value="llm_only">仅 LLM 视觉（高精度）</Radio.Button>
              </Tooltip>
            </Radio.Group>
          </div>
          <div className={styles.modeSelector}>
            <span className={styles.modeSelectorLabel}>按审批编号导入</span>
            <Input value={approvalNumber} onChange={(event) => setApprovalNumber(event.target.value)} placeholder="输入钉钉审批编号" style={{ width: 300 }} />
            <Button onClick={handleApprovalImport} loading={approvalImporting}>导入附件并解析</Button>
            {approvalImport && <span style={{ color: approvalImport.failed ? '#cf1322' : '#666' }}>状态：{approvalImport.failed ? '部分附件导入失败' : approvalImport.status} · 附件 {approvalImport.total} · 已导入 {approvalImport.invoices} · 跳过 {approvalImport.skipped} · 失败 {approvalImport.failed}</span>}
          </div>
          <div className={styles.uploadDropzone}>
            <Dragger {...uploadProps}>
              <div className="ant-upload-drag-icon">
                <span className={styles.uploadIcon}>
                  <UploadOutlined />
                </span>
              </div>
              <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
              <p className="ant-upload-hint">
                支持 PDF、JPG、PNG 格式，单个文件最大 10MB，支持批量上传
              </p>
            </Dragger>
          </div>

          <div className={styles.actionButtons}>
            <button
              className={styles.uploadButton}
              onClick={handleUpload}
              disabled={fileList.length === 0 || uploading}
            >
              {uploading ? '上传中...' : '开始上传'}
            </button>
            <button
              className={styles.backButton}
              onClick={() => navigate('/')}
            >
              返回列表
            </button>
          </div>
        </div>

        {results.length > 0 && (
          <div className={styles.resultsCard}>
            <h2 className={styles.resultsTitle}>上传结果</h2>
            <List
              className={styles.resultsList}
              dataSource={results}
              renderItem={(item) => (
                <List.Item>
                  <List.Item.Meta
                    avatar={
                      item.status === 'success' ? (
                        <CheckCircleOutlined style={{ color: '#52c41a', fontSize: 24 }} />
                      ) : (
                        <CloseCircleOutlined style={{ color: '#ff4d4f', fontSize: 24 }} />
                      )
                    }
                    title={item.file_name}
                    description={item.message}
                  />
                  {item.status === 'success' && (
                    <Tag color="success">ID: {item.id}</Tag>
                  )}
                </List.Item>
              )}
            />
            <button
              className={styles.viewListButton}
              onClick={() => navigate('/')}
            >
              {countdown !== null && countdown > 0
                ? `查看发票列表（${countdown}s 后自动跳转）`
                : '查看发票列表'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default UploadPage;
