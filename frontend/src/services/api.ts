import axios from 'axios';
import type {
  Invoice,
  InvoiceDetail,
  InvoiceListResponse,
  Statistics,
  UploadResponse,
  LLMStatusResponse,
  LLMConfigRequest,
  LLMConfigResponse,
  LLMTestResponse,
  ModelsResponse,
  DuplicateCheckResponse,
} from '../types/invoice';

import type { AuthUser, TokenResponse, UserCreateRequest } from '../types/user';

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
});

// Attach JWT token to every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle 401: clear stored credentials and redirect to login
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('auth_token');
      localStorage.removeItem('auth_user');
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

// ── Auth API ────────────────────────────────────────────────────────────────

export const loginUser = async (username: string, password: string): Promise<TokenResponse> => {
  const response = await api.post('/auth/login', { username, password });
  return response.data;
};

export const registerUser = async (
  username: string,
  password: string,
  displayName?: string,
): Promise<TokenResponse> => {
  const response = await api.post('/auth/register', {
    username,
    password,
    display_name: displayName || undefined,
  });
  return response.data;
};

export const getMe = async (): Promise<AuthUser> => {
  const response = await api.get('/auth/me');
  return response.data;
};

export const changePassword = async (oldPassword: string, newPassword: string): Promise<void> => {
  await api.post('/auth/change-password', { old_password: oldPassword, new_password: newPassword });
};

export const listUsers = async (): Promise<AuthUser[]> => {
  const response = await api.get('/auth/users');
  return response.data;
};

export const createUser = async (data: UserCreateRequest): Promise<AuthUser> => {
  const response = await api.post('/auth/users', data);
  return response.data;
};

export const resetUserPassword = async (userId: number, newPassword: string): Promise<void> => {
  await api.put(`/auth/users/${userId}/reset-password`, { new_password: newPassword });
};

export const deleteUser = async (userId: number): Promise<void> => {
  await api.delete(`/auth/users/${userId}`);
};

// Health check
export const healthCheck = async () => {
  const response = await api.get('/health');
  return response.data;
};

// Upload invoices
export const uploadInvoices = async (files: File[], processingMode: string = 'llm_only'): Promise<UploadResponse[]> => {
  const formData = new FormData();
  files.forEach((file) => {
    formData.append('files', file);
  });
  formData.append('processing_mode', processingMode);

  const response = await api.post('/invoices/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data;
};

// List invoices
export interface ListParams {
  company_key?: string;
  party_type?: 'company' | 'person';
  buyer_keyword?: string;
  page?: number;
  page_size?: number;
  status?: string;
  owner?: string;
  start_date?: string;
  end_date?: string;
  approval_number?: string;
}

export const listInvoices = async (params: ListParams = {}): Promise<InvoiceListResponse> => {
  const response = await api.get('/invoices', { params });
  return response.data;
};

export interface ParsingStatus {
  queued: number;
  processing: number;
  pending: number;
  reviewing: number;
  confirmed: number;
  archived_vouchers: number;
}

export const getParsingStatus = async (): Promise<ParsingStatus> => {
  const response = await api.get('/invoices/processing-status');
  return response.data;
};

// Get invoice detail
export const getInvoice = async (id: number): Promise<InvoiceDetail> => {
  const response = await api.get(`/invoices/${id}`);
  return response.data;
};

export const getAdjacentInvoices = async (id: number): Promise<{ prev_id: number | null; next_id: number | null }> => {
  const response = await api.get(`/invoices/${id}/adjacent`);
  return response.data;
};

// Fetch invoice file as blob (sends auth header, avoids 401)
export const fetchInvoiceFileBlob = async (id: number): Promise<{ url: string; filename: string }> => {
  const response = await api.get(`/invoices/${id}/file`, { responseType: 'blob' });
  const disposition: string = response.headers['content-disposition'] || '';
  const match = disposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';\n]+)/i);
  const filename = match ? decodeURIComponent(match[1]) : `invoice-${id}`;
  const url = URL.createObjectURL(response.data as Blob);
  return { url, filename };
};

// Trigger file download using pre-fetched blob URL
export const downloadBlobUrl = (url: string, filename: string): void => {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
};

// Export invoices (CSV / Excel) via axios so auth header is sent
export const downloadInvoiceExport = async (
  format: 'csv' | 'excel',
  params: URLSearchParams,
  filename?: string,
): Promise<void> => {
  const response = await api.get(`/invoices/export/${format}?${params.toString()}`, { responseType: 'blob' });
  const ext = format === 'csv' ? 'csv' : 'xlsx';
  const url = URL.createObjectURL(response.data as Blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || `invoices.${ext}`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

// Update invoice
export const updateInvoice = async (
  id: number,
  data: Partial<Invoice>
): Promise<Invoice> => {
  const response = await api.put(`/invoices/${id}`, data);
  return response.data;
};

// Batch update
export const batchUpdateInvoices = async (
  invoiceIds: number[],
  status?: string,
  owner?: string
): Promise<{ message: string; updated_count: number }> => {
  const response = await api.post('/invoices/batch-update', {
    invoice_ids: invoiceIds,
    status,
    owner,
  });
  return response.data;
};

// Delete invoice
export const deleteInvoice = async (id: number): Promise<void> => {
  await api.delete(`/invoices/${id}`);
};

// Batch delete invoices
export const batchDeleteInvoices = async (
  invoiceIds: number[]
): Promise<{ message: string; deleted_count: number }> => {
  const response = await api.post('/invoices/batch-delete', {
    invoice_ids: invoiceIds,
  });
  return response.data;
};

// Get statistics
export const getStatistics = async (
  invoiceIds?: number[],
  status?: string,
  owner?: string
): Promise<Statistics> => {
  const params: Record<string, string> = {};
  if (invoiceIds && invoiceIds.length > 0) {
    params.invoice_ids = invoiceIds.join(',');
  }
  if (status) {
    params.status = status;
  }
  if (owner) {
    params.owner = owner;
  }

  const response = await api.get('/invoices/statistics', { params });
  return response.data;
};

// Resolve parsing diff
export const resolveDiff = async (
  invoiceId: number,
  diffId: number,
  source: 'ocr' | 'llm' | 'custom',
  customValue?: string
): Promise<{ message: string; field_name: string; final_value: string; all_resolved: boolean }> => {
  const response = await api.post(`/invoices/${invoiceId}/diffs/${diffId}/resolve`, {
    source,
    custom_value: customValue,
  });
  return response.data;
};

// Confirm invoice
export const confirmInvoice = async (
  invoiceId: number
): Promise<{ message: string; resolved_count: number }> => {
  const response = await api.post(`/invoices/${invoiceId}/confirm`);
  return response.data;
};

export const unlockInvoice = async (invoiceId: number): Promise<{ message: string }> => {
  const response = await api.post(`/invoices/${invoiceId}/unlock`);
  return response.data;
};

// Batch confirm invoices
export const batchConfirmInvoices = async (
  invoiceIds: number[]
): Promise<{ success: number; failed: number; errors: string[] }> => {
  const results = await Promise.allSettled(
    invoiceIds.map((id) => api.post(`/invoices/${id}/confirm`))
  );
  const success = results.filter((r) => r.status === 'fulfilled').length;
  const failed = results.filter((r) => r.status === 'rejected').length;
  const errors = results
    .filter((r): r is PromiseRejectedResult => r.status === 'rejected')
    .map((r) => String(r.reason));
  return { success, failed, errors };
};

export const archiveInvoiceAsVoucher = async (
  invoiceId: number,
): Promise<{ message: string }> => {
  const response = await api.post(`/invoices/${invoiceId}/archive-voucher`);
  return response.data;
};

export const batchArchiveVouchers = async (
  invoiceIds: number[],
): Promise<{ message: string; archived_count: number }> => {
  const response = await api.post('/invoices/batch-archive-vouchers', {
    invoice_ids: invoiceIds,
  });
  return response.data;
};

// Re-process invoice (run OCR/LLM again)
export const reprocessInvoice = async (
  invoiceId: number
): Promise<{ message: string; invoice_id: number }> => {
  const response = await api.post(`/invoices/${invoiceId}/process`);
  return response.data;
};

// Batch reprocess invoices (keep old results until replacement succeeds)
export const batchReprocessInvoices = async (
  invoiceIds: number[]
): Promise<{ message: string; count: number }> => {
  const response = await api.post('/invoices/batch-reprocess', {
    invoice_ids: invoiceIds,
  });
  return response.data;
};

export const batchUnlockInvoices = async (
  invoiceIds: number[]
): Promise<{ message: string; unlocked: number; skipped: number }> => {
  const response = await api.post('/invoices/batch-unlock', { invoice_ids: invoiceIds });
  return response.data;
};

export const batchSyncInvoices = async (
  params: Record<string, string>
): Promise<{ message: string; count: number }> => {
  const response = await api.post('/invoices/batch-sync', null, { params });
  return response.data;
};

// Check duplicates
export const checkDuplicates = async (): Promise<DuplicateCheckResponse> => {
  const response = await api.get('/invoices/duplicates');
  return response.data;
};

// LLM Configuration APIs

// Get LLM status
export const getLLMStatus = async (): Promise<LLMStatusResponse> => {
  const response = await api.get('/settings/llm/status');
  return response.data;
};

// Configure LLM provider
export const configureLLM = async (
  config: LLMConfigRequest,
  configToken?: string
): Promise<LLMConfigResponse> => {
  const response = await api.post('/settings/llm/configure', config, {
    headers: configToken ? { 'X-LLM-Config-Token': configToken } : undefined,
  });
  return response.data;
};

// Test LLM connection
export const testLLMConnection = async (): Promise<LLMTestResponse> => {
  const response = await api.post('/settings/llm/test');
  return response.data;
};

// Test LLM config before saving (uses actual provider client library)
export const testLLMConfig = async (
  config: { provider: string; api_key: string; model?: string; base_url?: string }
): Promise<{ success: boolean; message: string; response_time_ms?: number }> => {
  const response = await api.post('/settings/llm/test-config', config);
  return response.data;
};

// Get available models for a provider
export const getAvailableModels = async (
  provider?: string,
  visionOnly: boolean = false
): Promise<ModelsResponse> => {
  const params: Record<string, string | boolean> = {};
  if (provider) {
    params.provider = provider;
  }
  if (visionOnly) {
    params.vision_only = true;
  }
  const response = await api.get('/settings/models', { params });
  return response.data;
};

export default api;

export interface CompanyGroup {
  key: string;
  name: string | null;
  tax_id: string | null;
  invoice_count: number;
  total_amount: string;
  missing_amount_count: number;
  review_count: number;
}
export const listCompanies = async (): Promise<{ items: CompanyGroup[] }> => {
  return (await api.get('/invoices/companies')).data;
};

export interface PersonGroup {
  key: string;
  name: string;
  invoice_count: number;
  total_amount: string;
  missing_amount_count: number;
  review_count: number;
}
export const listPersons = async (): Promise<{ items: PersonGroup[] }> => {
  return (await api.get('/invoices/persons')).data;
};
export const startDingTalkApprovalImport = async () => (await api.post('/dingtalk-import/run')).data;
export const startDingTalkApprovalNumberImport = async (approvalNumber: string) => (await api.post('/dingtalk-import/run-approval', { approval_number: approvalNumber })).data;
export const getDingTalkApprovalImportStatus = async () => (await api.get('/dingtalk-import/status')).data;
export const listDingTalkApprovals = async (): Promise<{ items: Array<{ approval_number: string; owner?: string; status?: string; submitted_at?: string | null }> }> => (await api.get('/dingtalk-import/approvals')).data;
export const listVouchers = async (): Promise<{ items: Invoice[]; total: number }> => (await api.get('/invoices/vouchers')).data;
