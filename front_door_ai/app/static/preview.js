'use strict';

// Ingress may remove the multipart boundary. Fall back to cached JPEG frames
// without opening another connection to the ESP32 camera.
class FramePreview {
  constructor(image, {streamUrl, snapshotUrl, onFrame = () => {}, onStop = () => {},
    firstFrameMs = 2000, intervalMs = 200, retryMs = 1000, requestTimeoutMs = 5000,
    env = globalThis}) {
    Object.assign(this, {image, streamUrl, snapshotUrl, onFrame, onStop,
      firstFrameMs, intervalMs, retryMs, requestTimeoutMs, env});
    this.active = false;
    this.generation = 0;
    this.timer = null;
    this.request = null;
    this.currentUrl = null;
    this.pendingUrl = null;
  }

  start() {
    if (this.active) return;
    this.active = true;
    this.image.dataset.streaming = 'true';
    this.image.dataset.previewMode = 'stream';
    const generation = ++this.generation;
    this.image.onload = () => {
      if (!this.active || generation !== this.generation) return;
      this.clearTimer();
      this.showFrame();
    };
    this.image.onerror = () => {
      if (this.active && generation === this.generation) this.useSnapshots();
    };
    this.timer = this.env.setTimeout(() => this.useSnapshots(), this.firstFrameMs);
    this.image.src = this.streamUrl;
  }

  clearTimer() {
    if (this.timer !== null) this.env.clearTimeout(this.timer);
    this.timer = null;
  }

  showFrame() {
    this.image.style.display = 'block';
    this.onFrame();
  }

  revoke(field) {
    if (this[field]) this.env.URL.revokeObjectURL(this[field]);
    this[field] = null;
  }

  useSnapshots() {
    if (!this.active) return;
    ++this.generation;
    this.clearTimer();
    this.image.onload = this.image.onerror = null;
    this.image.removeAttribute('src');
    this.image.dataset.previewMode = 'snapshot';
    this.snapshot();
  }

  async snapshot() {
    if (!this.active) return;
    const generation = ++this.generation;
    const request = new this.env.AbortController();
    this.request = request;
    const isCurrent = () => this.active && generation === this.generation;
    const finish = success => {
      if (!isCurrent()) return;
      // Invalidate late fetch completions and queued image callbacks.
      ++this.generation;
      this.clearTimer();
      request.abort();
      this.request = null;
      this.image.onload = this.image.onerror = null;
      if (success) {
        this.revoke('currentUrl');
        this.currentUrl = this.pendingUrl;
        this.pendingUrl = null;
        this.showFrame();
      } else {
        if (this.pendingUrl) {
          if (this.currentUrl) this.image.src = this.currentUrl;
          else this.image.removeAttribute('src');
          this.revoke('pendingUrl');
        }
      }
      this.timer = this.env.setTimeout(() => this.snapshot(), success ? this.intervalMs : this.retryMs);
    };
    this.timer = this.env.setTimeout(() => finish(false), this.requestTimeoutMs);
    try {
      const response = await this.env.fetch(this.snapshotUrl, {cache: 'no-store', signal: request.signal});
      if (!isCurrent()) return;
      if (!response.ok) throw new Error('Snapshot unavailable');
      const blob = await response.blob();
      if (!isCurrent()) return;
      this.pendingUrl = this.env.URL.createObjectURL(blob);
      this.image.onload = () => finish(true);
      this.image.onerror = () => finish(false);
      this.image.src = this.pendingUrl;
    } catch {
      finish(false);
    }
  }

  stop() {
    this.active = false;
    ++this.generation;
    this.clearTimer();
    if (this.request) this.request.abort();
    this.request = null;
    this.image.onload = this.image.onerror = null;
    this.image.removeAttribute('src');
    this.revoke('pendingUrl');
    this.revoke('currentUrl');
    this.image.dataset.streaming = 'false';
    delete this.image.dataset.previewMode;
    this.image.style.display = 'none';
    this.onStop();
  }
}

globalThis.FramePreview = FramePreview;
