"use client";

import Link from "next/link";
import { ArrowLeft, Instagram, CheckCircle2, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useSession } from "@/lib/auth-client";
import { Skeleton } from "@/components/ui/skeleton";

interface CredentialsStatus {
  connected: boolean;
  username?: string;
  auth_method?: "password" | "session_id";
}

export default function IntegrationsPage() {
  const { data: session, isPending } = useSession();

  const [credStatus, setCredStatus] = useState<CredentialsStatus | null>(null);
  const [credLoading, setCredLoading] = useState(true);

  const [connectMethod, setConnectMethod] = useState<"password" | "session_id">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [saving, setSaving] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (!session?.user) return;
    fetch("/api/instagram/credentials")
      .then((r) => r.json())
      .then((data) => setCredStatus(data))
      .catch(() => setCredStatus({ connected: false }))
      .finally(() => setCredLoading(false));
  }, [session]);

  async function handleConnect(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSuccess(false);
    setSaving(true);
    try {
      const endpoint =
        connectMethod === "session_id"
          ? "/api/instagram/credentials/session-id"
          : "/api/instagram/credentials";
      const body =
        connectMethod === "session_id"
          ? { username, session_id: sessionId }
          : { username, password };

      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || data.detail || "Failed to save credentials");
      } else {
        setCredStatus({ connected: true, username: data.username, auth_method: data.auth_method });
        setSuccess(true);
        setUsername("");
        setPassword("");
        setSessionId("");
      }
    } catch {
      setError("Network error — please try again");
    } finally {
      setSaving(false);
    }
  }

  async function handleDisconnect() {
    setDisconnecting(true);
    setError(null);
    try {
      await fetch("/api/instagram/credentials", { method: "DELETE" });
      setCredStatus({ connected: false });
      setSuccess(false);
    } catch {
      setError("Failed to disconnect — please try again");
    } finally {
      setDisconnecting(false);
    }
  }

  if (isPending) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Skeleton className="h-4 w-40" />
      </div>
    );
  }
  if (!session?.user) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Link href="/sign-in" className="underline">Sign in required</Link>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <div className="border-b">
        <div className="max-w-4xl mx-auto px-4 py-4 flex justify-between items-center">
          <Link href="/settings">
            <Button variant="ghost" size="sm">
              <ArrowLeft className="w-4 h-4" /> Back
            </Button>
          </Link>
          <h1 className="text-base font-semibold">Integrations</h1>
          <div className="w-10" />
        </div>
      </div>

      <div className="max-w-2xl mx-auto px-4 py-10 space-y-10">

        {/* ---- Direct Instagram connection ---- */}
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Instagram className="w-5 h-5" />
            <h2 className="text-lg font-semibold">Connect Instagram</h2>
          </div>
          <p className="text-sm text-muted-foreground mb-4">
            Enter your Instagram username and password to post Reels directly — no Make.com or
            Meta developer app required.
          </p>

          {credLoading ? (
            <Skeleton className="h-10 w-48" />
          ) : credStatus?.connected ? (
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2 text-sm font-medium">
                <CheckCircle2 className="w-4 h-4 text-green-500" />
                Connected as <span className="font-semibold">@{credStatus.username}</span>
                <span className="text-xs text-muted-foreground font-normal">
                  ({credStatus.auth_method === "session_id" ? "session ID" : "password"})
                </span>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={handleDisconnect}
                disabled={disconnecting}
              >
                {disconnecting ? <Loader2 className="w-3 h-3 animate-spin mr-1" /> : null}
                Disconnect
              </Button>
            </div>
          ) : (
            <div className="max-w-sm space-y-4">
              {/* Method toggle */}
              <div className="flex gap-1 p-1 bg-muted rounded-md w-fit">
                <button
                  type="button"
                  onClick={() => { setConnectMethod("password"); setError(null); }}
                  className={`px-3 py-1 text-sm rounded-sm transition-colors ${
                    connectMethod === "password"
                      ? "bg-background shadow-sm font-medium"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Username & Password
                </button>
                <button
                  type="button"
                  onClick={() => { setConnectMethod("session_id"); setError(null); }}
                  className={`px-3 py-1 text-sm rounded-sm transition-colors ${
                    connectMethod === "session_id"
                      ? "bg-background shadow-sm font-medium"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Session ID
                </button>
              </div>

              <form onSubmit={handleConnect} className="space-y-3">
                <div className="space-y-1">
                  <Label htmlFor="ig-username">Instagram username</Label>
                  <Input
                    id="ig-username"
                    placeholder="yourusername"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    autoComplete="username"
                    required
                  />
                </div>

                {connectMethod === "password" ? (
                  <div className="space-y-1">
                    <Label htmlFor="ig-password">Password</Label>
                    <Input
                      id="ig-password"
                      type="password"
                      placeholder="••••••••"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      autoComplete="current-password"
                      required
                    />
                  </div>
                ) : (
                  <div className="space-y-1">
                    <Label htmlFor="ig-session-id">Session ID</Label>
                    <Input
                      id="ig-session-id"
                      type="password"
                      placeholder="Paste your sessionid cookie value"
                      value={sessionId}
                      onChange={(e) => setSessionId(e.target.value)}
                      required
                    />
                    <p className="text-xs text-muted-foreground">
                      Open instagram.com (while logged in) → F12 → Application → Cookies →
                      https://www.instagram.com → find <code className="bg-muted px-1 rounded">sessionid</code> → copy its value.
                      Bypasses IP-based login blocks.
                    </p>
                  </div>
                )}

                {error && <p className="text-sm text-destructive">{error}</p>}
                {success && (
                  <p className="text-sm text-green-600 flex items-center gap-1">
                    <CheckCircle2 className="w-4 h-4" /> Connected successfully
                  </p>
                )}
                <Button type="submit" disabled={saving}>
                  {saving ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : null}
                  Connect
                </Button>
                <p className="text-xs text-muted-foreground pt-1">
                  Credentials are encrypted and stored on your server. This uses an unofficial
                  Instagram API — not affiliated with Meta. Accounts with two-factor authentication
                  are not supported.
                </p>
              </form>
            </div>
          )}
        </div>

        {/* ---- Make.com (advanced / alternative) ---- */}
        <div>
          <h2 className="text-base font-semibold mb-1">Make.com (advanced alternative)</h2>
          <p className="text-sm text-muted-foreground mb-4">
            If you prefer not to store credentials, you can use{" "}
            <a href="https://make.com" target="_blank" rel="noopener noreferrer" className="underline">
              Make.com
            </a>{" "}
            as a webhook relay. Set{" "}
            <code className="text-xs bg-muted px-1 py-0.5 rounded">MAKE_INSTAGRAM_WEBHOOK_URL</code>{" "}
            on your server — it takes priority over the direct connection above.
          </p>
          <Alert>
            <AlertDescription className="text-sm space-y-1">
              <p className="font-medium">Quick setup</p>
              <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                <li>New Scenario → Webhooks → <strong>Custom webhook</strong> → copy the webhook URL.</li>
                <li>Add module → <strong>Instagram for Business → Create a Reel</strong>.</li>
                <li>In the <strong>Connection</strong> field click <strong>Add</strong> → log in with the Facebook account linked to your Instagram.</li>
                <li>Map <code className="text-xs bg-muted px-1 rounded">{"{{1.video_url}}"}</code> and <code className="text-xs bg-muted px-1 rounded">{"{{1.caption}}"}</code> → Save → Activate.</li>
                <li>Paste the webhook URL into <code className="text-xs bg-muted px-1 rounded">MAKE_INSTAGRAM_WEBHOOK_URL</code> and restart the backend.</li>
              </ol>
            </AlertDescription>
          </Alert>
        </div>

      </div>
    </div>
  );
}
