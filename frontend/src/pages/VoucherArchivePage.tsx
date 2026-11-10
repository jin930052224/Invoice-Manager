import { useEffect, useState } from 'react';
import { Card, Empty, Table, Typography } from 'antd';
import { useNavigate } from 'react-router-dom';
import { listVouchers } from '../services/api';
import type { Invoice } from '../types/invoice';
export default function VoucherArchivePage() {
 const [rows,setRows]=useState<Invoice[]>([]); const navigate=useNavigate();
 useEffect(()=>{listVouchers().then(r=>setRows(r.items))},[]);
 return <div style={{padding:24}}><Typography.Title level={2}>凭证归档</Typography.Title><Typography.Paragraph type="secondary">仅作备份查看：系统识别为非发票的收据、付款截图和行程单会自动归档到此处，不参与钉钉同步。</Typography.Paragraph><Card><Table rowKey="id" dataSource={rows} locale={{emptyText:<Empty description="暂无归档凭证"/>}} columns={[{title:'文件名',dataIndex:'file_name'},{title:'审批编号',dataIndex:'approval_instance_id',render:v=>v||'-'},{title:'归属人',dataIndex:'owner',render:v=>v||'-'},{title:'状态',dataIndex:'status'},{title:'查看',render:(_,r:Invoice)=><a onClick={()=>navigate(`/invoices/${r.id}`)}>详情</a>}]} /></Card></div>
}
