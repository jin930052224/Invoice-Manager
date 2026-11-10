import { useState } from 'react';
import { Form, Input, Button, message } from 'antd';
import { UserOutlined, LockOutlined } from '@ant-design/icons';
import { useNavigate, Link } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { loginUser } from '../services/api';
import styles from './LoginPage.module.css';

export default function LoginPage() {
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const data = await loginUser(values.username, values.password);
      login(data.access_token, data.user);
      navigate('/');
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      message.error(detail || '登录失败，请检查用户名和密码');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.loginPage}>
      <div className={styles.card}>
        <div className={styles.header}>
          <div className={styles.logoRow}>
            <div className={styles.logoMark} />
            <span className={styles.logoText}>库次发票管理</span>
          </div>
          <h1 className={styles.title}>欢迎回来</h1>
          <p className={styles.subtitle}>请登录您的账号以继续</p>
        </div>

        <Form layout="vertical" onFinish={handleSubmit} size="large">
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input prefix={<UserOutlined />} placeholder="请输入用户名" autoComplete="username" />
          </Form.Item>

          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="请输入密码"
              autoComplete="current-password"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              className={styles.submitButton}
            >
              登录
            </Button>
          </Form.Item>
        </Form>

        <div className={styles.footer}>
          没有账号？<Link to="/register" className={styles.footerLink}>立即注册</Link>
        </div>
      </div>
    </div>
  );
}
