"""
pnl_chart.py — PnL chart generation for /last command.
Renders a horizontal bar chart of recent alerts' PnL% using matplotlib,
sends as photo instead of text-only message.

Falls back gracefully to text if matplotlib is not installed —
this keeps the bot fully functional even on minimal VPS setups
where pip install matplotlib wasn't run.
"""
import io
import logging
import time

log = logging.getLogger("PnLChart")

try:
    import matplotlib
    matplotlib.use("Agg")   # headless backend — no display needed on VPS
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    log.warning("matplotlib не установлен — /last будет работать в текстовом режиме")


# ─── COLOR SCHEME ─────────────────────────────────────────────────────────────

def _color_for_pnl(pnl: float) -> str:
    if pnl >= 100:  return "#00e676"   # bright green — x2+
    if pnl >= 50:   return "#66bb6a"   # green
    if pnl >= 0:    return "#ffd54f"   # yellow — break even
    if pnl >= -50:  return "#ff9800"   # orange
    return "#e53935"                  # red — heavy loss


def build_pnl_chart(alerts: list[dict]) -> bytes | None:
    """
    alerts: list of dicts with keys: symbol, mcap, outcome, outcome_mcap, sent_at
    Returns PNG bytes, or None if matplotlib unavailable or no data.
    """
    if not MATPLOTLIB_AVAILABLE or not alerts:
        return None

    labels   = []
    pnls     = []
    colors   = []
    statuses = []

    for a in alerts:
        symbol  = a.get("symbol", "???")
        mcap    = a.get("mcap", 0) or 0
        out_mcp = a.get("outcome_mcap")
        outcome = a.get("outcome")

        if mcap <= 0:
            continue

        if out_mcp:
            pnl = (out_mcp - mcap) / mcap * 100
        else:
            pnl = 0.0   # still alive, unknown — shown as neutral marker

        labels.append(symbol)
        pnls.append(pnl)
        colors.append(_color_for_pnl(pnl))
        statuses.append(outcome.upper() if outcome else "LIVE")

    if not labels:
        return None

    # reverse so most recent appears on top
    labels   = labels[::-1]
    pnls     = pnls[::-1]
    colors   = colors[::-1]
    statuses = statuses[::-1]

    fig, ax = plt.subplots(figsize=(7, 0.7 * len(labels) + 1.2), dpi=150)
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#0e1117")

    bars = ax.barh(labels, pnls, color=colors, height=0.6, edgecolor="none")

    ax.axvline(0, color="#555", linewidth=1)
    ax.set_xlabel("PnL %", color="#ccc", fontsize=10)
    ax.tick_params(colors="#ccc", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#333")

    # annotate each bar with value + status
    for bar, pnl, status in zip(bars, pnls, statuses):
        width = bar.get_width()
        align = "left" if width >= 0 else "right"
        offset = 3 if width >= 0 else -3
        label_text = f"{pnl:+.0f}% ({status})" if status != "LIVE" else "⏳ live"
        ax.text(
            width + offset, bar.get_y() + bar.get_height() / 2,
            label_text, va="center", ha=align,
            color="#fff", fontsize=9, fontweight="bold",
        )

    ax.set_title("Последние алерты — PnL", color="#fff", fontsize=13, pad=12)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
