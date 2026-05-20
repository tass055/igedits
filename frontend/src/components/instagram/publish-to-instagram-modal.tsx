"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Loader2, Instagram, CheckCircle2, Sparkles } from "lucide-react";

interface PublishToInstagramModalProps {
  clipId: string;
  method?: "make" | "direct" | null;
}

export function PublishToInstagramModal({ clipId, method }: PublishToInstagramModalProps) {
  const [open, setOpen] = useState(false);
  const [caption, setCaption] = useState("");
  const [sent, setSent] = useState(false);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleOpen = (isOpen: boolean) => {
    setOpen(isOpen);
    if (!isOpen) {
      setSent(false);
      setError(null);
    }
  };

  const handleGenerateCaption = async () => {
    setGenerating(true);
    setError(null);
    try {
      const res = await fetch(`/api/instagram/suggest-caption?clip_id=${clipId}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to generate caption");
      setCaption(data.caption);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to generate caption");
    } finally {
      setGenerating(false);
    }
  };

  const handlePublish = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/instagram/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip_id: clipId, caption }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to publish");
      setSent(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to publish to Instagram");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="gap-2">
          <Instagram className="h-4 w-4" />
          Publish to Instagram
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Publish to Instagram Reels</DialogTitle>
          <DialogDescription>
            {method === "direct"
              ? "Posts the clip directly to your connected Instagram account."
              : "Sends the clip to your Make.com scenario for posting."}
          </DialogDescription>
        </DialogHeader>

        {sent ? (
          <div className="space-y-4">
            <Alert className="border-green-200 bg-green-50">
              <CheckCircle2 className="h-4 w-4 text-green-600" />
              <AlertDescription className="text-green-800">
                {method === "direct"
                  ? "Posted! Your Reel should appear on Instagram shortly."
                  : "Sent! Your clip is being posted — check Instagram in about 30 seconds."}
              </AlertDescription>
            </Alert>
            <Button
              onClick={() => handleOpen(false)}
              variant="outline"
              className="w-full"
            >
              Done
            </Button>
          </div>
        ) : (
          <div className="space-y-4">
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm font-medium">Caption</label>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs gap-1 text-muted-foreground hover:text-foreground"
                  onClick={handleGenerateCaption}
                  disabled={generating}
                >
                  {generating ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Sparkles className="h-3 w-3" />
                  )}
                  {generating ? "Generating…" : "Generate with AI"}
                </Button>
              </div>
              <Textarea
                placeholder="Add a caption… or click Generate with AI"
                value={caption}
                onChange={(e) => setCaption(e.target.value)}
                className="resize-none"
                rows={4}
                maxLength={2200}
              />
              <p className="text-xs text-muted-foreground mt-1">
                {caption.length}/2200 characters
              </p>
            </div>

            {error && (
              <Alert className="border-red-200 bg-red-50">
                <AlertDescription className="text-red-800">{error}</AlertDescription>
              </Alert>
            )}

            <Button
              onClick={handlePublish}
              disabled={loading}
              className="w-full gap-2"
            >
              {loading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Sending…
                </>
              ) : (
                <>
                  <Instagram className="h-4 w-4" />
                  Post to Instagram
                </>
              )}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
