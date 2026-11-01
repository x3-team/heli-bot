import { Fragment, useMemo, type MouseEvent, type ReactNode } from "react";
import { openLink } from "./desktop";
import { parseMarkdown, type Block, type Inline } from "./markdown";

function onLinkClick(event: MouseEvent<HTMLAnchorElement>, href: string) {
  event.preventDefault();
  void openLink(href).catch(() => undefined);
}

function inline(nodes: Inline[]): ReactNode[] {
  return nodes.map((node, i) => {
    switch (node.type) {
      case "text":
        return <Fragment key={i}>{node.text}</Fragment>;
      case "br":
        return <br key={i} />;
      case "code":
        return <code key={i}>{node.text}</code>;
      case "strong":
        return <strong key={i}>{inline(node.children)}</strong>;
      case "em":
        return <em key={i}>{inline(node.children)}</em>;
      case "link":
        return (
          <a
            key={i}
            href={node.href}
            title={node.href}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(event) => onLinkClick(event, node.href)}
          >
            {inline(node.children)}
          </a>
        );
    }
  });
}

function block(node: Block, i: number): ReactNode {
  switch (node.type) {
    case "p":
      return <p key={i}>{inline(node.inline)}</p>;
    case "h":
      return <p key={i} className="md-h">{inline(node.inline)}</p>;
    case "quote":
      return <blockquote key={i}>{inline(node.inline)}</blockquote>;
    case "code":
      return <pre key={i}><code>{node.text}</code></pre>;
    case "hr":
      return <hr key={i} />;
    case "ul":
      return <ul key={i}>{node.items.map((item, j) => <li key={j}>{inline(item)}</li>)}</ul>;
    case "ol":
      return (
        <ol key={i} start={node.start}>
          {node.items.map((item, j) => <li key={j}>{inline(item)}</li>)}
        </ol>
      );
  }
}

/** A chat reply rendered from Markdown; links open in the system browser. */
export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => parseMarkdown(text), [text]);
  return <div className="md">{blocks.map(block)}</div>;
}
