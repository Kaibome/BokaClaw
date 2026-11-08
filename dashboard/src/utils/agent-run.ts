import { postStream } from "@/utils/api";
import { AGENT_ENDPOINTS } from "@/utils/api-endpoints";
import { SseParser } from "@/utils/sse";

interface StartAgentRunFallbacks {
  emptyResponse?: string;
  startFailed?: string;
  noRunId?: string;
}

interface StartAgentRunOptions {
  message: string;
  metadata?: Record<string, unknown>;
  /** Localized fallbacks for errors that carry no server-supplied detail. */
  fallbacks?: StartAgentRunFallbacks;
}

interface MetaEvent {
  run_id?: string;
}

interface ErrorEvent {
  detail?: string;
  message?: string;
  code?: string;
}

/**
 * Start one background agent run and return its server-generated run id.
 *
 * Execution belongs to the server after `POST /agent` returns; once the `meta`
 * event arrives the reader is aborted so a long generation does not hold a
 * browser connection open. The run keeps going and is polled separately.
 */
export async function startAgentRun({
  message,
  metadata,
  fallbacks,
}: StartAgentRunOptions): Promise<string> {
  const controller = new AbortController();
  const response = await postStream(
    AGENT_ENDPOINTS.INVOKE,
    {
      conversation_id: null,
      message,
      stream: true,
      ...(metadata && Object.keys(metadata).length > 0 ? { metadata } : {}),
    },
    { signal: controller.signal },
  );

  if (!response.body) {
    throw new Error(
      fallbacks?.emptyResponse ?? "The agent returned an empty response.",
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const event of parser.push(
        decoder.decode(value, { stream: true }),
      )) {
        if (event.event === "meta") {
          const payload = parseJson<MetaEvent>(event.data);
          if (payload?.run_id) return payload.run_id;
        }
        if (event.event === "error") {
          const payload = parseJson<ErrorEvent>(event.data);
          throw new Error(
            payload?.detail ??
              payload?.message ??
              payload?.code ??
              fallbacks?.startFailed ??
              "The agent failed to start.",
          );
        }
      }
    }
  } finally {
    controller.abort();
    void reader.cancel().catch(() => {});
  }

  throw new Error(fallbacks?.noRunId ?? "The agent did not return a run id.");
}

function parseJson<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}
