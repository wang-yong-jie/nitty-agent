import { Alert } from 'antd';
import type { Task } from '../api/client';

export default function VerificationNotice({ verification }: { verification: Task['verification'] }) {
  if (!verification) return null;
  const achieved = verification.status === 'achieved';
  return <Alert className="verification-notice" showIcon type={achieved ? 'success' : 'warning'}
    title={achieved ? '目标已验证' : verification.status === 'unmet' ? '目标尚未达到' : '目标结果尚不确定'}
    description={<><div>{verification.evidence}</div><small>验证期望：{verification.expected}</small></>} />;
}
