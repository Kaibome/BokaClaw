"use client";

import { memo, useMemo, type CSSProperties, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Lightweight Markdown renderer for agent chat messages.
 *
 * The dashboard deliberately avoids a Markdown dependency (see
 * `docs/dashboard/agent-chat-design.md`), so this module only covers the
 * subset that agent replies actually use: GFM tables, headings, fenced code,
 * lists, block quotes, rules, and inline emphasis/code/links.
 *
 * It renders React elements directly — never `dangerouslySetInnerHTML` — so
 * model output cannot inject markup.
 */

type Alignment = "left" | "center" | "right" | null;

type Block =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "heading"; level: number; text: string }
  | { kind: "code"; language: string | null; code: string }
  | { kind: "list"; ordered: boolean; start: number; items: string[] }
  | { kind: "table"; align: Alignment[]; header: string[]; rows: string[][] }
  | { kind: "quote"; lines: string[] }
  | { kind: "hr" };

const FENCE_RE = /^\s*(```|~~~)\s*([^\s`]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const RULE_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const LIST_RE = /^\s*(?:([-*+])|(\d{1,9})[.)])\s+(.*)$/;
const DELIMITER_CELL_RE = /^:?-{1,}:?$/;

function splitTableRow(line: string): string[] {
  const chars = Array.from(line.trim());
  let start = 0;
  let end = chars.length;
  if (chars[start] === "|") start += 1;
  if (end > start && chars[end - 1] === "|" && chars[end - 2] !== "\\") {
    end -= 1;
  }

  const cells: string[] = [];
  let current = "";
  let inCode = false;
  for (let i = start; i < end; i += 1) {
    const char = chars[i];
    if (char === "\\" && chars[i + 1] === "|") {
      current += "|";
      i += 1;
      continue;
    }
    if (char === "`") {
      inCode = !inCode;
      current += char;
      continue;
    }
    if (char === "|" && !inCode) {
      cells.push(current.trim());
      current = "";
      continue;
    }
    current += char;
  }
  cells.push(current.trim());
  return cells;
}

function isDelimiterRow(line: string): boolean {
  if (!line.includes("|")) return false;
  const cells = splitTableRow(line);
  return (
    cells.length > 0 && cells.every((cell) => DELIMITER_CELL_RE.test(cell))
  );
}

function tableAlignment(cell: string): Alignment {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  if (left && right) return "center";
  if (left) return "left";
  if (right) return "right";
  return null;
}

function isTableStart(lines: string[], index: number): boolean {
  const header = lines[index];
  const delimiter = lines[index + 1];
  if (!header?.includes("|") || !delimiter) return false;
  if (!isDelimiterRow(delimiter)) return false;
  return splitTableRow(header).length === splitTableRow(delimiter).length;
}

function parseBlocks(content: string): Block[] {
  const lines = content.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push({ kind: "paragraph", lines: paragraph });
      paragraph = [];
    }
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    if (line.trim() === "") {
      flushParagraph();
      continue;
    }

    const fence = FENCE_RE.exec(line);
    if (fence) {
      flushParagraph();
      const marker = fence[1];
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) {
        body.push(lines[i]);
        i += 1;
      }
      blocks.push({
        kind: "code",
        language: fence[2] || null,
        code: body.join("\n"),
      });
      continue;
    }

    const heading = HEADING_RE.exec(line);
    if (heading) {
      flushParagraph();
      blocks.push({
        kind: "heading",
        level: heading[1].length,
        text: heading[2].trim(),
      });
      continue;
    }

    if (RULE_RE.test(line)) {
      flushParagraph();
      blocks.push({ kind: "hr" });
      continue;
    }

    if (isTableStart(lines, i)) {
      flushParagraph();
      const header = splitTableRow(lines[i]);
      const align = splitTableRow(lines[i + 1]).map(tableAlignment);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length) {
        const rowLine = lines[i];
        if (rowLine.trim() === "" || !rowLine.includes("|")) break;
        if (FENCE_RE.test(rowLine)) break;
        const cells = splitTableRow(rowLine).slice(0, header.length);
        while (cells.length < header.length) cells.push("");
        rows.push(cells);
        i += 1;
      }
      // The loop stopped on the line after the table; the outer loop's
      // increment should not skip it.
      i -= 1;
      blocks.push({ kind: "table", align, header, rows });
      continue;
    }

    const quote = QUOTE_RE.exec(line);
    if (quote) {
      flushParagraph();
      const quoted = [quote[1]];
      while (i + 1 < lines.length) {
        const next = QUOTE_RE.exec(lines[i + 1]);
        if (!next) break;
        quoted.push(next[1]);
        i += 1;
      }
      blocks.push({ kind: "quote", lines: quoted });
      continue;
    }

    const listItem = LIST_RE.exec(line);
    if (listItem) {
      flushParagraph();
      const ordered = listItem[1] === undefined;
      const start = ordered ? Number(listItem[2]) : 1;
      const items: string[] = [];
      let cursor = i;
      while (cursor < lines.length) {
        const item = LIST_RE.exec(lines[cursor]);
        if (!item || (item[1] === undefined) !== ordered) break;
        items.push(item[3]);
        cursor += 1;
      }
      blocks.push({ kind: "list", ordered, start, items });
      i = cursor - 1;
      continue;
    }

    paragraph.push(line);
  }

  flushParagraph();
  return blocks;
}

const INLINE_SOURCE = [
  "(`+)([\\s\\S]*?)\\1", // 1,2 inline code
  "\\*\\*([\\s\\S]+?)\\*\\*", // 3 bold **
  "__([\\s\\S]+?)__", // 4 bold __
  "~~([\\s\\S]+?)~~", // 5 strikethrough
  "\\*([^*\\n]+?)\\*", // 6 italic *
  "(?<![A-Za-z0-9_])_([^_\\n]+?)_(?![A-Za-z0-9_])", // 7 italic _
  "\\[([^\\]]+)\\]\\(([^)\\s]+)\\)", // 8,9 link
].join("|");

const SAFE_HREF_RE = /^(?:https?:|mailto:)/i;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = new RegExp(INLINE_SOURCE, "g");
  let lastIndex = 0;
  let index = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    const key = `${keyPrefix}-${index}`;
    index += 1;

    if (match[2] !== undefined) {
      nodes.push(
        <code
          key={key}
          className="rounded bg-surface-default-tertiary px-1 py-0.5 font-dm-mono text-[0.9em] text-onSurface-default-primary"
        >
          {match[2]}
        </code>,
      );
    } else if (match[3] !== undefined || match[4] !== undefined) {
      const inner = match[3] ?? match[4] ?? "";
      nodes.push(
        <strong key={key} className="font-bold">
          {renderInline(inner, key)}
        </strong>,
      );
    } else if (match[5] !== undefined) {
      nodes.push(
        <del key={key} className="text-onSurface-default-secondary">
          {renderInline(match[5], key)}
        </del>,
      );
    } else if (match[6] !== undefined || match[7] !== undefined) {
      const inner = match[6] ?? match[7] ?? "";
      nodes.push(
        <em key={key} className="italic">
          {renderInline(inner, key)}
        </em>,
      );
    } else if (match[8] !== undefined && match[9] !== undefined) {
      const href = match[9];
      const label = renderInline(match[8], key);
      nodes.push(
        SAFE_HREF_RE.test(href) ? (
          <a
            key={key}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="break-all text-onSurface-default-brand-hover underline underline-offset-2"
          >
            {label}
          </a>
        ) : (
          <span key={key}>{label}</span>
        ),
      );
    }

    lastIndex = pattern.lastIndex;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

function paragraphNode(lines: string[], key: string) {
  return (
    <p key={key} className="break-words">
      {lines.map((line, i) => (
        <span key={`${key}-l-${i}`}>
          {renderInline(line, `${key}-l-${i}`)}
          {i < lines.length - 1 ? <br /> : null}
        </span>
      ))}
    </p>
  );
}

const HEADING_CLASS: Record<number, string> = {
  1: "text-lg font-bold",
  2: "text-base font-bold",
  3: "text-sm font-bold",
  4: "text-sm font-bold",
  5: "text-sm font-bold",
  6: "text-sm font-bold",
};

const ALIGN_STYLE: Record<Exclude<Alignment, null>, CSSProperties> = {
  left: { textAlign: "left" },
  center: { textAlign: "center" },
  right: { textAlign: "right" },
};

function blockNode(block: Block, index: number): ReactNode {
  const key = `block-${index}`;

  switch (block.kind) {
    case "paragraph":
      return paragraphNode(block.lines, key);

    case "heading": {
      const Tag = `h${Math.min(block.level, 6)}` as
        | "h1"
        | "h2"
        | "h3"
        | "h4"
        | "h5"
        | "h6";
      return (
        <Tag
          key={key}
          className={cn("break-words", HEADING_CLASS[block.level])}
        >
          {renderInline(block.text, key)}
        </Tag>
      );
    }

    case "code":
      return (
        <pre
          key={key}
          className="max-h-96 overflow-auto rounded-lg border border-memBorder-primary bg-surface-default-secondary p-2.5 font-dm-mono text-[11px] leading-5 text-onSurface-default-primary"
        >
          <code>{block.code}</code>
        </pre>
      );

    case "list": {
      const items = block.items.map((item, i) => (
        <li key={`${key}-i-${i}`} className="break-words">
          {renderInline(item, `${key}-i-${i}`)}
        </li>
      ));
      const listClassName = cn(
        "space-y-0.5 pl-5 marker:text-onSurface-default-secondary",
        block.ordered ? "list-decimal" : "list-disc",
      );
      return block.ordered ? (
        <ol key={key} start={block.start} className={listClassName}>
          {items}
        </ol>
      ) : (
        <ul key={key} className={listClassName}>
          {items}
        </ul>
      );
    }

    case "table":
      return (
        <div
          key={key}
          className="max-w-full overflow-x-auto rounded-lg border border-memBorder-primary"
        >
          <table className="w-full border-collapse text-left align-top text-sm">
            <thead className="bg-surface-default-secondary">
              <tr>
                {block.header.map((cell, i) => (
                  <th
                    key={`${key}-h-${i}`}
                    style={
                      block.align[i] ? ALIGN_STYLE[block.align[i]!] : undefined
                    }
                    className="whitespace-normal break-words border-b border-memBorder-primary px-2.5 py-1.5 font-semibold text-onSurface-default-primary"
                  >
                    {renderInline(cell, `${key}-h-${i}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr
                  key={`${key}-r-${rowIndex}`}
                  className={cn(
                    "border-b border-memBorder-primary last:border-b-0",
                    rowIndex % 2 === 1 && "bg-surface-default-secondary/50",
                  )}
                >
                  {row.map((cell, cellIndex) => (
                    <td
                      key={`${key}-r-${rowIndex}-c-${cellIndex}`}
                      style={
                        block.align[cellIndex]
                          ? ALIGN_STYLE[block.align[cellIndex]!]
                          : undefined
                      }
                      className="whitespace-normal break-words px-2.5 py-1.5 align-top text-onSurface-default-primary"
                    >
                      {renderInline(
                        cell,
                        `${key}-r-${rowIndex}-c-${cellIndex}`,
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );

    case "quote":
      return (
        <blockquote
          key={key}
          className="border-l-2 border-memBorder-primary pl-3 text-onSurface-default-secondary"
        >
          {paragraphNode(block.lines, key)}
        </blockquote>
      );

    case "hr":
      return <hr key={key} className="border-memBorder-primary" />;

    default:
      return null;
  }
}

export const MarkdownContent = memo(function MarkdownContent({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  const blocks = useMemo(() => parseBlocks(content), [content]);

  return (
    <div className={cn("flex flex-col gap-2 break-words", className)}>
      {blocks.map((block, index) => blockNode(block, index))}
    </div>
  );
});
