import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import CompanyPage from './pages/CompanyPage';
import PersonPage from './pages/PersonPage';
import ApprovalPage from './pages/ApprovalPage';
import ApprovalInvoiceListPage from './pages/ApprovalInvoiceListPage';
import VoucherArchivePage from './pages/VoucherArchivePage';
import ReviewQueuePage from './pages/ReviewQueuePage';
import UploadPage from './pages/UploadPage';
import InvoiceListPage from './pages/InvoiceListPage';
import InvoiceDetailPage from './pages/InvoiceDetailPage';
import UserManagePage from './pages/UserManagePage';
import LoginPage from './pages/LoginPage';
import RegisterPage from './pages/RegisterPage';
import LLMConfigModal from './components/LLMConfigModal';
import { MainLayout } from './components/layout';
import { getLLMStatus } from './services/api';
import { AuthProvider, useAuth } from './contexts/AuthContext';

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function AppContent() {
  const [llmConfigOpen, setLlmConfigOpen] = useState(false);
  const [llmConfigured, setLlmConfigured] = useState<boolean | null>(null);
  const [showLlmPromo, setShowLlmPromo] = useState(false);
  const { isAuthenticated } = useAuth();

  // Check LLM status after authentication
  useEffect(() => {
    if (!isAuthenticated) return;
    const checkLLMStatus = async () => {
      try {
        const status = await getLLMStatus();
        setLlmConfigured(status.is_configured);
        if (!status.is_configured) {
          setShowLlmPromo(true);
        }
      } catch (error) {
        console.error('Failed to check LLM status:', error);
        setLlmConfigured(false);
        setShowLlmPromo(true);
      }
    };
    checkLLMStatus();
  }, [isAuthenticated]);

  const handleLLMConfigured = () => {
    setLlmConfigured(true);
    setShowLlmPromo(false);
  };

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route
        path="/*"
        element={
          <ProtectedRoute>
            <MainLayout
              llmConfigured={llmConfigured}
              showLlmPromo={showLlmPromo}
              onOpenLLMConfig={() => setLlmConfigOpen(true)}
              onCloseLLMPromo={() => setShowLlmPromo(false)}
            >
              <Routes>
                <Route path="/" element={<InvoiceListPage />} />
                <Route path="/reviews" element={<ReviewQueuePage />} />
                <Route path="/companies" element={<CompanyPage />} />
                <Route path="/persons" element={<PersonPage />} />
                <Route path="/approvals" element={<ApprovalPage />} />
                <Route path="/approvals/:approvalNumber/invoices" element={<ApprovalInvoiceListPage />} />
                <Route path="/vouchers" element={<VoucherArchivePage />} />
                <Route path="/upload" element={<UploadPage />} />
                <Route path="/invoices/:id" element={<InvoiceDetailPage />} />
                <Route path="/admin/users" element={<UserManagePage />} />
              </Routes>

              <LLMConfigModal
                open={llmConfigOpen}
                onClose={() => setLlmConfigOpen(false)}
                onConfigured={handleLLMConfigured}
              />
            </MainLayout>
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppContent />
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
