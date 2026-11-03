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

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiStreamError";
    this.status = status;
    this.code = code;
  }
}

const requestStream = (
  url: string,
  data: unknown,
  token: string | null,
  signal?: AbortSignal,
) =>
  fetch(`${process.env.NEXT_PUBLIC_API_URL}${url}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      Authorization: token ? `Bearer ${token}` : "",
    },
    body: JSON.stringify(data),
    signal,
  });

/**
 * POST that keeps the response body unread so callers can stream it. Unlike
 * `EventSource`, this supports the Bearer token the API expects.
 */
export const postStream = async (
  url: string,
  data: unknown,
  options: { signal?: AbortSignal } = {},
): Promise<Response> => {
  let response = await requestStream(url, data, cachedToken, options.signal);

  if (response.status === 401) {
    const nextToken = await refreshAccessToken().catch(() => null);
    if (nextToken) {
      response = await requestStream(url, data, nextToken, options.signal);
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
    } | null;
    const detail =
      typeof payload?.detail === "string"
        ? payload.detail
        : `Request failed with status ${response.status}`;
    throw new ApiStreamError(detail, response.status, payload?.code);
  }

  return response;
};

export const api = createApi();
