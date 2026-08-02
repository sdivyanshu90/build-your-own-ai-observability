/**
 * Span buffering and export.
 *
 * A timer-driven batcher over `fetch`. Failure behaviour, in priority order:
 *
 * 1. The application never awaits telemetry: `submit` is synchronous and does
 *    no I/O.
 * 2. A full queue drops the OLDEST spans. During an incident the recent ones
 *    are what someone is reading.
 * 3. Retries are bounded and jittered, so a platform blip does not become a
 *    retry storm from every instrumented process.
 * 4. `shutdown()` flushes with a deadline -- delivering the last batch matters,
 *    hanging the process does not.
 */

import type {
  IngestBatch,
  IngestResponse,
  ResourceDescriptor,
  WireSpan,
} from "@aiobs/schemas";

import { canExport, ingestUrl, type Config } from "./config.js";

export interface ExportResult {
  accepted: number;
  rejected: number;
  duplicates: number;
  retryable: boolean;
  error?: string;
}

export interface Transport {
  send(batch: IngestBatch): Promise<ExportResult>;
  close?(): Promise<void>;
}

export class HttpTransport implements Transport {
  constructor(private readonly config: Config) {}

  async send(batch: IngestBatch): Promise<ExportResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.timeoutMs);
    try {
      const response = await fetch(ingestUrl(this.config), {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": this.config.apiKey ?? "",
          "user-agent": "aiobs-typescript/0.1.0",
        },
        body: JSON.stringify(batch),
        signal: controller.signal,
      });

      if (response.status === 200 || response.status === 202) {
        const data = (await response.json()) as IngestResponse;
        return {
          accepted: data.accepted ?? 0,
          rejected: data.rejected ?? 0,
          duplicates: data.duplicates ?? 0,
          retryable: false,
        };
      }

      // 4xx other than 429 means the payload is wrong; retrying cannot fix it.
      const retryable = response.status === 429 || response.status >= 500;
      const text = (await response.text()).slice(0, 400);
      return {
        accepted: 0,
        rejected: 0,
        duplicates: 0,
        retryable,
        error: `http ${response.status}: ${text}`,
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return {
        accepted: 0,
        rejected: 0,
        duplicates: 0,
        retryable: true,
        error: message,
      };
    } finally {
      clearTimeout(timer);
    }
  }
}

/** Captures batches in memory. The SDK's primary test utility. */
export class MemoryTransport implements Transport {
  readonly batches: IngestBatch[] = [];
  readonly spans: WireSpan[] = [];
  attempts = 0;
  private failuresRemaining: number;

  constructor(options: { failTimes?: number; retryable?: boolean } = {}) {
    this.failuresRemaining = options.failTimes ?? 0;
    this.retryable = options.retryable ?? true;
  }

  private readonly retryable: boolean;

  async send(batch: IngestBatch): Promise<ExportResult> {
    this.attempts += 1;
    if (this.failuresRemaining > 0) {
      this.failuresRemaining -= 1;
      return {
        accepted: 0,
        rejected: 0,
        duplicates: 0,
        retryable: this.retryable,
        error: "simulated transport failure",
      };
    }
    this.batches.push(batch);
    this.spans.push(...batch.spans);
    return {
      accepted: batch.spans.length,
      rejected: 0,
      duplicates: 0,
      retryable: false,
    };
  }

  clear(): void {
    this.batches.length = 0;
    this.spans.length = 0;
    this.attempts = 0;
  }
}

export interface ExporterStats {
  submitted: number;
  dropped: number;
  exported: number;
  failed: number;
  queued: number;
  lastError: string | null;
}

export class BatchExporter {
  private readonly queue: WireSpan[] = [];
  private timer: ReturnType<typeof setInterval> | null = null;
  private flushing: Promise<void> | null = null;
  private stopped = false;

  submitted = 0;
  dropped = 0;
  exported = 0;
  failed = 0;
  lastError: string | null = null;

  constructor(
    private readonly config: Config,
    private readonly transport: Transport,
    private readonly resource: ResourceDescriptor,
  ) {}

  start(): void {
    if (this.timer || !this.config.enabled) return;
    this.timer = setInterval(() => {
      void this.flush();
    }, this.config.flushIntervalMs);
    // Do not keep the event loop alive: a script whose work is done should
    // exit, and the shutdown hook flushes what remains.
    this.timer.unref?.();
  }

  submit(span: WireSpan): boolean {
    if (this.stopped) return false;
    this.submitted += 1;
    if (this.queue.length >= this.config.maxQueueSize) {
      this.queue.shift();
      this.dropped += 1;
    }
    this.queue.push(span);
    if (this.queue.length >= this.config.maxBatchSize) {
      void this.flush();
    }
    return true;
  }

  /** Export everything buffered. Safe to call concurrently. */
  async flush(): Promise<void> {
    if (this.flushing) {
      await this.flushing;
      return;
    }
    this.flushing = this.drain().finally(() => {
      this.flushing = null;
    });
    await this.flushing;
  }

  private async drain(): Promise<void> {
    while (this.queue.length > 0) {
      const batch = this.queue.splice(0, this.config.maxBatchSize);
      await this.exportWithRetry(batch);
    }
  }

  private async exportWithRetry(spans: WireSpan[]): Promise<void> {
    if (!canExport(this.config)) return;
    const batch: IngestBatch = {
      resource: this.resource,
      spans,
      sampling_rate: this.config.sampleRate,
    };

    let attempt = 0;
    let result: ExportResult = {
      accepted: 0,
      rejected: 0,
      duplicates: 0,
      retryable: true,
    };
    while (attempt <= this.config.maxRetries) {
      result = await this.transport.send(batch);
      if (!result.error) {
        this.exported += result.accepted;
        if (result.rejected > 0 && this.config.debug) {
          console.warn(
            `aiobs: ${result.rejected} spans rejected by the platform`,
          );
        }
        return;
      }
      if (!result.retryable) break;
      attempt += 1;
      if (attempt > this.config.maxRetries) break;
      const ceiling = Math.min(
        this.config.retryBaseDelayMs * 2 ** (attempt - 1),
        this.config.retryMaxDelayMs,
      );
      // Full jitter: a narrow band would reproduce the thundering herd that
      // caused the outage being retried against.
      await new Promise((resolve) =>
        setTimeout(resolve, Math.random() * ceiling),
      );
    }

    this.failed += spans.length;
    this.lastError = result.error ?? "unknown export failure";
    console.warn(`aiobs: dropping ${spans.length} spans: ${this.lastError}`);
  }

  async shutdown(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    await Promise.race([
      this.flush(),
      new Promise((resolve) =>
        setTimeout(resolve, this.config.shutdownTimeoutMs),
      ),
    ]);
    await this.transport.close?.();
  }

  stats(): ExporterStats {
    return {
      submitted: this.submitted,
      dropped: this.dropped,
      exported: this.exported,
      failed: this.failed,
      queued: this.queue.length,
      lastError: this.lastError,
    };
  }
}
