import { useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { Tooltip, Dropdown, Modal, Form, Input, message } from 'antd';
import type { MenuProps } from 'antd';
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  LogoutOutlined,
  KeyOutlined,
  TeamOutlined,
  DownOutlined,
} from '@ant-design/icons';
import { useAuth } from '../../../contexts/AuthContext';
import { changePassword } from '../../../services/api';
import styles from './Sidebar.module.css';

interface NavItem {
  path: string;
  label: string;
}

interface SidebarProps {
  llmConfigured: boolean | null;
  onOpenLLMConfig: () => void;
}

const navItems: NavItem[] = [
  { path: '/', label: '发票列表' },
  { path: '/reviews', label: '待审核' },
  { path: '/companies', label: '公司归类' },
  { path: '/persons', label: '个人归类' },
  { path: '/approvals', label: '审批编号归类' },
];

export default function Sidebar({ llmConfigured, onOpenLLMConfig }: SidebarProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [changePwOpen, setChangePwOpen] = useState(false);
  const [form] = Form.useForm();

  const displayName = user?.display_name || user?.username || '未知用户';
  const initial = displayName.charAt(0).toUpperCase();

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const handleChangePw = async (values: { old_password: string; new_password: string }) => {
    try {
      await changePassword(values.old_password, values.new_password);
      message.success('密码修改成功，请重新登录');
      form.resetFields();
      setChangePwOpen(false);
      handleLogout();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '修改失败');
    }
  };

  const userMenuItems: MenuProps['items'] = [
    {
      key: 'change-pw',
      icon: <KeyOutlined />,
      label: '修改密码',
      onClick: () => setChangePwOpen(true),
    },
    ...(user?.is_admin
      ? [{
          key: 'users',
          icon: <TeamOutlined />,
          label: '用户管理',
          onClick: () => navigate('/admin/users'),
        }]
      : []),
    { type: 'divider' as const },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      danger: true,
      onClick: handleLogout,
    },
  ];

  return (
    <aside className={styles.sidebar}>
      {/* Top Section: Logo + Navigation */}
      <div className={styles.sidebarTop}>
        {/* Logo */}
        <div className={styles.logo}>
          <div className={styles.logoMark} />
          <span className={styles.logoText}>库次发票管理</span>
        </div>

        {/* Navigation */}
        <nav className={styles.navigation}>
          {navItems.map((item) => {
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`${styles.navItem} ${isActive ? styles.active : ''}`}
              >
                <span className={styles.navDot} />
                <span className={styles.navLabel}>{item.label}</span>
              </Link>
            );
          })}

          {/* Settings (LLM Config) - appears as nav item */}
          <Tooltip title={llmConfigured ? 'LLM服务已配置' : 'LLM服务未配置，点击配置'} placement="right">
            <button
              className={styles.navItem}
              onClick={onOpenLLMConfig}
              style={{ border: 'none', background: 'none', width: '100%', textAlign: 'left' }}
            >
              <span className={styles.navDot} />
              <span className={styles.navLabel}>
                设置
                {llmConfigured === false && (
                  <ExclamationCircleOutlined
                    style={{ marginLeft: 8, color: 'var(--warning)', fontSize: 12 }}
                  />
                )}
                {llmConfigured === true && (
                  <CheckCircleOutlined
                    style={{ marginLeft: 8, color: 'var(--success)', fontSize: 12 }}
                  />
                )}
              </span>
            </button>
          </Tooltip>
        </nav>
      </div>

      {/* Bottom Section: User Profile */}
      <div className={styles.sidebarBottom}>
        <Dropdown menu={{ items: userMenuItems }} trigger={['click']} placement="topLeft">
          <div className={styles.userProfile} style={{ cursor: 'pointer' }}>
            <div className={styles.userAvatar}>
              <span className={styles.userInitial}>{initial}</span>
            </div>
            <div className={styles.userInfo}>
              <span className={styles.userName}>{displayName}</span>
              <span className={styles.userRole}>{user?.is_admin ? '管理员' : '普通用户'}</span>
            </div>
            <DownOutlined style={{ fontSize: 10, color: 'var(--muted-foreground)', marginLeft: 'auto' }} />
          </div>
        </Dropdown>
      </div>

      {/* Change Password Modal */}
      <Modal
        title="修改密码"
        open={changePwOpen}
        onCancel={() => { setChangePwOpen(false); form.resetFields(); }}
        onOk={() => form.submit()}
        okText="确认修改"
        cancelText="取消"
      >
        <Form form={form} layout="vertical" onFinish={handleChangePw} style={{ marginTop: 16 }}>
          <Form.Item name="old_password" label="原密码" rules={[{ required: true, message: '请输入原密码' }]}>
            <Input.Password placeholder="请输入原密码" />
          </Form.Item>
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[{ required: true, message: '请输入新密码' }, { min: 6, message: '至少6位' }]}
          >
            <Input.Password placeholder="至少6位" />
          </Form.Item>
        </Form>
      </Modal>
    </aside>
  );
}
