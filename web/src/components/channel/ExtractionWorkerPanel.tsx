/**
 * ExtractionWorkerPanel
 *
 * Replaces the legacy "Syncing channel · X of Y batches · Initializing..."
 * widget when DECOUPLE_EXTRACTION=true is in effect. Detects decoupled mode
 * by checking that extraction-status shows pending+extracting > 0 while
 * the sync job itself has zero batch_results (sync returned immediately after
 * upserting messages — no inline batches were processed).
 *
 * Shows four live counts, a stacked progress bar, claim rate, circuit-breaker
 * state and a wiki-activity summary sourced from the admin metrics endpoints.
 */

import { Loader2, Zap, CheckCircle2, XCircle, Clock, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ExtractionStatusResponse } from "@/hooks/useExtractionStatus";
import type {
  ExtractionWorkerMetrics,
  WikiMaintainerMetrics,
} from "@/hooks/useExtractionWorkerMetrics";
import { useExtractionWorkerMetrics } from "@/hooks/useExtractionWorkerMetrics";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ExtractionWorkerPanelProps {
  /** Channel to monitor. Must be provided; the panel renders null if absent. */
  channelId: string;
  /** Latest extraction-status counts from the polling hook. */
  extractionStatus: ExtractionStatusResponse | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtRate(rate: number | null | undefined): string {
  if (rate == null || !Number.isFinite(rate)) return "—";
  if (rate >= 10) return `${rate.toFixed(0)}/min`;
  if (rate >= 1) return `${rate.toFixed(1)}/min`;
  return `${(rate * 60).toFixed(1)}/min`;
}

function totalWikiRewrites(
  byKind: Record<string, number> | null | undefined,
): number {
  if (!byKind) return 0;
  return Object.values(byKind).reduce((a, b) => a + b, 0);
}

function totalPendingDirty(
  perChannel: Record<string, number> | null | undefined,
): number {
  if (!perChannel) return 0;
  return Object.values(perChannel).reduce((a, b) => a + b, 0);
}

// ---------------------------------------------------------------------------
// BreakerBadge
// ---------------------------------------------------------------------------

interface BreakerBadgeProps {
  state: string | null | undefined;
}

function BreakerBadge({ state }: BreakerBadgeProps) {
  if (!state) return null;

  const lower = state.toLowerCase();
  const isClosed = lower === "closed";
  const isHalf = lower === "half_open";
  const isOpen = lower === "open";

  return (
    <span
      data-testid="breaker-badge"
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium border",
        isClosed
          ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/20"
          : isHalf
            ? "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20"
            : isOpen
              ? "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20"
              : "bg-muted text-muted-foreground border-border",
      )}
    >
      <span
        className={cn(
          "w-1.5 h-1.5 rounded-full",
          isClosed
            ? "bg-emerald-500"
            : isHalf
              ? "bg-amber-500 animate-pulse"
              : isOpen
                ? "bg-red-500 animate-pulse"
                : "bg-muted-foreground/40",
        )}
      />
      {isClosed ? "healthy" : isHalf ? "recovering" : isOpen ? "open" : state}
    </span>
  );
}

// ---------------------------------------------------------------------------
// StackedBar
// ---------------------------------------------------------------------------

interface StackedBarProps {
  done: number;
  extracting: number;
  pending: number;
  failed: number;
  total: number;
}

function StackedBar({ done, extracting, pending, failed, total }: StackedBarProps) {
  if (total === 0) return null;

  const donePct = (done / total) * 100;
  const extractingPct = (extracting / total) * 100;
  const failedPct = (failed / total) * 100;

  return (
    <div
      data-testid="stacked-bar"
      className="h-2 w-full rounded-full bg-muted overflow-hidden flex"
      role="progressbar"
      aria-valuenow={done}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-label={`${done} of ${total} messages extracted`}
    >
      <div
        className="h-full bg-emerald-500 transition-all duration-700 ease-out"
        style={{ width: `${donePct}%` }}
      />
      <div
        className="h-full bg-primary animate-pulse transition-all duration-700 ease-out"
        style={{ width: `${extractingPct}%` }}
      />
      <div
        className="h-full bg-red-500/70 transition-all duration-700 ease-out"
        style={{ width: `${failedPct}%` }}
      />
      {/* pending fills the rest implicitly through bg-muted */}
      {pending > 0 && (
        <div
          className="h-full bg-muted-foreground/10"
          style={{ width: `${(pending / total) * 100}%` }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// CountChip
// ---------------------------------------------------------------------------

interface CountChipProps {
  label: string;
  value: number;
  variant: "done" | "active" | "pending" | "failed";
  testId?: string;
}

function CountChip({ label, value, variant, testId }: CountChipProps) {
  const cls = {
    done: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/20",
    active: "bg-primary/10 text-primary border-primary/20",
    pending: "bg-muted text-muted-foreground border-border",
    failed: "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20",
  }[variant];

  return (
    <span
      data-testid={testId}
      className={cn(
        "inline-flex flex-col items-center rounded-lg border px-2.5 py-1.5 min-w-[52px]",
        cls,
      )}
    >
      <span className="text-base font-semibold leading-none">{value}</span>
      <span className="text-[10px] mt-0.5 font-normal opacity-80">{label}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Inner panel (accepts pre-fetched metrics for testability)
// ---------------------------------------------------------------------------

interface InnerPanelProps {
  extractionStatus: ExtractionStatusResponse;
  workerMetrics: ExtractionWorkerMetrics | null;
  wikiMetrics: WikiMaintainerMetrics | null;
}

export function ExtractionWorkerPanelInner({
  extractionStatus,
  workerMetrics,
  wikiMetrics,
}: InnerPanelProps) {
  const { counts, total } = extractionStatus;
  const { pending, extracting, done, failed } = counts;

  const claimRate = workerMetrics?.claim_rate_5min ?? null;
  const breakerState = workerMetrics?.breaker_state ?? null;

  const wikiRewrites = totalWikiRewrites(wikiMetrics?.rewrite_count_by_page_kind);
  const wikiPendingDirty = totalPendingDirty(wikiMetrics?.pending_dirty_pages_per_channel);
  const applyUpdates = wikiMetrics?.apply_update_count_5min ?? 0;
  const wikiActive = wikiRewrites > 0 || applyUpdates > 0;

  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <div
      data-testid="extraction-worker-panel"
      className="rounded-xl border border-white/10 bg-card/70 backdrop-blur px-4 py-3 space-y-3"
    >
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Loader2 size={13} className="animate-spin text-primary shrink-0" />
          <span className="text-sm font-medium text-foreground">
            Extraction worker
          </span>
        </div>
        <BreakerBadge state={breakerState} />
      </div>

      {/* Count chips */}
      <div
        data-testid="count-chips"
        className="flex items-start gap-2 flex-wrap"
      >
        <CountChip
          label="done"
          value={done}
          variant="done"
          testId="chip-done"
        />
        <CountChip
          label="in progress"
          value={extracting}
          variant="active"
          testId="chip-extracting"
        />
        <CountChip
          label="pending"
          value={pending}
          variant="pending"
          testId="chip-pending"
        />
        <CountChip
          label="failed"
          value={failed}
          variant="failed"
          testId="chip-failed"
        />
      </div>

      {/* Progress bar */}
      <div className="space-y-1">
        <StackedBar
          done={done}
          extracting={extracting}
          pending={pending}
          failed={failed}
          total={total}
        />
        <div className="flex items-center justify-between text-[10px] text-muted-foreground">
          <span>
            {done}/{total} ({pct}%)
          </span>
          {claimRate != null && (
            <span className="flex items-center gap-1">
              <Zap size={9} />
              {fmtRate(claimRate)} throughput
            </span>
          )}
        </div>
      </div>

      {/* Worker throughput row */}
      {workerMetrics && (
        <div className="flex items-center gap-4 text-[11px] text-muted-foreground">
          <span className="flex items-center gap-1">
            <Clock size={10} />
            5m avg:{" "}
            <span className="text-foreground font-medium">
              {fmtRate(workerMetrics.claim_rate_5min)}
            </span>
          </span>
          <span className="flex items-center gap-1">
            15m avg:{" "}
            <span className="text-foreground font-medium">
              {fmtRate(workerMetrics.claim_rate_15min)}
            </span>
          </span>
          {workerMetrics.success_rate_5min < 1 && (
            <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
              <AlertTriangle size={10} />
              {Math.round(workerMetrics.success_rate_5min * 100)}% success
            </span>
          )}
        </div>
      )}

      {/* Wiki activity row — only shown when non-zero */}
      {wikiActive && (
        <div className="rounded-md border border-violet-500/20 bg-violet-500/5 px-3 py-1.5 flex items-center gap-2">
          <CheckCircle2 size={11} className="text-violet-500 shrink-0" />
          <span className="text-[11px] text-violet-800 dark:text-violet-200">
            <span className="font-semibold">{applyUpdates}</span> facts
            integrated into wiki
            {wikiRewrites > 0 && (
              <>
                {" · "}
                <span className="font-semibold">{wikiRewrites}</span> pages
                refreshed
              </>
            )}
            {wikiPendingDirty > 0 && (
              <span className="ml-1.5 text-violet-600/70 dark:text-violet-400/70">
                ({wikiPendingDirty} pending)
              </span>
            )}
          </span>
        </div>
      )}

      {/* Recent failures (condensed) */}
      {workerMetrics &&
        workerMetrics.recent_failures.length > 0 && (
          <div className="space-y-0.5">
            {workerMetrics.recent_failures.slice(0, 3).map((f, i) => (
              <div
                key={i}
                className="text-[10px] text-red-600 dark:text-red-400 truncate flex items-center gap-1"
              >
                <XCircle size={9} className="shrink-0" />
                {f.error_class}
                {f.channel_id && (
                  <span className="text-muted-foreground/60 font-mono">
                    #{f.channel_id.slice(-6)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Public component (owns the metrics-fetching lifecycle)
// ---------------------------------------------------------------------------

export function ExtractionWorkerPanel({
  channelId,
  extractionStatus,
}: ExtractionWorkerPanelProps) {
  const isActive =
    extractionStatus !== null &&
    ((extractionStatus.counts.pending ?? 0) > 0 ||
      (extractionStatus.counts.extracting ?? 0) > 0);

  const { workerMetrics, wikiMetrics } = useExtractionWorkerMetrics({
    isActive,
    pollMsActive: 4000,
    pollMsIdle: 0,
  });

  if (!channelId || !extractionStatus) return null;

  return (
    <ExtractionWorkerPanelInner
      extractionStatus={extractionStatus}
      workerMetrics={workerMetrics}
      wikiMetrics={wikiMetrics}
    />
  );
}
