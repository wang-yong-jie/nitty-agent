import { Image, Tag } from 'antd';
import { desktopArtifactUrl } from '../api/client';

type Trajectory = { step: number; before: string; after: string; target: string };
type Result = { trajectory?: Trajectory; stable?: boolean; timing_ms?: Record<string, number>;
  recovery?: { no_progress_steps: number; suggestion: string }; verification?: { status: string; evidence: string } };

export default function VisualStep({ taskId, result }: { taskId: string; result: Result }) {
  if (!result.trajectory && !result.verification && result.stable === undefined) return null;
  return <div className="visual-step">
    <div className="visual-step-facts">
      {result.stable !== undefined && <Tag color={result.stable ? 'green' : 'orange'}>{result.stable ? '画面稳定' : '画面尚未稳定'}</Tag>}
      {typeof result.timing_ms?.input === 'number' && typeof result.timing_ms?.settle_capture === 'number' &&
        <span>输入 {result.timing_ms.input.toFixed(0)} ms · 等待与截图 {result.timing_ms.settle_capture.toFixed(0)} ms</span>}
      {!!result.recovery?.no_progress_steps && <span>连续无进展 {result.recovery.no_progress_steps} 步 · {({ reobserve: '重新观察', reground: '重新定位', replan: '重新规划' } as Record<string, string>)[result.recovery.suggestion] ?? result.recovery.suggestion}</span>}
    </div>
    {result.verification && <p>{({ achieved: '已达到预期', unmet: '未达到预期', uncertain: '结果不确定' } as Record<string, string>)[result.verification.status]}：{result.verification.evidence}</p>}
    {result.trajectory && <Image.PreviewGroup><div className="visual-step-images">
      {(['target', 'after'] as const).map(kind => <figure key={kind}>
        <Image src={desktopArtifactUrl(taskId, result.trajectory![kind])} alt={kind === 'target' ? '动作前截图与点击标记' : '动作后截图'} />
        <figcaption>{kind === 'target' ? '动作前 · 点击标记' : '动作后'}</figcaption>
      </figure>)}
    </div></Image.PreviewGroup>}
  </div>;
}
