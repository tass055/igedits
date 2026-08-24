import { headers } from "next/headers";
import { NextResponse } from "next/server";

import { auth } from "@/lib/auth";
import { buildBackendAuthHeaders } from "@/lib/backend-auth";

// Stream large video uploads straight through to the backend. Using
// request.formData() here buffered the entire file in the Node process and
// truncated large uploads (a cut-off .mov loses its trailing moov atom and
// becomes unreadable), so we forward the raw request body as a stream instead.
export const runtime = "nodejs";

export async function POST(request: Request) {
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const apiUrl =
    process.env.BACKEND_INTERNAL_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "http://localhost:8000";
  const normalizedApiUrl = apiUrl.replace(/\/$/, "");

  // Preserve the original multipart Content-Type (with its boundary) and length
  // so the backend can parse the forwarded body byte-for-byte.
  const forwardHeaders: Record<string, string> = {
    ...buildBackendAuthHeaders(session.user.id),
  };
  const contentType = request.headers.get("content-type");
  if (contentType) forwardHeaders["content-type"] = contentType;
  const contentLength = request.headers.get("content-length");
  if (contentLength) forwardHeaders["content-length"] = contentLength;

  const upstream = await fetch(`${normalizedApiUrl}/upload`, {
    method: "POST",
    headers: forwardHeaders,
    body: request.body,
    // `duplex: "half"` is required to send a streamed request body via fetch.
    duplex: "half",
  } as RequestInit & { duplex: "half" });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") || "application/json",
      ...(upstream.headers.get("x-trace-id")
        ? { "x-trace-id": upstream.headers.get("x-trace-id") as string }
        : {}),
    },
  });
}
