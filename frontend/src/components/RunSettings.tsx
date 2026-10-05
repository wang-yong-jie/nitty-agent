import { Checkbox, Collapse, Input, InputNumber, Select, Switch } from 'antd';
import type { Capabilities, RunOptions } from '../api/client';

type Props = { value: RunOptions; onChange: (value: RunOptions) => void; capabilities: Capabilities | null };

export const defaultOptions: RunOptions = {
  provider: 'deepseek', model: 'deepseek-flash', workdir: null, base_url: null,
  desktop: false, with_local_tools: false, vision_mode: 'direct',
  vision_provider: null, vision_model: null, vision_base_url: null,
  max_turns: null, screenshot_size: 1600,
  stability_timeout: 3, desktop_recovery_limit: 6, record_desktop: false,
};

export default function RunSettings({ value, onChange, capabilities }: Props) {
  const set = (patch: Partial<RunOptions>) => onChange({ ...value, ...patch });
  const providerOptions = ['deepseek', 'openai', 'claude'].map(provider => ({
    value: provider, label: `${provider === 'deepseek' ? 'DeepSeek' : provider === 'openai' ? 'OpenAI' : 'Claude'}${capabilities?.providers[provider] ? '' : ' · 未配置密钥'}`,
  }));
  return <div className="settings">
    <div className="field-grid">
      <label>模型提供方<Select aria-label="模型提供方" value={value.provider} options={providerOptions}
        onChange={provider => set({ provider, model: provider === 'deepseek' ? 'deepseek-flash' : '' })} /></label>
      <label>模型名称<Input aria-label="模型名称" value={value.model ?? ''} placeholder="填写有权限使用的模型"
        onChange={event => set({ model: event.target.value })} /></label>
    </div>
    <label className="field">工作目录 <span className="optional">可选</span>
      <Input aria-label="工作目录" value={value.workdir ?? ''} placeholder="留空时按需创建临时目录"
        onChange={event => set({ workdir: event.target.value || null })} />
    </label>
    <div className="desktop-toggle"><div><strong>桌面操作</strong><p>截图、鼠标和键盘 · Windows 主显示器</p></div>
      <Switch aria-label="启用桌面操作" checked={value.desktop} disabled={!capabilities?.desktop_available}
        onChange={desktop => set(desktop ? { desktop } : {
          desktop, record_desktop: false, with_local_tools: false, vision_mode: 'direct', vision_provider: null, vision_model: null, vision_base_url: null,
        })} /></div>
    {value.desktop && <div className="desktop-options">
      <p className="desktop-note">运行时请保持桌面解锁。F8 或将鼠标移至主屏幕角落可急停；direct 模式需要主模型支持图片。</p>
      <Checkbox checked={value.with_local_tools} onChange={event => set({ with_local_tools: event.target.checked })}>同时启用文件和 Shell 工具</Checkbox>
      <div className="field"><Checkbox checked={value.record_desktop} onChange={event => set({ record_desktop: event.target.checked })}>记录操作前后截图</Checkbox>
        <p className="muted">截图保存在本机，执行详情可查看点击标记；删除会话时一并清理。</p></div>
      <label className="field">视觉模式<Select aria-label="视觉模式" value={value.vision_mode}
        options={[{ value: 'direct', label: '主模型直接看图' }, { value: 'separate', label: '独立视觉模型' }]}
        onChange={vision_mode => set(vision_mode === 'direct' ? {
          vision_mode, vision_provider: null, vision_model: null, vision_base_url: null,
        } : { vision_mode, vision_provider: value.provider ?? 'deepseek' })} /></label>
      {value.vision_mode === 'separate' && <>
        <div className="field-grid">
          <label>视觉提供方<Select aria-label="视觉提供方" value={value.vision_provider ?? value.provider}
            options={providerOptions} onChange={vision_provider => set({ vision_provider })} /></label>
          <label>视觉模型<Input aria-label="视觉模型" value={value.vision_model ?? ''} placeholder="或使用 VISION_MODEL"
            onChange={event => set({ vision_model: event.target.value || null })} /></label>
        </div>
        <label className="field">视觉服务地址<Input aria-label="视觉服务地址" value={value.vision_base_url ?? ''}
          placeholder="可选，或使用 VISION_BASE_URL" onChange={event => set({ vision_base_url: event.target.value || null })} /></label>
      </>}
    </div>}
    <Collapse ghost items={[{ key: 'advanced', label: '高级设置', children: <>
      <label className="field">模型服务地址<Input aria-label="模型服务地址" value={value.base_url ?? ''} placeholder="留空使用提供方默认地址"
        onChange={event => set({ base_url: event.target.value || null })} /></label>
      <div className="field-grid">
        <label>最大轮次<InputNumber aria-label="最大轮次" min={1} max={1000} value={value.max_turns}
          placeholder={value.desktop ? '默认 50' : '默认 10'} onChange={max_turns => set({ max_turns })} /></label>
        {value.desktop && <label>截图最长边<InputNumber aria-label="截图最长边" min={640} max={3840} step={160}
          value={value.screenshot_size} onChange={screenshot_size => set({ screenshot_size: screenshot_size ?? 1600 })} /></label>}
      </div>
      {value.desktop && <div className="field-grid field">
        <label>等待画面稳定上限（秒）<InputNumber aria-label="等待画面稳定上限" min={0.5} max={10} step={0.5}
          value={value.stability_timeout ?? 3} onChange={stability_timeout => set({ stability_timeout: stability_timeout ?? 3 })} /></label>
        <label>无进展动作上限<InputNumber aria-label="无进展动作上限" min={3} max={20}
          value={value.desktop_recovery_limit ?? 6} onChange={desktop_recovery_limit => set({ desktop_recovery_limit: desktop_recovery_limit ?? 6 })} /></label>
      </div>}
    </> }]} />
  </div>;
}
