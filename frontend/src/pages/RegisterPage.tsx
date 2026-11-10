import { useState } from 'react';
import { Form, Input, Button, message } from 'antd';
import { UserOutlined, LockOutlined, IdcardOutlined } from '@ant-design/icons';
import { useNavigate, Link } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { registerUser } from '../services/api';
import styles from './RegisterPage.module.css';

interface RegisterForm {
  username: string;
  display_name?: string;
  password: string;
  confirm_password: string;
}

export default function RegisterPage() {
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (values: RegisterForm) => {
    setLoading(true);
    try {
      const data = await registerUser(values.username, values.password, values.display_name);
      login(data.access_token, data.user);
      message.success('注册成功，欢迎使用！');
      navigate('/');
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      message.error(detail || '注册失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.header}>
          <div className={styles.logoRow}>
            <div className={styles.logoMark} />
            <span className={styles.logoText}>库次发票管理</span>
          </div>
          <h1 className={styles.title}>创建账号</h1>
          <p className={styles.subtitle}>填写信息以完成注册</p>
        </div>

        <Form layout="vertical" onFinish={handleSubmit} size="large">
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { min: 2, max: 20, message: '用户名长度为 2-20 位' },
              { pattern: /^[a-zA-Z0-9_]+$/, message: '只允许字母、数字和下划线' },
            ]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="字母、数字或下划线"
              autoComplete="username"
            />
          </Form.Item>

          <Form.Item
            name="display_name"
            label={<span>姓名 / 昵称 <span style={{ color: 'var(--muted-foreground)', fontWeight: 400, fontSize: 12 }}>（可选）</span></span>}
          >
            <Input
              prefix={<IdcardOutlined />}
              placeholder="显示在系统中的名称"
              autoComplete="name"
            />
          </Form.Item>

          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, message: '密码至少 6 位' },
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="至少 6 位"
              autoComplete="new-password"
            />
          </Form.Item>

          <Form.Item
            name="confirm_password"
            label="确认密码"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再次输入密码' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('password') === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error('两次输入的密码不一致'));
                },
              }),
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="再次输入密码"
              autoComplete="new-password"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              className={styles.submitButton}
            >
              注册
            </Button>
          </Form.Item>
        </Form>

        <div className={styles.footer}>
          已有账号？<Link to="/login" className={styles.footerLink}>立即登录</Link>
        </div>
      </div>
    </div>
  );
}
