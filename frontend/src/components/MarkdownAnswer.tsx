import { Typography } from 'antd';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

const components: Components = {
  a: ({ href, children, title }) => href
    ? <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a>
    : <span>{children}</span>,
  table: ({ children }) => <div className="markdown-table"><table>{children}</table></div>,
};

export default function MarkdownAnswer({ content }: { content: string }) {
  return <Typography.Paragraph className="markdown-answer" copyable={{
    text: content, tooltips: ['复制 Markdown 原文', '已复制'],
  }}>
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} skipHtml>{content}</ReactMarkdown>
  </Typography.Paragraph>;
}
