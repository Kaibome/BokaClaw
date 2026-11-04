import axios, { AxiosError, AxiosInstance } from "axios";

let cachedToken: string | null = null;
const LOGIN_PATH = "/login";

export const setAccessToken = (token: string | null) => {
  cachedToken = token;
};

export const getAccessToken = (): string | null => {
  return cachedToken;
};

const handleTokenError = () => {
  cachedToken = null;
};

const redirectToLogin = () => {
  if (typeof window !== "undefined") {
    window.location.href = LOGIN_PATH;
  }
};

const refreshAccessToken = async () => {
  const refreshResponse = await fetch("/api/auth/refresh", {
    method: "POST",
    credentials: "include",
  });

  if (!refreshResponse.ok) {
    return null;
  }

  const data = await refreshResponse.json();
  setAccessToken(data.access_token);
  return data.access_token as string;
};

const createApi = (): AxiosInstance & {
  postStream: (url: string, data: unknown) => Promise<Response>;
} => {
  const api = axios.create({
    baseURL: process.env.NEXT_PUBLIC_API_URL,
  });

  api.interceptors.request.use(
    async (config) => {
      if (cachedToken) {
        config.headers = config.headers ?? {};
        config.headers.Authorization = `Bearer ${cachedToken}`;
      }
      return config;
    },
    (error) => {
      return Promise.reject(error);
    },
  );

  api.interceptors.response.use(
    (response) => response,
    async (error: AxiosError<{ error?: string }>) => {
      if (error.response?.status === 401) {
        handleTokenError();

        try {
          const nextToken = await refreshAccessToken();
          if (nextToken && error.config) {
            error.config.headers = error.config.headers ?? {};
            error.config.headers.Authorization = `Bearer ${nextToken}`;
            return api.request(error.config);
          }
        } catch {}

        handleTokenError();
        redirectToLogin();
      }

      if (error.response?.data?.error) {
        return Promise.reject(error.response.data.error);
      }

      return Promise.reject(error);
    },
  );

  return Object.assign(api, { postStream });
};

export class ApiStreamError extends Error {
  status: number;
  code?: string;
  data: Record<string, unknown> | null;

  constructor(
    message: string,
    status: number,
    code?: string,
    data?: Record<string, unknown> | null,
  ) {
    super(message);
    this.name = "ApiStreamError";
    this.status = status;
    this.code = code;
    this.data = data ?? null;
  }
}

interface StreamRequestOptions {
  method: "GET" | "POST";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

const requestStream = (
  url: string,
  token: string | null,
  options: StreamRequestOptions,
) => {
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    Authorization: token ? `Bearer ${token}` : "",
  };
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
  }
  if (options.headers) Object.assign(headers, options.headers);

  return fetch(`${process.env.NEXT_PUBLIC_API_URL}${url}`, {
    method: options.method,
    headers,
    body: options.method === "POST" ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });
};

const performStreamRequest = async (
  url: string,
  options: StreamRequestOptions,
): Promise<Response> => {
  let response = await requestStream(url, cachedToken, options);

  if (response.status === 401) {
    const nextToken = await refreshAccessToken().catch(() => null);
    if (nextToken) {
      response = await requestStream(url, nextToken, options);
    }
  }

  if (response.status === 401) {
    handleTokenError();
    redirectToLogin();
    throw new ApiStreamError("Unauthorized", 401);
  }

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: unknown;
      code?: string;
      [key: string]: unknown;
    } | null;
    const detail =
      typeof payload?.detail === "string"
        ? payload.detail
        : `Request failed with status ${response.status}`;
    throw new ApiStreamError(detail, response.status, payload?.code, payload);
  }

  return response;
};

/**
 * POST that keeps the response body unread so callers can stream it. Unlike
 * `EventSource`, this supports the Bearer token the API expects.
 */
export const postStream = async (
  url: string,
  data: unknown,
  options: { signal?: AbortSignal } = {},
): Promise<Response> => {
  return performStreamRequest(url, {
    method: "POST",
    body: data,
    signal: options.signal,
  });
};

/**
 * GET that keeps the response body unread for SSE replay. Shares the Bearer
 * injection and single 401 refresh-and-retry with `postStream`.
 */
export const getStream = async (
  url: string,
  options: { signal?: AbortSignal; headers?: Record<string, string> } = {},
): Promise<Response> => {
  return performStreamRequest(url, {
    method: "GET",
    signal: options.signal,
    headers: options.headers,
  });
};

export const api = createApi();
