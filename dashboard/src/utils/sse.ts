export interface SseMessage {
  event: string;
  data: string;
  id?: string;
}

function parseBlock(block: string): SseMessage | null {
  let event = "message";
  let id: string | undefined;
  const dataLines: string[] = [];

  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;

    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") {
      event = value;
    } else if (field === "id") {
      id = value;
    } else if (field === "data") {
      dataLines.push(value);
    }
  }

  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n"), id };
}

/**
 * Incremental `text/event-stream` parser. Feed it decoded chunks; it returns
 * only the events completed so far. Handles CRLF, multi-line `data:` fields,
 * comments/heartbeats, and events split across chunk boundaries.
 */
export class SseParser {
  private buffer = "";

  push(chunk: string): SseMessage[] {
    this.buffer += chunk.replace(/\r\n?/g, "\n");

    const messages: SseMessage[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const message = parseBlock(this.buffer.slice(0, boundary));
      if (message) messages.push(message);
      this.buffer = this.buffer.slice(boundary + 2);
      boundary = this.buffer.indexOf("\n\n");
    }
    return messages;
  }

  flush(): SseMessage[] {
    const rest = this.buffer;
    this.buffer = "";
    const message = parseBlock(rest);
    return message ? [message] : [];
  }
}
