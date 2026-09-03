import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

// Markdown renderer for chat messages. Lazy-loaded by RunTab so react-markdown +
// remark/rehype + highlight.js land in a separate chunk (not the initial bundle).
//
// NOTE on code rendering: react-markdown v9+ removed the `inline` prop, so the only
// reliable way to style fenced blocks is to override `pre` (the block wrapper) and
// leave `code` to render as a normal <code>. That keeps inline `code` inline and lets
// rehype-highlight add `.hljs` classes to block code (themed in theme.css).
export default function Markdown({ children }) {
  if (!children) return null;
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={{
        pre({ children }) {
          return <pre className="pre">{children}</pre>;
        },
        table({ children }) {
          return (
            <div className="table-wrap">
              <table className="table">{children}</table>
            </div>
          );
        },
        a({ href, children }) {
          return (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
        },
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
