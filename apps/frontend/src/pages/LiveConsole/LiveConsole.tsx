import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import "./LiveConsole.css";

declare global {
  interface Window {
    __SEP_CONFIG__?: Record<string, unknown>;
  }
}

type RuntimeConfig = {
  API_URL?: string;
  API_BEARER_TOKEN?: string;
};

const runtimeConfig: RuntimeConfig =
  (typeof window !== "undefined" ? (window.__SEP_CONFIG__ as RuntimeConfig | undefined) : undefined) || {};

const stringOrUndefined = (value: unknown): string | undefined =>
  typeof value === "string" && value.length > 0 ? value : undefined;

const API_BASE = stringOrUndefined(runtimeConfig.API_URL) ?? (import.meta.env.VITE_API_HOST || "");
const API_BEARER_TOKEN = stringOrUndefined(runtimeConfig.API_BEARER_TOKEN) ?? "";

const normalisedBase = API_BASE ? API_BASE.replace(/\/$/, "") : "";

const buildApiUrl = (path: string): string => {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  if (!normalisedBase) {
    return path;
  }
  return `${normalisedBase}${path}`;
};

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers ?? {});
  if (API_BEARER_TOKEN && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${API_BEARER_TOKEN}`);
  }
  const response = await fetch(buildApiUrl(path), { ...init, headers });
  if (!response.ok) {
    throw new Error(path.replace(/^\//, ""));
  }
  return (await response.json()) as T;
}

const GATE_STALE_THRESHOLD = 180;

type ServiceState = "loading" | "online" | "offline";

interface RawPricingEntry {
  instrument: string;
  bid: number | null;
  ask: number | null;
  mid: number | null;
}

interface PricingRow extends RawPricingEntry {
  change: number | null;
  spread: number | null;
}

interface TradingHealth {
  status?: string;
  timestamp?: string;
  kill_switch?: boolean;
  trading_active?: boolean;
  enabled_pairs?: string[];
  service?: string;
}

interface PositionEntry {
  instrument: string;
  net_units: number;
  exposure: number;
}

interface NavSummary {
  nav_snapshot?: number;
  total_units?: number;
  exposure_usd?: number;
  positions?: PositionEntry[];
  kill_switch?: boolean;
  trading_active?: boolean;
  timestamp?: string;
}

interface HazardDecileLift {
  decile: number;
  count: number;
  avg_return_pct: number;
  lift_vs_overall?: number;
}

interface SignalOutcomeStats {
  count: number;
  avg_return_pct: number;
  median_return_pct: number;
  positive_pct: number;
  avg_positive_return_pct?: number;
  avg_negative_return_pct?: number;
  avg_hazard?: number | null;
  avg_cost_pct?: number;
  hazard_buckets?: Record<string, { count: number; avg_return_pct: number }>;
  auroc_admit?: number;
  hazard_deciles?: HazardDecileLift[];
  overall_avg_return_pct?: number;
  t_stat?: number;
  p_value?: number;
  p_value_holm?: number;
  samples?: Array<{
    ts: string;
    return_pct: number;
    hazard: number | null;
    coherence: number | null;
    admit: boolean;
    reasons: string[];
    price_start: number;
    price_end: number;
    cost_pct: number;
  }>;
  median_spread?: number | null;
}

interface SignalOutcomeInstrumentNote {
  note: string;
}

type SignalOutcomeInstrument = Record<string, SignalOutcomeStats> | SignalOutcomeInstrumentNote;

interface SignalOutcomePayload {
  generated_at?: string;
  horizons?: number[];
  cost_model?: string;
  commission_pips?: number;
  slippage_multiplier?: number;
  instruments?: Record<string, SignalOutcomeInstrument>;
}

const isInstrumentNote = (value: SignalOutcomeInstrument | undefined): value is SignalOutcomeInstrumentNote => {
  return Boolean(value && (value as SignalOutcomeInstrumentNote).note !== undefined);
};

interface GateMetric {
  instrument: string;
  admit: boolean;
  direction?: string;
  st_peak?: boolean;
  ml_probability?: number | null;
  age_seconds: number | null;
  updated_at: string | null;
  hazard?: number | null;
  hazard_threshold?: number | null;
  repetitions?: number | null;
  reasons?: string[];
  reason_details?: string[];
  regime?: {
    label?: string;
    confidence?: number;
  };
  structure?: Record<string, unknown>;
  guards?: {
    coherence_tau_slope?: number | null;
    domain_wall_slope?: number | null;
    spectral_lowf_share?: number | null;
  };
  source?: string;
  action?: string;
}

interface HistoryPoint {
  time: string;
  close: number;
}

type MetricTone = "success" | "danger" | "muted" | "accent" | "warning";

const MAJOR_INSTRUMENTS = ["EUR_USD", "USD_JPY", "AUD_USD", "NZD_USD", "USD_CHF", "GBP_USD", "USD_CAD"] as const;

const HAZARD_GUARDS = [
  { id: "entropy", label: "Entropy", max: 3.0 },
  { id: "coherence", label: "Coherence", min: 0.0 },
  { id: "stability", label: "Stability", min: 0.0 },
] as const;

interface RocHorizonEntry {
  count?: number;
  avg_roc_pct?: number;
  positive_pct?: number;
}

interface RegimeRocSummary {
  generated_at?: string;
  horizons?: number[];
  regimes?: Record<string, Record<string, RocHorizonEntry>>;
}

interface RegimeMapping {
  instrument_strategies?: Record<string, string>;
}

export default function LiveConsole() {
  const [health, setHealth] = useState<TradingHealth | null>(null);
  const [serviceState, setServiceState] = useState<ServiceState>("loading");
  const [pricingRows, setPricingRows] = useState<PricingRow[]>([]);
  const [pricingError, setPricingError] = useState<string | null>(null);
  const [lastPricingUpdate, setLastPricingUpdate] = useState<number | null>(null);
  const [navSummary, setNavSummary] = useState<NavSummary | null>(null);
  const [gateMetrics, setGateMetrics] = useState<GateMetric[]>([]);
  const [regimeMapping, setRegimeMapping] = useState<RegimeMapping | null>(null);
  const [killSwitchUpdating, setKillSwitchUpdating] = useState(false);
  const [tradingActiveUpdating, setTradingActiveUpdating] = useState(false);
  const [priceHistory, setPriceHistory] = useState<Record<string, HistoryPoint[]>>({});

  const previousMidRef: MutableRefObject<Record<string, number | null>> = useRef({});
  const isMountedRef = useRef(true);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let mounted = true;

    const fetchHealth = async () => {
      try {
        const payload = await fetchJson<TradingHealth>("/health");
        if (!mounted) return;
        setHealth(payload);
        setServiceState("online");
      } catch (error) {
        if (!mounted) return;
        setServiceState((prev) => (prev === "loading" ? "loading" : "offline"));
      }
    };

    const fetchPricing = async () => {
      try {
        const payload = await fetchJson<{ prices: Record<string, RawPricingEntry> }>("/api/pricing");
        if (!mounted) return;

        const entries: RawPricingEntry[] = Object.entries(payload.prices || {}).map(
          ([instrument, details]: [string, any]) => ({
            instrument,
            bid: numberOrNull(details?.bid),
            ask: numberOrNull(details?.ask),
            mid: numberOrNull(details?.mid),
          })
        );

        const nextRows = entries.map((entry) => {
          const previousMid = previousMidRef.current[entry.instrument];
          const change = entry.mid != null && previousMid != null ? entry.mid - previousMid : null;
          const spread = entry.ask != null && entry.bid != null ? entry.ask - entry.bid : null;
          return { ...entry, change, spread } satisfies PricingRow;
        });

        previousMidRef.current = entries.reduce<Record<string, number | null>>((acc, entry) => {
          acc[entry.instrument] = entry.mid;
          return acc;
        }, {});

        setPricingRows(nextRows);
        setPricingError(null);
        setLastPricingUpdate(Date.now());
      } catch (error) {
        if (!mounted) return;
        setPricingError("Unable to reach pricing endpoint");
        setPricingRows([]);
      }
    };

    const fetchNav = async () => {
      try {
        const payload = await fetchJson<{ nav?: NavSummary }>("/api/metrics/nav");
        if (!mounted) return;
        setNavSummary(payload.nav || null);
      } catch (error) {
        if (!mounted) return;
        setNavSummary((prev) => prev);
      }
    };

    const fetchGates = async () => {
      try {
        const payload = await fetchJson<{ gates: GateMetric[] }>("/api/metrics/gates");
        if (!mounted) return;
        setGateMetrics(Array.isArray(payload.gates) ? payload.gates : []);
      } catch (error) {
        if (!mounted) return;
        setGateMetrics((prev) => prev);
      }
    };

    const fetchRegimeMapping = async () => {
      try {
        const payload = await fetchJson<RegimeMapping>("/api/regime-map");
        if (!mounted) return;
        setRegimeMapping(payload);
      } catch (error) {
        if (!mounted) return;
        setRegimeMapping((prev) => prev);
      }
    };

    fetchHealth();
    fetchPricing();
    fetchNav();
    fetchGates();
    fetchRegimeMapping();

    const healthInterval = window.setInterval(fetchHealth, 30_000);
    const pricingInterval = window.setInterval(fetchPricing, 5_000);
    const navInterval = window.setInterval(fetchNav, 30_000);
    const gateInterval = window.setInterval(fetchGates, 15_000);
    const regimeMapInterval = window.setInterval(fetchRegimeMapping, 300_000);

    return () => {
      mounted = false;
      window.clearInterval(healthInterval);
      window.clearInterval(pricingInterval);
      window.clearInterval(navInterval);
      window.clearInterval(gateInterval);
      window.clearInterval(regimeMapInterval);
    };
  }, []);

  const sortedPricing = useMemo(() => {
    return [...pricingRows].sort((a, b) => a.instrument.localeCompare(b.instrument));
  }, [pricingRows]);

  const instrumentList = useMemo(() => sortedPricing.map((row) => row.instrument), [sortedPricing]);
  const instrumentsKey = useMemo(() => instrumentList.join(","), [instrumentList]);

  useEffect(() => {
    if (!instrumentList.length) return;
    let active = true;

    const fetchHistory = async () => {
      try {
        const next: Record<string, HistoryPoint[]> = {};
        const responses = await Promise.all(
          instrumentList.map(async (instrument) => {
            try {
              const payload = await fetchJson<{ points?: HistoryPoint[] }>(
                `/api/pricing/history?instrument=${encodeURIComponent(instrument)}&granularity=M5&count=96`
              );
              return { instrument, points: Array.isArray(payload.points) ? payload.points : [] };
            } catch {
              return { instrument, points: [] };
            }
          })
        );
        for (const entry of responses) {
          if (entry.instrument) {
            next[entry.instrument] = entry.points;
          }
        }
        if (!active) return;
        if (Object.keys(next).length > 0) {
          setPriceHistory((prev) => ({ ...prev, ...next }));
        }
      } catch (error) {
        if (!active) return;
      }
    };

    fetchHistory();
    const interval = window.setInterval(fetchHistory, 60_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [instrumentsKey, instrumentList]);

  const toggleKillSwitch = useCallback(
    async (nextState: boolean) => {
      try {
        setKillSwitchUpdating(true);
        await fetchJson<{ kill_switch: boolean }>("/api/kill-switch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kill_switch: nextState }),
        });
        setHealth((prev) => (prev ? { ...prev, kill_switch: nextState } : prev));
        setNavSummary((prev) => (prev ? { ...prev, kill_switch: nextState } : prev));
      } catch (error) {
        console.error("Failed to toggle kill switch", error);
      } finally {
        setKillSwitchUpdating(false);
      }
    },
    []
  );

  const toggleTradingActive = useCallback(
    async (nextState: boolean) => {
      try {
        setTradingActiveUpdating(true);
        await fetchJson<{ trading_active: boolean }>("/api/trading-active", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ trading_active: nextState }),
        });
        setHealth((prev) => (prev ? { ...prev, trading_active: nextState } : prev));
        setNavSummary((prev) => (prev ? { ...prev, trading_active: nextState } : prev));
      } catch (error) {
        console.error("Failed to toggle trading active", error);
      } finally {
        setTradingActiveUpdating(false);
      }
    },
    []
  );

  const enabledPairs = health?.enabled_pairs ?? [];
  const healthTimestamp = health?.timestamp ? new Date(health.timestamp) : null;
  const healthTimestampLabel = healthTimestamp
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(healthTimestamp)
    : "–";

  const lastPricingLabel = lastPricingUpdate
    ? new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(
      new Date(lastPricingUpdate)
    )
    : "–";

  const serviceBadgeLabel =
    serviceState === "online" ? "Online" : serviceState === "offline" ? "Offline" : "Connecting";

  const navSnapshot = navSummary?.nav_snapshot ?? null;
  const exposureUsd = navSummary?.exposure_usd ?? null;
  const navTimestampIso = navSummary?.timestamp ?? null;
  const navTimestampLabel = navTimestampIso ? formatDateTime(navTimestampIso) : "–";
  const navRelativeHint = navTimestampIso ? formatRelativeFromIso(navTimestampIso) : "Snapshot pending";
  const killSwitchState = health?.kill_switch ?? navSummary?.kill_switch;
  const killSwitchEngaged = killSwitchState ?? true;
  const killSwitchReady = typeof killSwitchState === "boolean";
  const killSwitchStateLabel = killSwitchReady
    ? killSwitchEngaged
      ? "Kill switch engaged — trading paused"
      : "Kill switch clear — trading permitted"
    : "Awaiting kill switch status";

  const tradingActiveState = health?.trading_active ?? navSummary?.trading_active ?? false;
  const tradingActiveLabel = tradingActiveState
    ? "Auto-Trading Execution LIVE"
    : "Shadow Mode (No Live Orders)";

  const gateStats = useMemo(() => {
    let staleCount = 0;
    let worstAge: number | null = null;
    let freshestUpdate: string | null = null;
    let blockedCount = 0;
    for (const entry of gateMetrics) {
      const age = entry.age_seconds;
      if (age != null) {
        if (worstAge === null || age > worstAge) {
          worstAge = age;
        }
        if (age > GATE_STALE_THRESHOLD) {
          staleCount += 1;
        }
      }
      if (entry.updated_at && (!freshestUpdate || entry.updated_at > freshestUpdate)) {
        freshestUpdate = entry.updated_at;
      }
      if (entry.reasons && entry.reasons.length > 0) {
        blockedCount += 1;
      }
    }
    return { staleCount, worstAge, freshestUpdate, blockedCount };
  }, [gateMetrics]);

  const gateMetricTone: MetricTone = gateStats.staleCount > 0
    ? "danger"
    : gateStats.blockedCount > 0
      ? "warning"
      : gateStats.worstAge != null && gateStats.worstAge > GATE_STALE_THRESHOLD * 0.6
        ? "warning"
        : "success";

  const gateMetricValue = gateStats.worstAge != null ? `${Math.round(gateStats.worstAge)}s` : "–";
  const gateMetricHint = gateStats.staleCount > 0
    ? `${gateStats.staleCount} instrument${gateStats.staleCount > 1 ? "s" : ""} stale`
    : gateStats.blockedCount > 0
      ? `${gateStats.blockedCount} blocked by profile`
      : gateStats.worstAge != null
        ? "Max gate age across enabled pairs"
        : "Waiting for gate payloads";

  const gateByInstrument = useMemo(() => {
    const map: Record<string, GateMetric> = {};
    for (const entry of gateMetrics) {
      map[entry.instrument?.toUpperCase() ?? ""] = entry;
    }
    return map;
  }, [gateMetrics]);

  const hazardGuardsData = useMemo(() => {
    return MAJOR_INSTRUMENTS.map((instrument) => {
      const gate = gateByInstrument[instrument] || null;
      const hazardValue = gate?.hazard ?? null;
      const hazardThreshold = gate?.hazard_threshold ?? null;
      const reasons = gate?.reasons ?? [];
      const metrics = (gate?.structure as Record<string, number> | undefined) ?? {};

      let status: "active" | "ready" | "blocked" | "idle" = "idle";
      if (!gate) status = "idle";
      else if (reasons.length === 0) status = "active";
      else if (reasons.includes("hazard_exceeds_max") || reasons.includes("hazard_exceeds_adaptive_threshold")) status = "ready";
      else status = "blocked";

      const segments = HAZARD_GUARDS.map((guard) => {
        const value = metrics[guard.id];
        let ready = status === "idle" ? false : true;
        if (value != null) {
          if ('max' in guard) ready = value <= guard.max;
          else if ('min' in guard) ready = value >= guard.min;
        } else if (status === "idle") {
          ready = false;
        }
        return { id: guard.id, label: guard.label, value: value != null ? value : null, ready };
      });

      const guardsPassing = segments.filter(s => s.ready).length;
      const totalGuards = segments.length;

      const statusLabel = status === "idle" ? "Awaiting Stream" : status === "active" ? "Armed (Admit)" : status === "ready" ? "Waiting on Hazard" : "Guards Blocked";
      const missingText = status === "idle" ? "Backend connection lost" : reasons.length > 0 ? reasons.map(formatGateReason).join(", ") : "All conditions met";

      return { instrument, hazardValue, hazardThreshold, segments, guardsPassing, totalGuards, status, statusLabel, missingText };
    });
  }, [instrumentList, gateByInstrument]);

  const gateTimestampLabel = gateStats.freshestUpdate ? `Gates updated ${formatDateTime(gateStats.freshestUpdate)}` : "Awaiting gate stream";

  return (
    <div className="dashboard">
      <div className="dashboard__inner">
        <section className="panel hero">
          <div className="hero__copy">
            <span className="hero__eyebrow">SEP Trading</span>
            <h1 className="hero__title">Operations Console</h1>
            <p className="hero__subtitle">Live view of gate admits, execution posture, and price action.</p>
            <div className="hero__chips" role="list">
              {enabledPairs.length > 0 ? (
                enabledPairs.map((instrument) => (
                  <span key={instrument} className="chip" role="listitem">
                    {instrument}
                  </span>
                ))
              ) : (
                <span className="chip chip--muted" role="listitem">
                  No instruments enabled
                </span>
              )}
            </div>
          </div>
          <div className="hero__status" style={{ display: 'flex', gap: '16px', alignItems: 'flex-start' }}>
            <div>
              <button
                type="button"
                className={`status-pill status-pill--button ${killSwitchEngaged ? "status-pill--danger" : "status-pill--online"
                  }`}
                onClick={() => toggleKillSwitch(!killSwitchEngaged)}
                disabled={killSwitchUpdating}
                aria-pressed={killSwitchEngaged}
              >
                <span className="status-pill__dot" />
                Kill Switch
                <span className="status-pill__state">{killSwitchEngaged ? "Engaged" : "Clear"}</span>
              </button>
              <p className="hero__timestamp" style={{ marginTop: '8px' }}>{killSwitchStateLabel}</p>
            </div>

            <div>
              <button
                type="button"
                className={`status-pill status-pill--button ${tradingActiveState ? "status-pill--warning" : "status-pill--muted"
                  }`}
                onClick={() => toggleTradingActive(!tradingActiveState)}
                disabled={tradingActiveUpdating}
                aria-pressed={tradingActiveState}
              >
                <span className="status-pill__dot" />
                Execution Phase
                <span className="status-pill__state">{tradingActiveState ? "Live Trading On" : "Shadow Mode"}</span>
              </button>
              <p className="hero__timestamp" style={{ marginTop: '8px' }}>{tradingActiveLabel}</p>
            </div>

            <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
              <p className="hero__timestamp">
                {healthTimestamp ? `Health ping ${healthTimestampLabel}` : "Awaiting health heartbeat"}
              </p>
            </div>
          </div>
        </section>

        <section className="panel ops-panel">
          <header className="panel__header">
            <h2 className="panel__title">Operations Status</h2>
            <span className="panel__timestamp">
              {navTimestampIso ? `Account snapshot ${navTimestampLabel}` : "Awaiting account snapshot"}
            </span>
          </header>
          <div className="metrics-grid">
            <div className="metric">
              <span className="metric__label">Kill Switch</span>
              <span className={metricValueClass(health?.kill_switch ? "danger" : "success")}>
                {health ? (health.kill_switch ? "Engaged" : "Clear") : "—"}
              </span>
              <span className="metric__hint">
                {health?.kill_switch ? "Orders blocked until switched off" : "Trading permitted"}
              </span>
            </div>
            <div className="metric">
              <span className="metric__label">Trading Mode</span>
              <span className={metricValueClass(health?.trading_active ? "success" : "muted")}>
                {health ? (health.trading_active ? "Active" : "Standby") : "—"}
              </span>
              <span className="metric__hint">Execution loop status</span>
            </div>
            <div className="metric">
              <span className="metric__label">Equity (NAV)</span>
              <span className={metricValueClass(navSnapshot != null ? "accent" : "muted")}>
                {navSnapshot != null ? formatCurrency(navSnapshot) : "—"}
              </span>
              <span className="metric__hint">{navRelativeHint}</span>
            </div>
            <div className="metric">
              <span className="metric__label">Exposure</span>
              <span className={metricValueClass(exposureUsd ? "warning" : "muted")}>
                {exposureUsd ? formatCurrency(exposureUsd, { compact: true }) : "—"}
              </span>
              <span className="metric__hint">Portfolio notional at risk</span>
            </div>
            <div className="metric">
              <span className="metric__label">Gate Freshness</span>
              <span className={metricValueClass(gateMetricTone)}>{gateMetricValue}</span>
              <span className="metric__hint">{gateMetricHint}</span>
            </div>
          </div>
        </section>

        <section className="panel panel--guards">
          <header className="panel__header">
            <h2 className="panel__title">Execution Readiness</h2>
            <span className="panel__timestamp">{gateTimestampLabel}</span>
          </header>
          <div className="positions-table-wrapper">
            <table className="table positions-table">
              <thead>
                <tr>
                  <th scope="col">Instrument</th>
                  <th scope="col" className="right">Live Hazard / Max</th>
                  <th scope="col" className="right">Structural Guards</th>
                </tr>
              </thead>
              <tbody>
                {hazardGuardsData.map((row) => (
                  <tr key={row.instrument}>
                    <th scope="row" style={{ verticalAlign: 'middle' }}>{row.instrument}</th>
                    <td className="right" style={{ verticalAlign: 'middle' }}>
                      <span className={`status-pill ${row.hazardValue != null && row.hazardThreshold != null && row.hazardValue <= row.hazardThreshold ? "status-pill--success" : "status-pill--warning"}`} style={{ display: 'inline-flex', padding: '4px 8px', borderRadius: '4px', fontSize: '12px', fontWeight: 600 }}>
                        {row.hazardValue != null ? formatPercent(row.hazardValue) : "–"} / {row.hazardThreshold != null ? formatPercent(row.hazardThreshold) : "–"}
                      </span>
                    </td>
                    <td className="right" style={{ verticalAlign: 'middle' }}>
                      <div className={`stoplight stoplight--${row.status === "active" ? "active" : row.status === "ready" ? "ready" : "warming"}`} style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'flex-end', width: '100%' }} title={row.missingText}>
                        <div className="stoplight__segments" style={{ marginRight: '8px' }}>
                          {row.segments.map((segment) => (
                            <span
                              key={`${row.instrument}-${segment.id}`}
                              className={`stoplight__segment ${segment.ready ? "stoplight__segment--on" : "stoplight__segment--off"}`}
                              title={`${segment.label}: ${segment.value != null ? formatNumber(segment.value) : "–"} (${segment.ready ? "Pass" : "Fail"})`}
                            />
                          ))}
                        </div>
                        <div className="stoplight__content" style={{ textAlign: 'right' }}>
                          <span className="stoplight__label" style={{ display: 'block', whiteSpace: 'nowrap' }}>
                            {row.statusLabel}
                          </span>
                          <span className="stoplight__meta" style={{ display: 'block', fontSize: '11px', whiteSpace: 'nowrap', maxWidth: '200px', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {row.missingText}
                          </span>
                        </div>
                      </div>
                    </td>
                  </tr>
                ))}
                {instrumentList.length === 0 && (
                  <tr>
                    <td colSpan={3} className="empty-cell">
                      Awaiting instruments
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel panel--positions">
          <header className="panel__header">
            <h2 className="panel__title">Open Positions</h2>
            <span className="panel__timestamp">
              {navSummary?.positions && navSummary.positions.length
                ? `${navSummary.positions.length} instrument${navSummary.positions.length === 1 ? "" : "s"}`
                : "No active positions"}
            </span>
          </header>
          <div className="positions-table-wrapper">
            <table className="table positions-table">
              <thead>
                <tr>
                  <th scope="col">Instrument</th>
                  <th scope="col" className="right">Units</th>
                  <th scope="col" className="right">Exposure</th>
                </tr>
              </thead>
              <tbody>
                {navSummary?.positions && navSummary.positions.length > 0 ? (
                  navSummary.positions.map((position) => (
                    <tr key={position.instrument}>
                      <th scope="row">{position.instrument}</th>
                      <td className="right">{formatInteger(position.net_units)}</td>
                      <td className="right">{formatCurrency(position.exposure, { compact: true })}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={3} className="empty-cell">
                      No open positions
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel panel--pricing">
          <header className="panel__header">
            <h2 className="panel__title">Pricing Snapshot</h2>
            <span className="panel__timestamp">{lastPricingUpdate ? `Last update ${lastPricingLabel}` : "Awaiting prices"}</span>
          </header>
          {pricingError && (
            <div className="notice notice--error">
              <strong>Pricing offline.</strong> {pricingError}
            </div>
          )}
          <div className="pricing-table-wrapper">
            <table className="table pricing-table">
              <thead>
                <tr>
                  <th scope="col">Instrument</th>
                  <th scope="col" className="right">
                    Bid
                  </th>
                  <th scope="col" className="right">
                    Ask
                  </th>
                  <th scope="col" className="right">
                    Mid
                  </th>
                  <th scope="col" className="right">
                    Δ (vs last)
                  </th>
                  <th scope="col" className="right">
                    Spread
                  </th>
                  <th scope="col" className="right">Regime</th>
                  <th scope="col" className="right">Hazard</th>
                  <th scope="col" className="right">Signal</th>
                  <th scope="col" className="right">Gate Age</th>
                  <th scope="col" className="right trend-head">Trend</th>
                </tr>
              </thead>
              <tbody>
                {sortedPricing.length === 0 ? (
                  <tr>
                    <td colSpan={11} className="empty-cell">
                      {serviceState === "online" && !pricingError
                        ? "No instruments streaming prices"
                        : "Pricing data unavailable"}
                    </td>
                  </tr>
                ) : (
                  sortedPricing.map((row) => {
                    const gateInfo = gateByInstrument[row.instrument.toUpperCase()];
                    const gateAge = gateInfo?.age_seconds ?? null;
                    const gateUpdated = gateInfo?.updated_at ?? null;
                    const gateReasons = gateInfo?.reasons ?? [];
                    const gateReasonDetails = gateInfo?.reason_details ?? [];
                    const hazardValue = gateInfo?.hazard ?? null;
                    const hazardThreshold = gateInfo?.hazard_threshold ?? null;
                    const regimeLabel = gateInfo?.regime?.label ?? null;
                    const regimeConfidence = gateInfo?.regime?.confidence ?? null;
                    const gateDirection = normalizeDirection(gateInfo?.direction);
                    const gateDirectionTone = gateDirection === "FLAT" ? "gate-reason--blocked" : "gate-reason--ok";
                    const hazardTone =
                      hazardThreshold != null &&
                        hazardValue != null &&
                        hazardValue > hazardThreshold
                        ? "hazard-cell hazard-cell--warn"
                        : "hazard-cell";
                    const gateTone = [
                      "gate-age",
                      gateAge != null && gateAge > GATE_STALE_THRESHOLD ? "gate-age--stale" : null,
                      gateReasons.length > 0 ? "gate-age--blocked" : null,
                    ]
                      .filter(Boolean)
                      .join(" ");
                    const historyPoints = priceHistory[row.instrument] ?? [];
                    return (
                      <tr key={row.instrument}>
                        <th scope="row">{row.instrument}</th>
                        <td className="right">{formatNumber(row.bid)}</td>
                        <td className="right">{formatNumber(row.ask)}</td>
                        <td className="right">{formatNumber(row.mid)}</td>
                        <td className={`right delta ${deltaClass(row.change)}`}>{formatDelta(row.change)}</td>
                        <td className="right">{formatSpread(row.spread)}</td>
                        <td className="right regime-cell">
                          <span
                            className={`regime-badge regime-badge--${(regimeLabel || "unknown").toLowerCase()}`}
                            title={regimeLabel || "Unknown regime"}
                          >
                            {formatRegimeLabel(regimeLabel)}
                          </span>
                          <span className="regime-confidence">
                            {regimeConfidence != null ? formatPercent(regimeConfidence) : "–"}
                          </span>
                        </td>
                        <td className={`right ${hazardTone}`}>
                          <span>{hazardValue != null ? formatPercent(hazardValue) : "–"}</span>
                          {hazardThreshold != null && (
                            <span className="hazard-threshold">/ {formatPercent(hazardThreshold)}</span>
                          )}
                        </td>
                        <td className="right">
                          <div className="gate-reasons">
                            <span className={`gate-reason ${gateDirectionTone}`}>
                              {formatDirection(gateDirection)}
                            </span>
                            {gateInfo?.st_peak ? (
                              <span className="gate-reason gate-reason--ok">ST peak</span>
                            ) : null}
                            {gateInfo?.ml_probability != null ? (
                              <span className="gate-reason">ML {formatPercent(gateInfo.ml_probability)}</span>
                            ) : null}
                          </div>
                        </td>
                        <td className="right">
                          <span
                            className={gateTone}
                            title={
                              gateUpdated
                                ? `Updated ${formatDateTime(gateUpdated)}${gateReasonDetails.length ? `\n${gateReasonDetails.join("\n")}` : ""}`
                                : undefined
                            }
                          >
                            {formatGateAge(gateAge)}
                          </span>
                          <div className="gate-reasons">
                            {gateReasons.length > 0 ? (
                              gateReasons.map((reason) => (
                                <span key={reason} className="gate-reason gate-reason--blocked">
                                  {formatGateReason(reason)}
                                </span>
                              ))
                            ) : (
                              <span className="gate-reason gate-reason--ok">Admitted</span>
                            )}
                          </div>
                        </td>
                        <td className="right trend-cell">
                          <Sparkline points={historyPoints} />
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}

function numberOrNull(value: unknown): number | null {
  if (typeof value !== "number") return null;
  if (!Number.isFinite(value)) return null;
  return value;
}

function safeFixed(value: number | null | undefined, digits: number, fallback = "–"): string {
  if (value == null || Number.isNaN(value)) return fallback;
  try {
    return value.toFixed(digits);
  } catch (error) {
    return fallback;
  }
}

function formatNumber(value: number | null): string {
  if (value == null) return "–";
  return value.toFixed(5);
}

function formatDelta(value: number | null): string {
  if (value == null) return "–";
  const formatted = value >= 0 ? `+${value.toFixed(5)}` : value.toFixed(5);
  return formatted;
}

function formatSpread(value: number | null): string {
  if (value == null) return "–";
  return value.toFixed(5);
}

function formatGateAge(value: number | null): string {
  if (value == null) return "–";
  if (value >= 3600) return `${Math.round(value / 3600)}h`;
  if (value >= 120) return `${Math.round(value / 60)}m`;
  return `${Math.round(value)}s`;
}

function formatGateReason(code: string): string {
  const mapping: Record<string, string> = {
    missing_payload: "No payload",
    missing_strategy_profile: "Missing profile",
    missing_hazard: "No hazard",
    flat_direction: "Flat direction",
    invalid_hazard: "Hazard invalid",
    hazard_exceeds_max: "Hazard above max",
    hazard_below_min: "Hazard below min",
    repetitions_short: "Insufficient reps",
    coherence_low: "Coherence low",
    coherence_invalid: "Coherence invalid",
    stability_below_min: "Stability low",
    stability_invalid: "Stability invalid",
    entropy_above_max: "Entropy high",
    entropy_invalid: "Entropy invalid",
    coherence_tau_slope_above_max: "Coherence slope high",
    coherence_tau_slope_invalid: "Coherence slope invalid",
    domain_wall_slope_above_max: "Domain slope high",
    domain_wall_slope_invalid: "Domain slope invalid",
    spectral_lowf_share_below_min: "Low-frequency share low",
    spectral_lowf_share_invalid: "Low-frequency share invalid",
    reynolds_above_max: "Reynolds high",
    reynolds_invalid: "Reynolds invalid",
    temporal_half_life_below_min: "Half-life low",
    temporal_half_life_invalid: "Half-life invalid",
    spatial_corr_length_below_min: "Corr length low",
    spatial_corr_length_invalid: "Corr length invalid",
    pinned_alignment_below_min: "Pinned alignment low",
    pinned_alignment_invalid: "Pinned alignment invalid",
    regime_filtered: "Regime filtered",
    regime_missing: "Regime missing",
    regime_confidence_low: "Regime confidence low",
    regime_confidence_missing: "Regime confidence missing",
    st_no_peak_reversal: "No reversal peak",
    semantic_filter_missing: "Semantic filter missing",
    ml_confidence_low: "ML confidence low",
    ml_eval_error: "ML error",
  };
  return mapping[code] ?? code.replace(/_/g, " ");
}

function normalizeDirection(direction: string | undefined): "BUY" | "SELL" | "FLAT" {
  const value = (direction ?? "").toUpperCase();
  if (value === "BUY" || value === "SELL") return value;
  return "FLAT";
}

function formatDirection(direction: "BUY" | "SELL" | "FLAT"): string {
  if (direction === "BUY") return "Buy";
  if (direction === "SELL") return "Sell";
  return "Flat";
}

function formatRegimeLabel(label: string | null): string {
  if (!label) return "Unknown";
  const normalized = label.toLowerCase();
  const mapping: Record<string, string> = {
    trend_bull: "Trend ↑",
    trend_bear: "Trend ↓",
    mean_revert: "Mean Revert",
    chaotic: "Chaotic",
    neutral: "Neutral",
  };
  return mapping[normalized] ?? label;
}

function formatCurrency(value: number | null, options?: { compact?: boolean }): string {
  if (value == null || Number.isNaN(value)) return "–";
  const formatter = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    notation: options?.compact ? "compact" : "standard",
    minimumFractionDigits: options?.compact ? 0 : 2,
    maximumFractionDigits: options?.compact ? 1 : 2,
  });
  return formatter.format(value);
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "–";
  return `${safeFixed(value * 100, 1)}%`;
}

function formatBp(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "–";
  const fixed = safeFixed(value, 1);
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${fixed} bp`;
}

function formatInteger(value: number | null): string {
  if (value == null || Number.isNaN(value)) return "–";
  return value.toLocaleString();
}

function deltaClass(value: number | null): string {
  if (value == null) return "delta--muted";
  if (value > 0) return "delta--positive";
  if (value < 0) return "delta--negative";
  return "delta--muted";
}

function metricValueClass(tone: MetricTone): string {
  return `metric__value metric__value--${tone}`;
}

function rocTone(value: number | null): string {
  const base = "roc-cell";
  if (value == null) return `${base} roc-cell--muted`;
  if (value > 0) return `${base} roc-cell--positive`;
  if (value < 0) return `${base} roc-cell--negative`;
  return `${base} roc-cell--muted`;
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function formatRelativeFromIso(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "Timestamp unavailable";
  const diffMillis = date.getTime() - Date.now();
  const absSeconds = Math.round(Math.abs(diffMillis) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (absSeconds > 86_400) {
    const days = Math.round(diffMillis / 86_400_000);
    return formatter.format(days, "day");
  }
  if (absSeconds > 3600) {
    const hours = Math.round(diffMillis / 3_600_000);
    return formatter.format(hours, "hour");
  }
  if (absSeconds > 60) {
    const minutes = Math.round(diffMillis / 60_000);
    return formatter.format(minutes, "minute");
  }
  return formatter.format(Math.round(diffMillis / 1000), "second");
}

function Sparkline({ points }: { points: HistoryPoint[] }) {
  if (!points || points.length < 2) {
    return <span className="sparkline sparkline--empty">–</span>;
  }
  const values = points.map((point) => point.close);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const width = 100;
  const height = 32;
  const coords = points
    .map((point, index) => {
      const x = (index / (points.length - 1)) * width;
      const y = height - ((point.close - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const direction = points[points.length - 1].close - points[0].close;
  const tone = direction > 0 ? "sparkline--up" : direction < 0 ? "sparkline--down" : "sparkline--flat";
  return (
    <svg className={`sparkline ${tone}`} viewBox={`0 0 ${width} ${height}`} role="img" aria-hidden>
      <polyline className="sparkline__stroke" points={coords} />
    </svg>
  );
}
